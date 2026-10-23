import docker
import psutil
import subprocess
import re
import time
import csv
import io
import os
from threading import Event
import boto3
from datetime import datetime


cliente = None
container = None

BUCKET = "bucket-coretrace"
PASTA_S3 = "raw"

s3 = None

# Guarda os contadores da coleta anterior para calcular o tráfego de rede
rede_anterior = None

COLUNAS = [
    "TIMESTAMP", "ENDERECO_MAC", "CONTAINER", "CPU_PERCENT", "RAM_PERCENT",
    "REDE_RECEBIDA_MB", "REDE_ENVIADA_MB",
    "DISCO_LEITURA_MB", "DISCO_ESCRITA_MB",
    "CAMADA_GRAVAVEL_PERCENT", "IMAGEM_PERCENT"
]


def obter_endereco_mac():
    # Usa o MAC da máquina, seguindo o padrão da captura do host.
    for enderecos in psutil.net_if_addrs().values():
        for endereco in enderecos:
            if endereco.family == psutil.AF_LINK:
                if endereco.address and endereco.address != "00:00:00:00:00:00":
                    return endereco.address.upper()

    return None


def enviar_s3(conteudo, nome_arquivo):
    chave = f"{PASTA_S3}/{nome_arquivo}"

    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=chave,
            Body=conteudo.encode("utf-8"),
            ContentType="text/csv; charset=utf-8"
        )
        print(f"Enviado: s3://{BUCKET}/{chave}")
        return True

    except Exception as erro:
        print(f"Erro no envio: {erro}")
        return False


def coletar_uso_disco():
    # Executa o comando Docker para obter o tamanho da camada gravável
    saida = subprocess.run(
        [
            "docker", "ps", "-s",
            "--filter", f"id={container.id}",
            "--format", "{{.Size}}"
        ],
        capture_output=True,
        text=True,
        check=True
    ).stdout.strip()

    resultado = re.search(r"^([\d.]+)([kMGT]?B)", saida)

    if not resultado:
        raise ValueError(f"Tamanho inválido: {saida}")

    valor, unidade = resultado.groups()

    fatores = {
        "B": 1, "KB": 1000, "MB": 1000**2,
        "GB": 1000**3, "TB": 1000**4
    }
    tamanho_gravavel = float(valor) * fatores[unidade.upper()]

    imagem = cliente.api.inspect_image(container.attrs["Image"])

    total = psutil.disk_usage(os.path.abspath(os.sep)).total

    # Calcula a proporção dos tamanhos em relação à capacidade do disco
    return {
        "CAMADA_GRAVAVEL_PERCENT": tamanho_gravavel / total * 100,
        "IMAGEM_PERCENT": imagem["Size"] / total * 100
    }


def coletar_metricas():
    global rede_anterior

    # Obtém as estatísticas atuais do container
    dados = container.stats(stream=False)

    # Calcula a utilização de CPU comparando os contadores atuais e anteriores
    cpu = dados["cpu_stats"]
    cpu_ant = dados["precpu_stats"]

    delta_cpu = (
        cpu["cpu_usage"]["total_usage"]
        - cpu_ant["cpu_usage"]["total_usage"]
    )
    delta_sistema = (
        cpu.get("system_cpu_usage", 0)
        - cpu_ant.get("system_cpu_usage", 0)
    )

    # Calcula a porcentagem de CPU considerando os núcleos disponíveis
    cpu_percent = (
        delta_cpu / delta_sistema
        * cpu.get("online_cpus", 1) * 100
        if delta_sistema > 0 and delta_cpu >= 0 else 0
    )

    # Calcula a RAM usada, descontando o cache inativo quando disponível
    memoria = dados["memory_stats"]
    usada = max(
        0,
        memoria.get("usage", 0)
        - memoria.get("stats", {}).get("inactive_file", 0)
    )
    limite = memoria.get("limit", 0)

    # Calcula a RAM como porcentagem do limite informado pelo Docker
    ram_percent = usada / limite * 100 if limite > 0 else 0

    # Soma os bytes recebidos e enviados por todas as interfaces do container
    redes = dados.get("networks") or {}
    recebidos = sum(r.get("rx_bytes", 0) for r in redes.values())
    enviados = sum(r.get("tx_bytes", 0) for r in redes.values())

    # Calcula quanto tráfego ocorreu desde a última coleta
    if rede_anterior is None:
        # Na primeira coleta não há valores anteriores para comparar
        delta_recebidos = delta_enviados = 0
    else:
        delta_recebidos = recebidos - rede_anterior["recebidos"]
        delta_enviados = enviados - rede_anterior["enviados"]

        # Se um contador reiniciar, utiliza o valor atual como referência
        if delta_recebidos < 0:
            delta_recebidos = recebidos
        if delta_enviados < 0:
            delta_enviados = enviados

    # Atualiza a referência para a próxima coleta
    rede_anterior = {
        "recebidos": recebidos,
        "enviados": enviados
    }

    # Soma os bytes de leitura e escrita informados pelo Docker
    leitura = escrita = 0
    operacoes = dados.get("blkio_stats", {}).get(
        "io_service_bytes_recursive"
    ) or []

    for op in operacoes:
        tipo = op.get("op", "").lower()

        if tipo == "read":
            leitura += op.get("value", 0)
        elif tipo == "write":
            escrita += op.get("value", 0)

    # Obtém as métricas de armazenamento
    disco = coletar_uso_disco()

    # Retorna todas as métricas em um dicionário para o CSV e o terminal
    return {
        "TIMESTAMP": datetime.now().isoformat(),
        "ENDERECO_MAC": obter_endereco_mac(),
        "CONTAINER": container.name,
        "CPU_PERCENT": round(cpu_percent, 2),
        "RAM_PERCENT": round(ram_percent, 2),
        "REDE_RECEBIDA_MB": round(delta_recebidos / 1024**2, 6),
        "REDE_ENVIADA_MB": round(delta_enviados / 1024**2, 6),
        "DISCO_LEITURA_MB": round(leitura / 1024**2, 2),
        "DISCO_ESCRITA_MB": round(escrita / 1024**2, 2),
        "CAMADA_GRAVAVEL_PERCENT": disco["CAMADA_GRAVAVEL_PERCENT"],
        "IMAGEM_PERCENT": disco["IMAGEM_PERCENT"]
    }


