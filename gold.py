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
import requests
from silver import processar_arquivos as processar_silver
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from botocore.exceptions import ClientError
from resumos_gold import AcumuladorResumo, AcumuladorTempoNoEstado, AcumuladorEvolucao, AcumuladorEstadoRecursos, AcumuladorPrevisao, calcular_severidade, piores_recursos, estado_por_categoria
from formato_gold import (
    organizar_metricas,
    organizar_processo,
    organizar_container,
    organizar_evento,
    organizar_log,
    resumir_documento,
    para_cada_medida,
    para_cada_medida_com_recurso
)

BUCKET = "buckettavares"
SILVER = "trusted/"
GOLD = "client/"

# Fuso utilizado para agrupar as capturas por dia.
# Os timestamps Docker continuam preservados em UTC.
FUSO_DIA = "America/Sao_Paulo"

# Mesma API e mesma convenção de URL que capturas/captura.py já usa.
API_BASE_URL = "http://localhost:3333"

s3 = boto3.client("s3")


def buscar_limites_perfil(token_instalacao, token_perfil_servidor):
    """Busca na API do site os limites de atenção/crítico do perfil desse servidor.

    Essa chamada é opcional pro resultado da Gold: se o site não estiver no ar
    (por exemplo, num teste local sem ele rodando), só avisamos e devolvemos None.
    Quem chamar esta função deve tratar None como "sem tempo no estado pra esse
    MAC", sem interromper o resto do processamento - médias, mínimos e máximos
    não dependem dessa chamada.
    """
    try:
        resposta = requests.get(
            f"{API_BASE_URL}/empresa/metricas",
            headers={
                "Authorization": f"Bearer {token_instalacao}",
                "token-perfil": token_perfil_servidor
            },
            timeout=5
        )
    except requests.RequestException as erro:
        print(
            f"Aviso: nao foi possivel consultar os limites em {API_BASE_URL} ({erro}). "
            "O resumo sera gerado sem o campo de tempo no estado para esse servidor."
        )
        return None

    if resposta.status_code != 200:
        print(
            f"Aviso: a API de metricas respondeu {resposta.status_code} ao buscar limites. "
            "O resumo sera gerado sem o campo de tempo no estado para esse servidor."
        )
        return None

    # Agrupamos pelo nome_coluna, que é a mesma chave usada no campo "metrica" das leituras.
    limites_por_metrica = {}
    for componente in resposta.json():
        limites_por_metrica[componente["nome_coluna"]] = {
            "operador": componente.get("operador"),
            "limite_atencao": componente.get("limite_atencao"),
            "limite_critico": componente.get("limite_critico"),
            "operador_2": componente.get("operador_2"),
            "limite_atencao_2": componente.get("limite_atencao_2"),
            "limite_critico_2": componente.get("limite_critico_2"),
        }
    return limites_por_metrica


