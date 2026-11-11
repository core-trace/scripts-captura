"""Lê os CSVs da trusted e publica lotes JSON e resumos separados no client."""
import io
import time
import boto3
import pandas as pd
import json
import csv
import hashlib
import sqlite3
import math
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from botocore.exceptions import ClientError
from resumos_gold import AcumuladorResumo
from formato_gold import (organizar_metricas, organizar_processo, organizar_container,
                          resumir_documento)

BUCKET = "bucket-coretrace"
SILVER = "trusted/"
GOLD = "client/"

s3 = boto3.client("s3")


def preparar_gold(df, tipo_arquivo):

    print("Preparando dados para Gold...")

    df = df.copy()
    df.columns = df.columns.astype(str).str.strip().str.upper()
    if df.columns.duplicated().any():
        raise ValueError("Colunas duplicadas")

    obrigatorias = ["TIMESTAMP", "ENDERECO_MAC"]

    if tipo_arquivo == "processos":
        obrigatorias.extend(["PID", "NOME"])
    elif tipo_arquivo == "docker":
        obrigatorias.append("CONTAINER")

    faltantes = []
    for coluna in obrigatorias:
        if coluna not in df.columns:
            faltantes.append(coluna)

    if faltantes:
        raise ValueError(
            f"Colunas obrigatorias ausentes: {faltantes}"
        )

    # Uma data inválida interrompe em vez de apagar a linha
    df["TIMESTAMP"] = pd.to_datetime(
        df["TIMESTAMP"],
        errors="raise",
        format="ISO8601"
    )

    # O mesmo MAC com hífen ou dois pontos vai pra mesma pasta
    df["ENDERECO_MAC"] = (
        df["ENDERECO_MAC"]
        .astype("string")
        .str.strip()
        .str.upper()
        .str.replace("-", ":", regex=False)
    )

    # isna procura campos vazios. 
    # O padrão do MAC exige seis pares AA:BB:CC:DD:EE:FF
    tem_campo_vazio = df[obrigatorias].isna().any().any()
    
    macs_validos = df["ENDERECO_MAC"].str.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}").fillna(False)
    
    tem_mac_zerado = df["ENDERECO_MAC"].eq("00:00:00:00:00:00").any()
    
    if tem_campo_vazio or not macs_validos.all() or tem_mac_zerado:
        raise ValueError("Identidade ou horario ausente/invalido")
    
    if tipo_arquivo == "processos":
        pids = pd.to_numeric(df["PID"], errors="raise")
        # O resto da divisão por 1 precisa ser zero, pq PID não pode ser fracionado
        pid_negativo = pids.lt(0)
        pid_fracionado = pids.mod(1).ne(0)
        
        nome_vazio = df["NOME"].astype("string").str.strip().eq("")
        if (pid_negativo | pid_fracionado).any() or nome_vazio.any():
            raise ValueError("PID ou nome do processo invalido")
        df["PID"] = pids.astype("Int64")
        
    elif tipo_arquivo == "docker" and df["CONTAINER"].astype("string").str.strip().eq("").any():
        raise ValueError("Nome do container vazio")

    if df["TIMESTAMP"].dt.tz is not None:
        raise ValueError("As capturas atuais devem usar horario local sem fuso")

    df = df.sort_values(
        ["ENDERECO_MAC", "TIMESTAMP"]
    ).reset_index(drop=True)

    print("Registros preparados:", len(df))

    return df


# Função pra converter csv para json

def converter_registros(df):

    # O drop pra tirar a coluna de data pq hoje em dia usamos a data como parêmetro só pra agrupar os dados
    # Ignorar erros pq se algum arquivo vier sem essa coluna ele só ignora isso sem dar erro que trave tudo
    df = df.drop(columns=["DATA"], errors="ignore")

    # Lê o texto json e transforma numa estrutura de lista com dicionários que o python entende
    return json.loads(
        # Transforma em json
        df.to_json(
            # Cada linha/registro vira um objeto. A coluna vira a chave e o conteúdo vira o valor
            orient="records",
            date_format="iso",
            date_unit="us",
            double_precision=15,
            # Pros acentos e ç e ã ficarem sem bugar
            force_ascii=False
        )
    )