def criar_nome_arquivo():
    endereco_mac = obter_endereco_mac()
    if endereco_mac is None:
        raise ValueError("Não foi possível identificar o endereço MAC da máquina.")

    mac_arquivo = endereco_mac.replace(":", "-").upper()
    horario = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    return f"docker_{mac_arquivo}_{horario}.csv"


def main(parada=None):
    global cliente, container, s3, rede_anterior

    if parada is None:
        parada = Event()

    cliente = docker.from_env()
    for container in cliente.containers.list():
        s3 = boto3.client("s3")
        rede_anterior = None

        # Cria o CSV na memória e escreve o cabeçalho
        # O StringIO permite manipular texto como se fosse um arquivo, sem salvá-lo no disco
        buffer = io.StringIO()
        escritor = csv.DictWriter(buffer, fieldnames=COLUNAS)
        escritor.writeheader()

        # Controla a quantidade de registros pendentes e o momento do próximo envio
        quantidade_registros = 0
        ultimo_envio = time.monotonic()
        nome_arquivo = criar_nome_arquivo()


        try:
            while not parada.is_set():
                try:
                    metricas = coletar_metricas()
                    linha = metricas.copy()

                    # Evita notação científica nas porcentagens do CSV
                    linha["CAMADA_GRAVAVEL_PERCENT"] = (
                        f'{metricas["CAMADA_GRAVAVEL_PERCENT"]:.10f}'
                    )
                    linha["IMAGEM_PERCENT"] = (
                        f'{metricas["IMAGEM_PERCENT"]:.8f}'
                    )

                    # Adiciona uma linha ao CSV em memória
                    escritor.writerow(linha)
                    quantidade_registros += 1

                    print(
                        f'{metricas["TIMESTAMP"]} - '
                        f'Captura realizada para {container.name}'
                    )

                    for chave, valor in metricas.items():
                        if chave == "CAMADA_GRAVAVEL_PERCENT":
                            print(f"{chave}: {valor:.10f}%")
                        elif chave == "IMAGEM_PERCENT":
                            print(f"{chave}: {valor:.8f}%")
                        else:
                            print(f"{chave}: {valor}")

                except Exception as erro:
                    print(f"Erro na coleta: {erro}")

                if time.monotonic() - ultimo_envio >= 60:
                    if quantidade_registros and enviar_s3(
                        buffer.getvalue(), nome_arquivo
                    ):
                        buffer.seek(0)
                        buffer.truncate(0)
                        escritor.writeheader()

                        quantidade_registros = 0
                        nome_arquivo = criar_nome_arquivo()
                        print("Buffer limpo; iniciando novo arquivo.")

                    ultimo_envio = time.monotonic()

                parada.wait(1)

        except KeyboardInterrupt:
            print("\nMonitoramento encerrado.")

        finally:
            if quantidade_registros:
                if not enviar_s3(buffer.getvalue(), nome_arquivo):
                    print("Envio final falhou; os dados restantes não foram enviados.")

            buffer.close()


if __name__ == "__main__":
    main()