def preparar_gold(df, tipo_arquivo):

    print("Preparando dados para Gold...")

    df = df.copy()
    df.columns = df.columns.astype(str).str.strip().str.upper()
    if df.columns.duplicated().any():
        raise ValueError("Colunas duplicadas")

    obrigatorias = ["TIMESTAMP", "ENDERECO_MAC"]

    if tipo_arquivo == "processos":
        obrigatorias.extend(["PID", "NOME"])

    elif tipo_arquivo in (
        "docker_metricas",
        "docker_eventos",
        "docker_logs"
    ):
        # O ID identifica o contêiner mesmo quando seu nome muda.
        obrigatorias.extend(["CONTAINER_ID", "CONTAINER"])

    faltantes = []
    for coluna in obrigatorias:
        if coluna not in df.columns:
            faltantes.append(coluna)

    if faltantes:
        raise ValueError(
            f"Colunas obrigatorias ausentes: {faltantes}"
        )

    if tipo_arquivo.startswith("docker_"):

        # O Docker registra timestamps com informações de fuso.
        # Padronizamos todos os horários em UTC.
        df["TIMESTAMP"] = pd.to_datetime(
            df["TIMESTAMP"],
            errors="raise",
            format="ISO8601",
            utc=True
        )

    else:

        # Mantém o tratamento original dos arquivos antigos.
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
        
    elif tipo_arquivo.startswith("docker_"):

        # O contêiner precisa de ID e nome para ser identificado.
        # Também impedimos identificadores compostos apenas por espaços.
        for coluna in ("CONTAINER_ID", "CONTAINER"):

            valores = df[coluna].astype("string").str.strip()

            if valores.isna().any() or valores.eq("").any():
                raise ValueError(
                    f"Identificacao do container invalida: {coluna}"
                )

    # As capturas tradicionais continuam utilizando horário local sem fuso.
    # Os arquivos Docker já foram normalizados para UTC.
    if not tipo_arquivo.startswith("docker_"):

        if df["TIMESTAMP"].dt.tz is not None:
            raise ValueError(
                "As capturas atuais devem usar horario local sem fuso"
            )

    df = df.sort_values(
        ["ENDERECO_MAC", "TIMESTAMP"]
    ).reset_index(drop=True)

    print("Registros preparados:", len(df))

    return df


# Função pra converter csv para json

