import psutil as p
import csv
import math
import time
import os
import json
import requests
import io
from datetime import datetime
from pathlib import Path
from rede.rede import coletar_rede, COLUNAS_REDE
from processos.processos import coletar_processos, COLUNAS_PROCESSOS


API_BASE_URL = "http://localhost:3333"
API_FILTRO_URL = "http://localhost:3334"

ARQUIVO_CONFIG = Path(__file__).with_name("config.json")

BUCKET = "bucket-coretrace"
PASTA_S3 = "raw"

s3 = None

def enviar_s3(conteudo, nome_arquivo):
    global s3
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    chave_s3 = f"{PASTA_S3}/{nome_arquivo}"

    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=chave_s3,
            Body=conteudo.encode("utf-8"),
            ContentType="text/csv; charset=utf-8"
        )

        print(f"Arquivo enviado para s3://{BUCKET}/{chave_s3}")
        return True

    except Exception as erro:
        print(f"Erro ao enviar {nome_arquivo}: {erro}")
        return False

def obter_endereco_mac():
    interfaces = p.net_if_addrs()

    for nome_interface, enderecos in interfaces.items():
        for endereco in enderecos:
            if endereco.family == p.AF_LINK:
                if endereco.address and endereco.address != "00:00:00:00:00:00":
                    return endereco.address.upper()

    return None


def carregar_config():
    if not os.path.exists(ARQUIVO_CONFIG):
        print(f"Arquivo de configuração '{ARQUIVO_CONFIG}' não encontrado.")
        exit()

    try:
        with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)

    except json.JSONDecodeError:
        print("O arquivo config.json possui um JSON inválido.")
        exit()


def ativar_agente(
    token_instalacao,
    token_perfil_servidor,
    endereco_mac
):
    dados = {
        "tokenInstalacaoServer": token_instalacao,
        "tokenPerfilServidorServer": token_perfil_servidor,
        "enderecoMacServer": endereco_mac
    }

    try:
        resposta = requests.post(
            f"{API_BASE_URL}/servidor/ativarAgente",
            json=dados
        )

        if resposta.status_code == 200:
            print(f"Agente ativado no MAC {endereco_mac}")
            return True

        print(f"Erro ao ativar agente ({resposta.status_code})")
        print(resposta.text)

        return False

    except requests.RequestException as erro:
        print(f"Falha de conexão com a API de ativação: {erro}")
        return False


def buscar_metricas_cliente(
    token_instalacao,
    token_perfil_servidor
):
    try:
        resposta = requests.get(
            f"{API_FILTRO_URL}/empresa/metricas",
            headers={
                "Authorization": f"Bearer {token_instalacao}",
                "token-perfil": token_perfil_servidor
            }
        )

        if resposta.status_code == 200:
            metricas = resposta.json()

            print(f"{len(metricas)} métricas carregadas.\n")

            return metricas

        print(
            f"Houve um erro ao consultar as métricas "
            f"({resposta.status_code})."
        )

        print(resposta.text)

        return []

    except requests.RequestException as erro:
        print(f"Falha de conexão com a API de métricas: {erro}")
        return []


def converter_valor(valor):
    if valor is None:
        return None

    if not isinstance(valor, str):
        return valor

    valor = valor.strip()

    if valor.lower() == "true":
        return True

    if valor.lower() == "false":
        return False

    if valor.lower() == "none":
        return None

    try:
        return int(valor)

    except ValueError:
        pass

    try:
        return float(valor)

    except ValueError:
        pass

    return valor


def montar_argumentos(metrica):
    nome = metrica.get("argumento_nome")

    valor = converter_valor(
        metrica.get("argumento_valor")
    )

    if nome is None:
        return {}

    return {
        nome: valor
    }


def executar_funcao(metrica, cache):
    nome_funcao = metrica["funcao_psutil"]

    argumentos = montar_argumentos(metrica)

    chave_cache = (
        nome_funcao,
        tuple(sorted(argumentos.items()))
    )

    if chave_cache not in cache:
        funcao = getattr(
            p,
            nome_funcao
        )

        cache[chave_cache] = funcao(
            **argumentos
        )

    return cache[chave_cache]