def ler_arquivo(chave, tipo_arquivo):
    # Le o CSV da trusted e fechamos a resposta mesmo se der erro na leitura
    resposta = s3.get_object(Bucket=BUCKET, Key=chave)
    corpo = resposta["Body"]
    try:
        conteudo = corpo.read()
    finally:
        corpo.close()

    # utf-8-sig também aceita o marcador que alguns programas colocam no começo do CSV
    texto = conteudo.decode("utf-8-sig")
    leitor = csv.reader(io.StringIO(texto), strict=True)
    cabecalho = next(leitor, [])
    colunas = []
    for nome in cabecalho:
        colunas.append(nome.strip().upper())

    # Confere se faltou coluna ou se alguma foi escrita duas vezes
    # O pandas pode renomear colunas repetidas, então a gente confere antes
    if not colunas or not all(colunas) or len(set(colunas)) != len(colunas):
        raise ValueError("Cabecalho vazio ou colunas duplicadas")
    
    for linha in leitor:
        if linha and len(linha) != len(colunas):
            raise ValueError(f"Linha CSV incompleta: {leitor.line_num}")

    # Esses campos são texto mesmo quando parecem números, como um boot ID com zeros na frente
    colunas_texto = {
        "ENDERECO_MAC": "string",
        "NOME": "string",
        "USUARIO": "string",
        "CONTAINER": "string",
        "RAM_OOM_BOOT_ID": "string",
        "CPU_SCHEMA_VERSION": "string",

        # Identificação e informações do contêiner
        "CONTAINER_ID": "string",
        "IMAGEM": "string",
        "STATUS": "string",
        "HEALTH_STATUS": "string",
        "OOM_KILLED": "string",
        "STARTED_AT": "string",
        "FINISHED_AT": "string",

        # Eventos
        "EVENT_ID": "string",
        "EVENT_TYPE": "string",
        "EVENT_ACTION": "string",
        "EVENT_DETAILS": "string",

        # Logs
        "LOG_LEVEL": "string",
        "LOG_MENSAGEM": "string"
    }
    
    df = pd.read_csv(
        io.StringIO(texto), header=0, names=colunas, low_memory=False,
        keep_default_na=False, na_values=[""], dtype=colunas_texto
    )

    # Colunas obrigatórias para cada tipo de arquivo Docker.
    # Isso evita processar um CSV na categoria errada.
    esquemas_docker = {
        "docker_metricas": {
            "CONTAINER_ID",
            "CONTAINER",
            "CPU_PERCENT",
            "RAM_PERCENT"
        },
        "docker_eventos": {
            "CONTAINER_ID",
            "CONTAINER",
            "EVENT_ID",
            "EVENT_TYPE",
            "EVENT_ACTION"
        },
        "docker_logs": {
            "CONTAINER_ID",
            "CONTAINER",
            "LOG_MENSAGEM"
        }
    }
    
    if tipo_arquivo in esquemas_docker:

        obrigatorias = esquemas_docker[tipo_arquivo]

        faltantes = obrigatorias - set(df.columns)

        if faltantes:
            raise ValueError(
                f"Colunas Docker ausentes: {sorted(faltantes)}"
            )

    elif tipo_arquivo == "processos":

        if not {"PID", "NOME"}.issubset(df.columns):
            raise ValueError("Esquema de processos invalido")

        if "CONTAINER" in df.columns:
            raise ValueError("Container recebido na pasta de processos")

    elif tipo_arquivo == "metricas":

        if {"PID", "NOME", "CONTAINER"} & set(df.columns):
            raise ValueError("Esquema incompativel com metricas")

    else:
        raise ValueError(f"Tipo de arquivo desconhecido: {tipo_arquivo}")

    # Esse hash identifica o conteúdo de origem
    hash_origem = hashlib.sha256(conteudo).hexdigest()
    df_preparado = preparar_gold(df, tipo_arquivo)
    return df_preparado, tipo_arquivo, hash_origem