def converter_registros(df, tipo_arquivo):

    # O drop pra tirar a coluna de data pq hoje em dia usamos a data como parêmetro só pra agrupar os dados
    # Ignorar erros pq se algum arquivo vier sem essa coluna ele só ignora isso sem dar erro que trave tudo
    df = df.drop(columns=["DATA"], errors="ignore").copy()

    # Os timestamps Docker podem possuir nanossegundos.
    # Convertendo para string antes do JSON, evitamos
    # a perda de precisão causada pelo to_json().
    if tipo_arquivo.startswith("docker_"):

        df["TIMESTAMP"] = df["TIMESTAMP"].map(
            lambda instante: instante.isoformat().replace("+00:00", "Z")
        )

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

        # Tokens: sem forçar texto, "123" sem outro valor na coluna viraria
        # inteiro/float no pandas (ex.: 123.0) e deixaria de bater com o token
        # (texto) cadastrado no banco do site.
        "TOKEN_INSTALACAO": "string",
        "TOKEN_PERFIL_SERVIDOR": "string",

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
    return {
        "schema_version": 1,
        "endereco_mac": mac,
        "data": data,
        "metricas": [],
        "processos": [],
        "containers": [],
        "eventos": [],
        "logs": []
    }


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

    for categoria in (
        "metricas",
        "processos",
        "containers",
        "eventos",
        "logs"
    ):

        # Conta quantas vezes o mesmo log apareceu no lote.
        # Evita atribuir o mesmo ID a duas ocorrências iguais.
        repeticoes_logs = {}

        for registro in documento[categoria]:

            # Evita incluir um ID previamente calculado no hash.
            registro_original = {
                chave: valor
                for chave, valor in registro.items()
                if chave != "id_captura"
            }

            identificacao = {
                "mac": documento["endereco_mac"],
                "categoria": categoria,
                "registro": registro_original
            }

            texto = json.dumps(
                normalizar_id(identificacao),
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":")
            )

            if categoria == "logs":

                # Dois logs completamente iguais dentro do lote
                # recebem números de ocorrência diferentes.
                ordem = repeticoes_logs.get(texto, 0)

                repeticoes_logs[texto] = ordem + 1

                # A ordem diferencia ocorrências iguais,
                # sem precisar alterar a mensagem original.
                texto = f"{texto}|ocorrencia={ordem}"

            registro["id_captura"] = hashlib.sha256(
                texto.encode("utf-8")
            ).hexdigest()

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

    elif tipo == "docker_metricas":
        campo = "containers"
        organizar = organizar_container

    elif tipo == "docker_eventos":
        campo = "eventos"
        organizar = organizar_evento

    elif tipo == "docker_logs":
        campo = "logs"
        organizar = organizar_log

    else:
        raise ValueError(f"Tipo de arquivo desconhecido: {tipo}")

    # Só removemos linhas inteiramente iguais
    # Ordena os registros pelo horário original.
    grupo = grupo.sort_values("TIMESTAMP", kind="stable")

    # Logs e eventos representam ocorrências individuais.
    # Evitamos remover registros apenas por terem conteúdo igual.
    if tipo not in ("docker_eventos", "docker_logs"):
        grupo = grupo.drop_duplicates()
    
    # Informa o tipo para que o conversor saiba
    # quando deve preservar os nanossegundos.
    for registro in converter_registros(grupo, tipo):
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
    tempos_no_estado = {}  # (mac, data) -> AcumuladorTempoNoEstado, em paralelo aos "diarios"
    evolucoes = {}  # (mac, data) -> AcumuladorEvolucao, mesma ideia do tempos_no_estado
    estados_recursos = {}  # (mac, data) -> AcumuladorEstadoRecursos, estado por núcleo/volume/interface
    previsoes = {}  # (mac, data) -> AcumuladorPrevisao, mesma ideia tambem
    tokens_por_mac = {}  # mac -> (token_instalacao, token_perfil_servidor), pra consultar limites depois
    envios = []

    # O arquivo de IDs evita guardar milhões de identificadores na RAM
    with TemporaryDirectory(prefix="coretrace-gold-") as temporaria:
        pasta = Path(temporaria)
        with closing(sqlite3.connect(pasta / "ids.sqlite")) as vistos:
            vistos.execute("CREATE TABLE vistos (id TEXT PRIMARY KEY) WITHOUT ROWID")
            # Guarda as mensagens em disco até montar o resumo de cada máquina/dia.
            vistos.execute(
                "CREATE TABLE logs_diarios (mac TEXT, dia TEXT, instante INTEGER, "
                "id TEXT PRIMARY KEY, registro TEXT)"
            )
            vistos.execute("CREATE INDEX logs_por_dia ON logs_diarios (mac, dia, instante, id)")
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
                        
                        if tipo.startswith("docker_"):

                            # Converte o horário UTC para o fuso utilizado
                            # no agrupamento diário do CoreTrace.
                            # Não altera o TIMESTAMP original.
                            df["DATA"] = (
                                df["TIMESTAMP"]
                                .dt.tz_convert(FUSO_DIA)
                                .dt.strftime("%Y-%m-%d")
                            )

                        else:

                            # As capturas tradicionais já utilizam horário local.
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
                                tempos_no_estado[chave_dia] = AcumuladorTempoNoEstado()
                                evolucoes[chave_dia] = AcumuladorEvolucao()
                                estados_recursos[chave_dia] = AcumuladorEstadoRecursos()
                                previsoes[chave_dia] = AcumuladorPrevisao()
                            acumulador = diarios[chave_dia]
                            acumulador_tempo = tempos_no_estado[chave_dia]
                            acumulador_evolucao = evolucoes[chave_dia]
                            acumulador_recursos = estados_recursos[chave_dia]
                            acumulador_previsao = previsoes[chave_dia]
                            lotes_atuais[chave_dia].append({
                                "lote_id": lote_id, "chave": destino, "origem": lote["origem"]
                            })

                            # Guarda os tokens dessa máquina pra consultar os limites na API
                            # só uma vez, depois que todos os arquivos já foram lidos.
                            if tipo == "metricas" and {"TOKEN_INSTALACAO", "TOKEN_PERFIL_SERVIDOR"} <= set(grupo.columns):
                                token_instalacao = grupo["TOKEN_INSTALACAO"].iloc[0]
                                token_perfil_servidor = grupo["TOKEN_PERFIL_SERVIDOR"].iloc[0]
                                if pd.notna(token_instalacao) and pd.notna(token_perfil_servidor):
                                    tokens_por_mac[mac] = (str(token_instalacao), str(token_perfil_servidor))

                            for registro in lote[campo]:
                                # O ID é chave primária. Se já apareceu em outro arquivo, o SQLite ignora
                                # rowcount vale 1 quando inseriu e vale 0 quando era uma repetição
                                consulta = vistos.execute(
                                    "INSERT OR IGNORE INTO vistos VALUES (?)",
                                    (registro["id_captura"],)
                                )
                                if consulta.rowcount == 1:
                                    if campo == "logs":
                                        vistos.execute(
                                            "INSERT INTO logs_diarios VALUES (?, ?, ?, ?, ?)",
                                            (mac, data, pd.Timestamp(registro["timestamp"]).value,
                                             registro["id_captura"],
                                             json.dumps(registro, ensure_ascii=False, allow_nan=False))
                                        )
                                    observacao = novo_documento(mac, data)
                                    observacao[campo] = [registro]
                                    resumo = resumir_documento(observacao)
                                    acumulador.adicionar(resumo, registro["timestamp"])

                                    # tempo no estado e evolucao real precisam das leituras cruas,
                                    # nao do resumo ja combinado la em cima, entao alimento os dois
                                    # acumuladores aqui mesmo, na mesma passada
                                    if campo == "metricas":
                                        instante = registro["timestamp"]

                                        def _alimentar_leitura(leitura):
                                            metrica = leitura.get("metrica")
                                            valor = leitura.get("valor")
                                            status = leitura.get("status")
                                            acumulador_tempo.adicionar(metrica, instante, valor, status)
                                            acumulador_evolucao.adicionar(metrica, instante, valor, status)
                                            acumulador_previsao.adicionar(metrica, instante, valor, status)

                                        para_cada_medida(registro, _alimentar_leitura)
                                        # estado por recurso (núcleo, volume, interface) e horário da captura, pra dashboard do Marcelo
                                        def _alimentar_recurso(leitura, categoria, recurso):
                                            acumulador_recursos.adicionar(
                                                categoria, recurso, leitura.get("metrica"), instante,
                                                leitura.get("valor"), leitura.get("status"),
                                                leitura.get("unidade"), leitura.get("tipo")
                                            )

                                        para_cada_medida_com_recurso(registro, _alimentar_recurso)
                                        acumulador_recursos.registrar_captura(instante)
                    except Exception as erro:
                        raise ValueError(f"Falha em {chave}: {erro}") from erro

            # Uma consulta de limites por token já serve pra todos os dias desse MAC.
            cache_limites = {}

            # O resumo diário fica separado dos lotes que o Java usa para gerar alertas
            for (mac, data), acumulador in sorted(diarios.items()):
                referencias = sorted(lotes_atuais[(mac, data)], key=chave_do_lote)
                resumos = acumulador.resultado()

                evolucao = None
                tokens = tokens_por_mac.get(mac)
                if tokens is not None:
                    if tokens not in cache_limites:
                        cache_limites[tokens] = buscar_limites_perfil(*tokens)
                    limites_por_metrica = cache_limites[tokens]

                    if limites_por_metrica is not None:
                        tempo_por_metrica = tempos_no_estado[(mac, data)].calcular(limites_por_metrica)
                        if tempo_por_metrica:
                            def _injetar_tempo_no_estado(leitura):
                                tempo = tempo_por_metrica.get(leitura.get("metrica"))
                                if tempo is not None:
                                    leitura["tempo_no_estado"] = tempo
                            para_cada_medida(resumos, _injetar_tempo_no_estado)

                        evolucao = evolucoes[(mac, data)].calcular(limites_por_metrica)

                        previsao_por_metrica = previsoes[(mac, data)].calcular(limites_por_metrica)
                        if previsao_por_metrica:
                            def _injetar_previsao(leitura):
                                previsao = previsao_por_metrica.get(leitura.get("metrica"))
                                if previsao is not None:
                                    leitura["previsao_limite"] = previsao
                            para_cada_medida(resumos, _injetar_previsao)

                # A severidade usa o que acabamos de calcular acima (tempo_por_metrica
                # ja injetado em "resumos"), mais a contagem de OOM que o resumo ja tinha
                # antes disso - nao precisa de nenhuma chamada nova a API.
                severidade = calcular_severidade(resumos)
                # Estado de cada recurso (núcleo, volume, interface...) com valor atual e desde quando.
                # Reaproveita a consulta de limites feita acima (cache_limites), sem nova chamada à API.
                limites_estado = None
                if tokens is not None:
                    limites_estado = cache_limites.get(tokens)
                estado_recursos = estados_recursos[(mac, data)].calcular(limites_estado)

                diario = {
                    "schema_version": 1, "tipo_documento": "resumo_diario",
                    "endereco_mac": mac, "data": data, "resumos": resumos,
                    # Mesmo formato dos lotes; as contagens continuam em resumos.logs.
                    "logs": [
                        json.loads(registro)
                        for (registro,) in vistos.execute(
                            "SELECT registro FROM logs_diarios WHERE mac = ? AND dia = ? "
                            "ORDER BY instante, id", (mac, data)
                        )
                    ]
                }
                # Omitimos a chave inteira (em vez de usar None) quando nao deu pra calcular -
                # mesma convencao do tempo_no_estado em cada metrica.
                if severidade is not None:
                    diario["severidade"] = severidade
                # evolucao vem vazia (lista []) quando nenhuma leitura bateu com algum
                # limite configurado - mesmo "nao tem o que calcular" do severidade, so
                # que aqui o "vazio" e uma lista, nao None
                if evolucao:
                    diario["evolucao"] = evolucao
                diario["lotes_atuais"] = referencias
                # Campos extras pro consumo da dashboard do Marcelo. O que já existia no resumo não muda.
                # Mesma convenção dos outros: omitimos a chave quando não deu pra calcular.
                diario["gerado_em"] = pd.Timestamp.now(tz="UTC").isoformat().replace("+00:00", "Z")
                # False quando a API de limites não respondeu: quem lê não deve tratar a falta de estado como "normal".
                diario["classificacao_disponivel"] = limites_estado is not None
                ultima_coleta = estados_recursos[(mac, data)].ultima_coleta_iso()
                if ultima_coleta is not None:
                    diario["ultima_coleta"] = ultima_coleta
                ultimo_oom = estados_recursos[(mac, data)].ultimo_oom_iso()
                if ultimo_oom is not None:
                    diario["ultimo_oom_em"] = ultimo_oom
                if estado_recursos:
                    diario["recursos_estado"] = estado_recursos
                    diario["piores_recursos"] = piores_recursos(estado_recursos)
                    diario["estado_por_categoria"] = estado_por_categoria(estado_recursos)
                destino = f"{GOLD}resumos/maquina={mac}/data={data}/resumo.json"
                preparar_envio(pasta, envios, destino, diario)

        # Só chegamos aqui se todos os documentos foram preparados sem erro
        # A pasta temporária é apagada automaticamente no final, inclusive se o envio falhar
        for destino, local in envios:
            with local.open("rb") as corpo:
                s3.put_object(Bucket=BUCKET, Key=destino, Body=corpo, ContentType="application/json")
            print("Arquivo JSON enviado para:", destino)
    print("Processamento Gold finalizado!")

def lambda_handler(event, context):
    print("=== Etapa 1: Processando camada Silver (raw/ -> trusted/) ===")
    processar_silver()

    print("=== Etapa 2: Processando camada Gold (trusted/ -> client/) ===")
    processar_gold()

    return {
        "statusCode": 200,
        "body": json.dumps("Pipeline Silver + Gold finalizado com sucesso!")
    }

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


    