def extrair_retorno(
    resultado,
    metrica
):
    atributo = metrica.get(
        "atributo_retorno"
    )

    indice = metrica.get(
        "indice_retorno"
    )

    if atributo is not None:
        resultado = getattr(
            resultado,
            atributo
        )

    if indice is not None:
        resultado = resultado[indice]

    return resultado


def capturar_metrica(
    metrica,
    cache
):
    try:
        resultado = executar_funcao(
            metrica,
            cache
        )

        resultado = extrair_retorno(
            resultado,
            metrica
        )

        return resultado

    except (
        AttributeError,
        IndexError,
        TypeError,
        OSError
    ) as erro:

        print(
            f"Não foi possível capturar "
            f"{metrica['nome_coluna']}: "
            f"{erro}"
        )

        return None


# Identificadores que este py sabe coletar
METRICAS_SELECIONAVEIS = [
    "cpu_percent", "cpu_percent_percpu", "cpu_user", "cpu_system", "cpu_idle",
    "cpu_iowait", "cpu_interrupts_per_second", "cpu_context_switches_per_second",
    "cpu_freq_current",
]

INVENTARIO_CPU = ["cpu_count_logical", "cpu_count_physical", "cpu_freq_min", "cpu_freq_max"]

IDS_CPU = METRICAS_SELECIONAVEIS + INVENTARIO_CPU

MODOS_CPU = {
    "cpu_user": "user", "cpu_system": "system",
    "cpu_idle": "idle", "cpu_iowait": "iowait",
}

TAXAS_CPU = {
    "cpu_interrupts_per_second": "interrupts",
    "cpu_context_switches_per_second": "ctx_switches",
}

#Prepara as escolhas
def preparar_coleta_cpu(selecionadas, fonte=p, relogio=time.monotonic):
    #chama uma vez só
    quantidade = fonte.cpu_count(logical=True)
    if not isinstance(quantidade, int) or quantidade < 1:
        raise RuntimeError("Não foi possível identificar as CPUs lógicas para montar o CSV.")

    escolhas = []
    for nome in selecionadas:
        if nome in IDS_CPU and nome not in escolhas:
            escolhas.append(nome)

    # Dependências obg do coletor
    for nome in ["cpu_percent", "cpu_percent_percpu"]:
        if nome not in escolhas:
            escolhas.append(nome)

    colunas_por_cpu = []
    for indice in range(quantidade):
        colunas_por_cpu.append(f"cpu_logical_{indice}_percent")

    return {
        "selecionadas": escolhas,
        "quantidade": quantidade,
        "colunas_por_cpu": colunas_por_cpu,
        "aquecidas": [],
        "contadores_anteriores": None,
        "horario_anterior": None,
        "fonte": fonte,
        "relogio": relogio,
    }



# Monta o cabeçalho CPU do CSV
def colunas_cpu(estado):
    # Uma opção "por CPU lógica" pode gerar várias colunas no CSV
    valores = []
    for identificador in METRICAS_SELECIONAVEIS:
        if identificador in estado["selecionadas"] and identificador != "cpu_percent_percpu":
            valores.append(identificador)

    valores.extend(estado["colunas_por_cpu"])
    valores.append("cpu_count_logical")
    for identificador in INVENTARIO_CPU:
        if identificador in estado["selecionadas"] and identificador != "cpu_count_logical":
            valores.append(identificador)

    colunas = valores.copy()
    for nome in valores:
        colunas.append(f"{nome}_status")
    colunas.extend(["cpu_interval_seconds", "cpu_schema_version"])
    return colunas


# Verifica valores se da par usar
def numero_valido(valor):
    # bool é considerado inteiro mas não é uma leitura CPU
    if isinstance(valor, bool):
        return False
    if not isinstance(valor, (int, float)):
        return False
    return math.isfinite(valor)