def novo_documento(mac, data):
    return {"schema_version": 1, "endereco_mac": mac, "data": data,
            "metricas": [], "processos": [], "containers": []}


def normalizar_id(valor):
    # CSVs podem trazer a mesma leitura como 1 ou 1.0. Os dois precisam gerar o mesmo ID
    # A função percorre os valores por dentro sem alterar a captura
    if isinstance(valor, dict):
        resultado = {}
        for campo, conteudo in valor.items():
            resultado[campo] = normalizar_id(conteudo)
        return resultado
    if isinstance(valor, list):
        resultado = []
        for item in valor:
            resultado.append(normalizar_id(item))
        return resultado
    if isinstance(valor, float) and math.isfinite(valor) and valor.is_integer():
        return int(valor)
    return valor


def identificar_registros(documento):
    # O ID serve pro Java ver uma observação já vista, mesmo em outro lote
    # Usar só o horário não resolve, pq duas leituras diferentes podem ter o mesmo horário
    for categoria in ("metricas", "processos", "containers"):
        for registro in documento[categoria]:
            identificacao = {
                "mac": documento["endereco_mac"],
                "categoria": categoria,
                "registro": registro
            }
            texto = json.dumps(
                normalizar_id(identificacao), sort_keys=True, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")
            )
            registro["id_captura"] = hashlib.sha256(texto.encode("utf-8")).hexdigest()


def arquivo_existe(chave):
    # Só consultamos se existe, sem baixar o JSON inteiro de novo
    try:
        s3.head_object(Bucket=BUCKET, Key=chave)
        return True
    except ClientError as erro:
        codigo = erro.response.get("Error", {}).get("Code")
        if codigo in ("404", "NoSuchKey", "NotFound"):
            return False
        # Sem permissão ou com falha de conexão, não dá pra concluir que o arquivo não existe
        raise


def montar_lote(grupo, mac, data, tipo, chave, hash_origem):
    lote = novo_documento(mac, data)
    lote["tipo_documento"] = "lote"
    lote["origem"] = {"chave": chave, "conteudo_sha256": hash_origem}

    # Um CSV pode conter só métricas, só processos ou só Docker. As outras listas ficam vazias
    if tipo == "metricas":
        campo = "metricas"
        organizar = organizar_metricas
    elif tipo == "processos":
        campo = "processos"
        organizar = organizar_processo
    else:
        campo = "containers"
        organizar = organizar_container

    # Só removemos linhas inteiramente iguais
    grupo = grupo.drop_duplicates().sort_values("TIMESTAMP", kind="stable")
    
    for registro in converter_registros(grupo):
        lote[campo].append(organizar(registro))

    identificar_registros(lote)
    lote["resumos"] = resumir_documento(lote)

    # Mesma origem e mesmo conteúdo organizado geram o mesmo lote_id na próxima execução
    conteudo = json.dumps(lote, sort_keys=True, ensure_ascii=False, allow_nan=False)
    lote["lote_id"] = hashlib.sha256(conteudo.encode("utf-8")).hexdigest()
    return lote, campo


def preparar_envio(pasta, envios, destino, documento):
    # Guarda o resultado no computador antes de enviar qualquer coisa p bucket
    # Se outro arquivo der erro durante a validação, nada dessa rodada foi publicado ainda
    local = pasta / f"arquivo-{len(envios)}.json"
    conteudo = json.dumps(documento, ensure_ascii=False, allow_nan=False)
    local.write_text(conteudo, encoding="utf-8")
    envios.append((destino, local))


def chave_do_lote(referencia):
    return referencia["chave"]


