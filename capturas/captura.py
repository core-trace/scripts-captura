import psutil as p
import csv
import time
import os
import json
import requests
import io
from datetime import datetime
from pathlib import Path
from processos.processos import coletar_processos, COLUNAS_PROCESSOS


# A coleta de cada categoria tá no arquivo próprio
from utils.coleta import (
    numero_valido, salvar_leitura, nova_linha_metricas, diferenca_contador,
)
from cpu.coleta import (
    METRICAS_SELECIONAVEIS,
    INVENTARIO_CPU,
    IDS_CPU,
    MODOS_CPU,
    TAXAS_CPU,
    preparar_coleta_cpu,
    colunas_cpu,
    coletar_cpu,
    coletar_percentuais,
    coletar_modos,
    coletar_taxas,
    coletar_frequencias,
    coletar_inventario_fisico,
)
from ram.coleta import (
    IDS_RAM,
    ATRIBUTOS_RAM,
    ler_oom_linux,
    preparar_coleta_ram,
    colunas_ram,
    coletar_ram,
    coletar_oom,
)
from disco.coleta import (
    METRICAS_VOLUME,
    METRICAS_IO,
    IDS_DISCO,
    CAMPOS_INODES,
    ler_fila_linux,
    preparar_coleta_disco,
    campos_volumes,
    colunas_volumes,
    campos_io,
    colunas_io,
    descobrir_alvos_disco,
    coluna_disco,
    colunas_disco,
    coletar_disco_geral,
    marcar_campos_disco,
    coletar_inodes,
    coletar_volumes,
    salvar_taxa_disco,
    salvar_tempo_disco,
    coletar_io_disco,
)
from swap.coleta import (
    IDS_SWAP,
    preparar_coleta_swap,
    colunas_swap,
    coletar_swap,
    coletar_atividade_swap,
)
from carga.coleta import (
    IDS_CARGA,
    preparar_coleta_carga,
    colunas_carga,
    coletar_carga,
)
from rede.coleta import (
    GRUPOS_REDE,
    ATRIBUTOS_REDE,
    IDS_REDE,
    descobrir_interfaces_rede,
    preparar_coleta_rede,
    campos_rede,
    colunas_interface_rede,
    coluna_rede,
    colunas_rede,
    coletar_rede_geral,
    salvar_taxa_rede,
    coletar_rede,
)

API_BASE_URL = "http://localhost:3333"

ARQUIVO_CONFIG = Path(__file__).resolve().parent.parent / "config.json"

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
            f"{API_BASE_URL}/empresa/metricas",
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
    endereco_mac,
    estado_cpu,
    estado_ram,
    estado_swap=None,
    estado_carga=None
):
    cache = {}

    dados: dict[str, str | int | float | None] = {
        "TIMESTAMP": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        "ENDERECO_MAC": endereco_mac
    }

    for metrica in metricas:
        coluna = metrica["nome_coluna"]
        if coluna in IDS_CPU or coluna in IDS_RAM or coluna in IDS_DISCO or coluna in IDS_SWAP or coluna in IDS_CARGA or coluna in IDS_REDE:
            continue

        dados[coluna] = capturar_metrica(
            metrica,
            cache
        )

    dados.update(coletar_cpu(estado_cpu))
    dados.update(coletar_ram(estado_ram))
    if estado_swap is not None:
        dados.update(coletar_swap(estado_swap))
    if estado_carga is not None:
        dados.update(coletar_carga(estado_carga))
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


    # Preparamos a memória de CPU e RAM uma vez só
    escolhas_metricas = []
    for metrica in metricas:
        escolhas_metricas.append(metrica["nome_coluna"])
    estado_cpu = preparar_coleta_cpu(escolhas_metricas)
    estado_ram = preparar_coleta_ram(escolhas_metricas)
    estado_disco = preparar_coleta_disco(escolhas_metricas)
    estado_swap = preparar_coleta_swap(escolhas_metricas)
    estado_carga = preparar_coleta_carga(escolhas_metricas)
    estado_rede = preparar_coleta_rede(escolhas_metricas)

    colunas = [
        "TIMESTAMP",
        "ENDERECO_MAC"
    ]

    colunas.extend(
        metrica["nome_coluna"]
        for metrica in metricas
        if metrica["nome_coluna"] not in IDS_CPU
        and metrica["nome_coluna"] not in IDS_RAM
        and metrica["nome_coluna"] not in IDS_DISCO
        and metrica["nome_coluna"] not in IDS_SWAP
        and metrica["nome_coluna"] not in IDS_CARGA
        and metrica["nome_coluna"] not in IDS_REDE
    )
    colunas.extend(colunas_cpu(estado_cpu))
    colunas.extend(colunas_ram(estado_ram))
    colunas.extend(colunas_swap(estado_swap))
    colunas.extend(colunas_carga(estado_carga))
    colunas.extend(colunas_rede(estado_rede))
    colunas.extend(colunas_disco(estado_disco))

    mac_arquivo = endereco_mac.replace(":", "-").upper()
    timestamp_arquivo = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")

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
            dados = capturar_dados(metricas, endereco_mac, estado_cpu, estado_ram, estado_swap, estado_carga)

            dados.update(coletar_rede_geral(estado_rede))
            dados.update(coletar_disco_geral(estado_disco))
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
                        "%Y-%m-%d_%H-%M-%S-%f"
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