# Organiza uma rodada de captura
def coletar_cpu(estado):
    dados = {}
    for coluna in colunas_cpu(estado):
        dados[coluna] = None
    dados["cpu_schema_version"] = "cpu-1"
    agora_rodada = estado["relogio"]()
    if estado["horario_anterior"] is not None:
        intervalo_rodada = agora_rodada - estado["horario_anterior"]
        if intervalo_rodada > 0:
            dados["cpu_interval_seconds"] = intervalo_rodada
    estado["horario_anterior"] = agora_rodada

    try:
        salvar_leitura(dados, "cpu_count_logical", estado["fonte"].cpu_count(logical=True))
    except (OSError, NotImplementedError):
        salvar_leitura(dados, "cpu_count_logical", None, "erro_fonte")
    if dados["cpu_count_logical"] != estado["quantidade"]:
        for coluna in colunas_cpu(estado):
            if coluna.endswith("_status") and coluna != "cpu_count_logical_status":
                dados[coluna] = "topologia_alterada"
        estado["aquecidas"].clear()
        estado["contadores_anteriores"] = None
        return dados

    # Cada etapa consulta uma fonte. Não precisa esperar por metrica
    coletar_percentuais(estado, dados, por_cpu=False)
    coletar_percentuais(estado, dados, por_cpu=True)
    coletar_modos(estado, dados)
    coletar_taxas(estado, dados)
    coletar_frequencias(estado, dados)
    coletar_inventario_fisico(estado, dados)
    return dados


# Guarda o valor e status
# Falha fica vazia, não vira zero
def salvar_leitura(dados, coluna, valor, status="ok"):
    # Campo vazio significa leitura inválida
    # zero continua sendo um valor válido
    if status == "ok" and (not numero_valido(valor) or valor < 0):
        valor = None
        status = "indisponivel"
    if status == "ok":
        dados[coluna] = valor
    else:
        dados[coluna] = None
    dados[f"{coluna}_status"] = status


# Lê o uso total ou cada CPU lógica e descarta a primeira leitura
def coletar_percentuais(estado, dados, por_cpu):
    if por_cpu:
        grupo = "por_cpu"
        colunas = estado["colunas_por_cpu"]
    else:
        grupo = "total"
        colunas = ["cpu_percent"]

    try:
        if por_cpu:
            valores = estado["fonte"].cpu_percent(interval=None, percpu=True)
        else:
            valores = [estado["fonte"].cpu_percent(interval=None)]
        if len(valores) != len(colunas):
            raise ValueError("Quantidade de CPUs mudou durante a leitura")

        # A primeira consulta apenas cria a referência do psutil
        pronta = grupo in estado["aquecidas"]
        for indice in range(len(colunas)):
            valor = valores[indice]
            if not pronta:
                status = "aquecendo"
            elif not numero_valido(valor) or not 0 <= valor <= 100:
                status = "indisponivel"
            else:
                status = "ok"
            salvar_leitura(dados, colunas[indice], valor, status)
        if grupo not in estado["aquecidas"]:
            estado["aquecidas"].append(grupo)
    except (OSError, ValueError, AttributeError, NotImplementedError):
        for coluna in colunas:
            salvar_leitura(dados, coluna, None, "erro_fonte")
        if grupo in estado["aquecidas"]:
            estado["aquecidas"].remove(grupo)


# Uma única consulta dá usuário, sistema, ociosa e espera por I/O
def coletar_modos(estado, dados):
    modos = []
    for nome in MODOS_CPU:
        if nome in estado["selecionadas"]:
            modos.append(nome)
    if modos:
        try:
            tempos = estado["fonte"].cpu_times_percent(interval=None)
            pronta = "modos" in estado["aquecidas"]
            for nome in modos:
                valor = getattr(tempos, MODOS_CPU[nome], None)
                if valor is None:
                    status = "indisponivel"
                elif not pronta:
                    status = "aquecendo"
                else:
                    status = "ok"
                if pronta and valor is not None and (not numero_valido(valor) or not 0 <= valor <= 100):
                    status = "indisponivel"
                salvar_leitura(dados, nome, valor, status)
            if "modos" not in estado["aquecidas"]:
                estado["aquecidas"].append("modos")
        except (OSError, ValueError, AttributeError, NotImplementedError):
            for nome in modos:
                salvar_leitura(dados, nome, None, "erro_fonte")
            if "modos" in estado["aquecidas"]:
                estado["aquecidas"].remove("modos")


