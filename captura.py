import psutil as p
import csv
import time
import os
import json
import requests
import boto3
import io
from datetime import datetime
from rede.rede import coletar_rede, COLUNAS_REDE
from processos.processos import coletar_processos, COLUNAS_PROCESSOS


API_BASE_URL = "http://localhost:3333"
API_FILTRO_URL = "http://localhost:3334"

ARQUIVO_CONFIG = "config.json"

BUCKET = "bucket-coretrace"
PASTA_S3 = "raw"

s3 = boto3.client("s3")

def enviar_s3(conteudo, nome_arquivo):
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


def capturar_dados(
    metricas,
    endereco_mac
):
    cache = {}

    dados = {
        "TIMESTAMP": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        "ENDERECO_MAC": endereco_mac
    }

    for metrica in metricas:
        coluna = metrica["nome_coluna"]

        dados[coluna] = capturar_metrica(
            metrica,
            cache
        )

    return dados


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


colunas = [
    "TIMESTAMP",
    "ENDERECO_MAC"
]

colunas.extend(
    metrica["nome_coluna"]
    for metrica in metricas
)

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
        dados = capturar_dados(metricas, endereco_mac)
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