def processar_gold():
    print("Buscando lotes na Silver...")
    diarios = {}
    lotes_atuais = {}
    envios = []

    # O arquivo de IDs evita guardar milhões de identificadores na RAM
    with TemporaryDirectory(prefix="coretrace-gold-") as temporaria:
        pasta = Path(temporaria)
        with closing(sqlite3.connect(pasta / "ids.sqlite")) as vistos:
            vistos.execute("CREATE TABLE vistos (id TEXT PRIMARY KEY) WITHOUT ROWID")
            paginador = s3.get_paginator("list_objects_v2")
            paginas = paginador.paginate(Bucket=BUCKET, Prefix=SILVER)

            for pagina in paginas:
                for arquivo in pagina.get("Contents", []):
                    chave = arquivo["Key"]
                    if not chave.lower().endswith(".csv"):
                        continue
                    if chave.startswith(SILVER + "metricas/"):
                        tipo = "metricas"
                    elif chave.startswith(SILVER + "processos/"):
                        tipo = "processos"

                    # Identifica os três tipos de arquivos Docker.
                    # Cada um terá seu próprio tratamento no formato_gold.
                    elif chave.startswith(SILVER + "docker/metricas/"):
                        tipo = "docker_metricas"

                    elif chave.startswith(SILVER + "docker/eventos/"):
                        tipo = "docker_eventos"

                    elif chave.startswith(SILVER + "docker/logs/"):
                        tipo = "docker_logs"

                    else:
                        print("Arquivo ignorado:", chave)
                        continue

                    try:
                        df, tipo, hash_origem = ler_arquivo(chave, tipo)
                        df["DATA"] = df["TIMESTAMP"].dt.strftime("%Y-%m-%d")

                        # Até um CSV que cruza meia noite é separado na máquina e no dia corretos
                        for (mac, data), grupo in df.groupby(["ENDERECO_MAC", "DATA"]):
                            lote, campo = montar_lote(grupo, mac, data, tipo, chave, hash_origem)
                            lote_id = lote["lote_id"]
                            destino = f"{GOLD}lotes/maquina={mac}/data={data}/lote={lote_id}/capturas.json"
                            if not arquivo_existe(destino):
                                preparar_envio(pasta, envios, destino, lote)

                            # Mesmo já publicado, o lote participa da reconstrução do resumo diário
                            chave_dia = (mac, data)
                            if chave_dia not in diarios:
                                diarios[chave_dia] = AcumuladorResumo()
                                lotes_atuais[chave_dia] = []
                            acumulador = diarios[chave_dia]
                            lotes_atuais[chave_dia].append({
                                "lote_id": lote_id, "chave": destino, "origem": lote["origem"]
                            })

                            for registro in lote[campo]:
                                # O ID é chave primária. Se já apareceu em outro arquivo, o SQLite ignora
                                # rowcount vale 1 quando inseriu e vale 0 quando era uma repetição
                                consulta = vistos.execute(
                                    "INSERT OR IGNORE INTO vistos VALUES (?)",
                                    (registro["id_captura"],)
                                )
                                if consulta.rowcount == 1:
                                    observacao = novo_documento(mac, data)
                                    observacao[campo] = [registro]
                                    resumo = resumir_documento(observacao)
                                    acumulador.adicionar(resumo, registro["timestamp"])
                    except Exception as erro:
                        raise ValueError(f"Falha em {chave}: {erro}") from erro

            # O resumo diário fica separado dos lotes que o Java usa para gerar alertas
            for (mac, data), acumulador in sorted(diarios.items()):
                referencias = sorted(lotes_atuais[(mac, data)], key=chave_do_lote)
                diario = {
                    "schema_version": 1, "tipo_documento": "resumo_diario",
                    "endereco_mac": mac, "data": data, "resumos": acumulador.resultado(),
                    "lotes_atuais": referencias
                }
                destino = f"{GOLD}resumos/maquina={mac}/data={data}/resumo.json"
                preparar_envio(pasta, envios, destino, diario)

        # Só chegamos aqui se todos os documentos foram preparados sem erro
        # A pasta temporária é apagada automaticamente no final, inclusive se o envio falhar
        for destino, local in envios:
            with local.open("rb") as corpo:
                s3.put_object(Bucket=BUCKET, Key=destino, Body=corpo, ContentType="application/json")
            print("Arquivo JSON enviado para:", destino)
    print("Processamento Gold finalizado!")


if __name__ == "__main__":

    while True:

        try:
            print("\nIniciando processamento Gold...")
            processar_gold()

        except Exception as erro:
            print(
                "Erro durante o processamento:",
                erro
            )

        print("Aguardando 5 minutos...")
        time.sleep(300)