# Compara contadores de duas rodadas e divide os novos eventos pelo tempo
def coletar_taxas(estado, dados):
    taxas = []
    for nome in TAXAS_CPU:
        if nome in estado["selecionadas"]:
            taxas.append(nome)
    if taxas:
        try:
            stats = estado["fonte"].cpu_stats()
            agora = estado["relogio"]()
            anterior = estado["contadores_anteriores"]
            intervalo = None
            if anterior is not None:
                intervalo = agora - anterior["horario"]
            for nome in taxas:
                valor = getattr(stats, TAXAS_CPU[nome], None)
                antes = None
                if anterior is not None:
                    antes = getattr(anterior["contadores"], TAXAS_CPU[nome], None)
                if valor is None or not numero_valido(valor):
                    salvar_leitura(dados, nome, None, "indisponivel")
                elif anterior is None or antes is None or not numero_valido(antes):
                    salvar_leitura(dados, nome, None, "aquecendo")
                # Primeiro verifica se existe um intervalo depois compara com zero.
                elif intervalo is None or intervalo <= 0:
                    salvar_leitura(dados, nome, None, "intervalo_invalido")
                elif valor < antes:
                    salvar_leitura(dados, nome, None, "contador_reiniciado")
                else:
                    # Ex: 100 novos eventos em 2 s = 50 eventos/s
                    novos_eventos = valor - antes
                    eventos_por_segundo = novos_eventos / intervalo
                    salvar_leitura(dados, nome, eventos_por_segundo)
            estado["contadores_anteriores"] = {"horario": agora, "contadores": stats}
        except (OSError, ValueError, AttributeError, NotImplementedError):
            for nome in taxas:
                salvar_leitura(dados, nome, None, "erro_fonte")
            estado["contadores_anteriores"] = None


# Lê as frequências escolhidas e trata fonte indisponível
def coletar_frequencias(estado, dados):
    atributos = {
        "cpu_freq_current": "current",
        "cpu_freq_min": "min",
        "cpu_freq_max": "max",
    }
    frequencias = []
    for nome in atributos:
        if nome in estado["selecionadas"]:
            frequencias.append(nome)
    if frequencias:
        try:
            freq = estado["fonte"].cpu_freq()
            for coluna in frequencias:
                valor = getattr(freq, atributos[coluna], None)
                if valor is not None and numero_valido(valor) and valor > 0:
                    salvar_leitura(dados, coluna, valor)
                else:
                    salvar_leitura(dados, coluna, None, "indisponivel")
        except (OSError, ValueError, AttributeError, NotImplementedError):
            for coluna in frequencias:
                salvar_leitura(dados, coluna, None, "erro_fonte")

# Preserva a quantidade de núcleos físicos
def coletar_inventario_fisico(estado, dados):
    if "cpu_count_physical" in estado["selecionadas"]:
        try:
            salvar_leitura(dados, "cpu_count_physical", estado["fonte"].cpu_count(logical=False))
        except (OSError, NotImplementedError):
            salvar_leitura(dados, "cpu_count_physical", None, "erro_fonte")


# CPU métricas formam uma linha do CSV
def capturar_dados(
    metricas,
    endereco_mac,
    estado_cpu
):
    cache = {}

    # A linha mistura horário/MAC (texto), medições (números) e campos vazios.
    dados: dict[str, str | int | float | None] = {
        "TIMESTAMP": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        "ENDERECO_MAC": endereco_mac
    }

    for metrica in metricas:
        coluna = metrica["nome_coluna"]
        if coluna in IDS_CPU:
            continue

        dados[coluna] = capturar_metrica(
            metrica,
            cache
        )

    dados.update(coletar_cpu(estado_cpu))
    return dados


def main():
    config = carregar_config()


    token_instalacao = config.get(
        "token_instalacao"
    )

    token_perfil_servidor = config.get(
        "token_perfil_servidor"
    )


    if token_instalacao is None:
        print(
            "Token de instalação não encontrado "
            "no arquivo de configuração."
        )

        exit()


    if token_perfil_servidor is None:
        print(
            "Token do perfil do servidor não encontrado "
            "no arquivo de configuração."
        )

        exit()


    endereco_mac = obter_endereco_mac()

    if endereco_mac is None:
        print(
            "Não foi possível identificar "
            "o endereço MAC da máquina."
        )

        exit()


    agente_ativado = ativar_agente(
        token_instalacao,
        token_perfil_servidor,
        endereco_mac
    )

    if not agente_ativado:
        print("Não foi possível ativar o agente.")
        exit()


    metricas = buscar_metricas_cliente(
        token_instalacao,
        token_perfil_servidor
    )

    if not metricas:
        print(
            "Nenhuma métrica configurada "
            "para este perfil."
        )

        exit()


    # Preparamos a memória CPU uma vez, antes de começar as rodadas de captura
    escolhas_cpu = []
    for metrica in metricas:
        escolhas_cpu.append(metrica["nome_coluna"])
    estado_cpu = preparar_coleta_cpu(escolhas_cpu)

    colunas = [
        "TIMESTAMP",
        "ENDERECO_MAC"
    ]

    colunas.extend(
        metrica["nome_coluna"]
        for metrica in metricas if metrica["nome_coluna"] not in IDS_CPU
    )
    colunas.extend(colunas_cpu(estado_cpu))

    colunas.extend(COLUNAS_REDE)



    mac_arquivo = endereco_mac.replace(":", "-")
    timestamp_arquivo = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    nome_arquivo = f"coretrace_coleta_{mac_arquivo}_{timestamp_arquivo}.csv"
    nome_processos = f"processos_{mac_arquivo}_{timestamp_arquivo}.csv"

    # CSVs mantidos apenas na memória
    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=colunas)
    escritor.writeheader()

    buffer_processos = io.StringIO()
    escritor_processos = csv.DictWriter(
        buffer_processos,
        fieldnames=COLUNAS_PROCESSOS
    )
    escritor_processos.writeheader()

    ultimo_envio = time.monotonic()

    try:
        while True:
            dados = capturar_dados(metricas, endereco_mac, estado_cpu)
            dados.update(coletar_rede())

            escritor.writerow(dados)

            escritor_processos.writerows(
                coletar_processos(
                    dados["TIMESTAMP"],
                    endereco_mac
                )
            )

            print(
                f"{dados['TIMESTAMP']} - "
                f"Captura realizada para o MAC {endereco_mac}"
            )

            if time.monotonic() - ultimo_envio >= 60:
                envio_coleta = enviar_s3(
                    buffer.getvalue(),
                    nome_arquivo
                )

                envio_processos = enviar_s3(
                    buffer_processos.getvalue(),
                    nome_processos
                )

                if envio_coleta and envio_processos:
                    # Limpa os buffers e prepara novos CSVs
                    buffer.seek(0)
                    buffer.truncate(0)
                    escritor.writeheader()

                    buffer_processos.seek(0)
                    buffer_processos.truncate(0)
                    escritor_processos.writeheader()

                    # Gera novos nomes para o próximo intervalo
                    timestamp_arquivo = datetime.now().strftime(
                        "%Y-%m-%d_%H-%M-%S"
                    )

                    nome_arquivo = (
                        f"coretrace_coleta_{mac_arquivo}_{timestamp_arquivo}.csv"
                    )

                    nome_processos = (
                        f"processos_{mac_arquivo}_{timestamp_arquivo}.csv"
                    )

                    ultimo_envio = time.monotonic()

                else:
                    print("Envio incompleto. Os dados foram mantidos na memória.")
                    ultimo_envio = time.monotonic()

            time.sleep(1)

    except KeyboardInterrupt:
        print("\nMonitoramento encerrado.")

    finally:
        # Envia os dados restantes ao encerrar o programa
        if buffer.tell() > 0:
            enviar_s3(buffer.getvalue(), nome_arquivo)

        if buffer_processos.tell() > 0:
            enviar_s3(buffer_processos.getvalue(), nome_processos)


if __name__ == "__main__":
    main()