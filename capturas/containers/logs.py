import csv
import json
import re
import hashlib
import uuid
import docker

from datetime import datetime, timezone
from pathlib import Path

# Reutiliza a identificação da máquina utilizada em metricas.py.
# Os dois arquivos precisam estar na mesma pasta.
from .metricas import obter_endereco_mac


COLUNAS_LOGS = [
    "TIMESTAMP",
    "ENDERECO_MAC",
    "CONTAINER_ID",
    "CONTAINER",
    "LOG_LEVEL",
    "LOG_MENSAGEM"
]


# Utilizamos caminhos relativos ao próprio script.
# Assim, os arquivos serão gerados no mesmo lugar,
# independentemente de onde o Python for executado.
DIRETORIO_BASE = Path(__file__).resolve().parent
DIRETORIO_CSV = DIRETORIO_BASE / "csv"
CAMINHO_ESTADO = DIRETORIO_BASE / "estado" / "estado_logs.json"

cliente = docker.from_env()


# Expressão para separar o timestamp Docker da mensagem.
# O Docker normalmente utiliza RFC3339Nano em UTC,
# como 2026-11-08T23:40:00.123456789Z.
PADRAO_TIMESTAMP = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}"
    r"T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z)"
    r"\s(?P<mensagem>.*)$"
)


def listar_containers():
    # Inclui containers em execução e parados.
    return cliente.containers.list(all=True)


def identificar_nivel_log(mensagem):
    # Primeiro verificamos se a mensagem é um JSON.
    # Aplicações com logs estruturados podem informar
    # diretamente o nível nos campos level ou severity.
    try:
        conteudo = json.loads(mensagem)

        if isinstance(conteudo, dict):
            nivel = (
                conteudo.get("level")
                or conteudo.get("severity")
                or conteudo.get("severityText")
            )

            if isinstance(nivel, str):
                nivel = nivel.upper()

                if nivel == "WARNING":
                    return "WARN"

                if nivel in {
                    "TRACE", "DEBUG", "INFO", "WARN",
                    "ERROR", "FATAL", "CRITICAL"
                }:
                    return nivel

    except (json.JSONDecodeError, TypeError):
        pass

    # Também reconhece mensagens de texto convencionais:
    # INFO Aplicação iniciada
    # [WARN] Tempo elevado
    # 2026-11-08 20:30:00 ERROR Falha de conexão
    padrao = (
        r"^\s*"
        r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\s+)?"
        r"[\[(]?"
        r"(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)"
        r"\b"
    )

    resultado = re.search(padrao, mensagem, re.IGNORECASE)

    if resultado:
        nivel = resultado.group(1).upper()

        if nivel == "WARNING":
            return "WARN"

        return nivel

    # Nunca inventamos o nível de uma mensagem.
    return "UNKNOWN"


def timestamp_para_ns(timestamp):
    # Converte o timestamp UTC do Docker para nanossegundos.
    #
    # Python datetime possui precisão de microssegundos,
    # enquanto o Docker pode fornecer nanossegundos.
    #
    # Por isso, tratamos a parte fracionária separadamente,
    # preservando a precisão para comparar os logs.
    data = datetime.strptime(
        timestamp[:19],
        "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    segundos = int(data.timestamp())

    fracao = ""

    if len(timestamp) > 20 and timestamp[19] == ".":
        fracao = timestamp[20:-1]

    nanossegundos = int(fracao.ljust(9, "0")) if fracao else 0

    return segundos * 1_000_000_000 + nanossegundos


def carregar_estado():
    # Na primeira execução ainda não existe cursor.
    if not CAMINHO_ESTADO.exists():
        return {}

    # O arquivo guarda a última posição processada
    # de cada container, identificada pelo seu ID.
    with open(CAMINHO_ESTADO, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def salvar_estado(estado):
    CAMINHO_ESTADO.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # Gravamos em um arquivo temporário antes de substituir
    # o anterior, reduzindo o risco de um JSON incompleto.
    temporario = CAMINHO_ESTADO.with_suffix(".tmp")

    with open(temporario, "w", encoding="utf-8") as arquivo:
        json.dump(
            estado,
            arquivo,
            indent=4,
            ensure_ascii=False
        )

    temporario.replace(CAMINHO_ESTADO)


def capturar_logs_container(
    container,
    endereco_mac,
    cursor=None,
    limite_inicial=100
):
    # Recupera o último timestamp processado.
    cursor = cursor or {}

    ultimo_timestamp = cursor.get("timestamp")
    contagens_anteriores = cursor.get("contagens") or {}

    ultimo_ns = (
        timestamp_para_ns(ultimo_timestamp)
        if ultimo_timestamp
        else None
    )

    # Na primeira coleta buscamos apenas os últimos
    # 100 registros de cada container.
    parametros = {
        "timestamps": True,
        "stream": False,
        "stdout": True,
        "stderr": True,
        "tail": limite_inicial
    }

    if ultimo_timestamp:
        # Nas próximas coletas, buscamos desde um segundo
        # antes do último timestamp processado.
        #
        # Fazemos isso porque o filtro "since" utiliza
        # precisão de segundos. A margem evita perder
        # mensagens na fronteira entre duas coletas.
        segundos = ultimo_ns // 1_000_000_000

        parametros["since"] = max(0, segundos - 1)
        parametros["tail"] = "all"

    # Retorna as mensagens de stdout e stderr.
    conteudo = container.logs(**parametros)

    # O Docker normalmente retorna bytes.
    mensagens = conteudo.decode(
        "utf-8",
        errors="replace"
    ).splitlines()

    registros = []

    # Conta mensagens repetidas no último timestamp.
    # Duas mensagens iguais podem ser emitidas no
    # mesmo instante e não devem ser confundidas.
    ocorrencias_anteriores = {}

    maior_timestamp = ultimo_timestamp
    maior_ns = ultimo_ns
    contagens_finais = {}

    for linha in mensagens:
        resultado = PADRAO_TIMESTAMP.match(linha)

        if not resultado:
            # Sem timestamp válido, não conseguimos
            # posicionar a mensagem no cursor com segurança.
            continue

        timestamp = resultado.group("timestamp")
        mensagem = resultado.group("mensagem")

        atual_ns = timestamp_para_ns(timestamp)

        # Descarta mensagens anteriores à última coleta.
        if ultimo_ns is not None and atual_ns < ultimo_ns:
            continue

        # Identifica o conteúdo e seu timestamp.
        # O hash permite contabilizar mensagens idênticas.
        identificador = hashlib.sha256(
            linha.encode("utf-8")
        ).hexdigest()

        # Encontramos um timestamp mais recente.
        if maior_ns is None or atual_ns > maior_ns:
            maior_ns = atual_ns
            maior_timestamp = timestamp
            contagens_finais = {}

        # Guarda as ocorrências do timestamp mais recente.
        # Essas contagens serão usadas na próxima coleta.
        if atual_ns == maior_ns:
            contagens_finais[identificador] = (
                contagens_finais.get(identificador, 0) + 1
            )

        # Verifica duplicações na fronteira da última coleta.
        if ultimo_ns is not None and atual_ns == ultimo_ns:
            ocorrencias_anteriores[identificador] = (
                ocorrencias_anteriores.get(identificador, 0) + 1
            )

            # Ignora as ocorrências já processadas.
            if (
                ocorrencias_anteriores[identificador]
                <= contagens_anteriores.get(identificador, 0)
            ):
                continue

        registros.append({
            "TIMESTAMP": timestamp,
            "ENDERECO_MAC": endereco_mac,
            "CONTAINER_ID": container.id,
            "CONTAINER": container.name,
            "LOG_LEVEL": identificar_nivel_log(mensagem),
            "LOG_MENSAGEM": mensagem
        })

    # Quando não avançamos para outro timestamp,
    # preservamos as contagens anteriormente conhecidas.
    if maior_ns == ultimo_ns:
        for identificador, quantidade in contagens_anteriores.items():
            contagens_finais[identificador] = max(
                quantidade,
                contagens_finais.get(identificador, 0)
            )

    novo_cursor = {
        "timestamp": maior_timestamp,
        "contagens": contagens_finais
    }

    return registros, novo_cursor


def capturar_logs(endereco_mac=None):
    containers = listar_containers()

    # Permite receber o MAC do agente principal.
    # Caso contrário, utiliza o mesmo método de metricas.py.
    if endereco_mac is None:
        endereco_mac = obter_endereco_mac()

    # Carrega os cursores da coleta anterior.
    estado = carregar_estado()
    novo_estado = estado.copy()

    registros = []

    for container in containers:
        try:
            logs, cursor = capturar_logs_container(
                container=container,
                endereco_mac=endereco_mac,
                cursor=estado.get(container.id)
            )

            # Reúne as mensagens de todos os containers.
            registros.extend(logs)

            # Atualiza a posição somente dos containers
            # cuja consulta terminou com sucesso.
            novo_estado[container.id] = cursor

        except docker.errors.APIError as erro:
            # Uma falha em um container não pode interromper
            # a captura dos demais.
            print(
                f"Erro ao capturar logs de "
                f"{container.name}: {erro}"
            )

    return registros, novo_estado


def gerar_csv(dados, diretorio=DIRETORIO_CSV):
    # Não geramos um CSV vazio quando não existem
    # mensagens novas nesta execução.
    if not dados:
        return None

    pasta = Path(diretorio)
    pasta.mkdir(parents=True, exist_ok=True)

    # Timestamp e identificador adicional evitam colisões
    # entre nomes de arquivos de diferentes coletas.
    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    identificador = uuid.uuid4().hex[:8]

    caminho = pasta / f"logs_{timestamp}_{identificador}.csv"

    with open(
        caminho,
        "w",
        newline="",
        encoding="utf-8"
    ) as arquivo:

        escritor = csv.DictWriter(
            arquivo,
            fieldnames=COLUNAS_LOGS,
            delimiter=";"
        )

        escritor.writeheader()

        # Cada mensagem representa uma linha do CSV.
        # O DictWriter também trata delimitadores e
        # aspas existentes no conteúdo dos logs.
        escritor.writerows(dados)

    return caminho


if __name__ == "__main__":
    try:
        # Captura somente as mensagens ainda não processadas.
        registros, novo_estado = capturar_logs()

        # Salva as mensagens antes de avançar o cursor.
        caminho = gerar_csv(registros)

        # Atualiza as posições de leitura depois da gravação.
        # Na futura integração com S3, o cursor deverá ser
        # confirmado após a persistência do lote no destino.
        salvar_estado(novo_estado)

        if caminho:
            print(f"CSV gerado em: {caminho}")
            print(f"Novos logs capturados: {len(registros)}")
        else:
            print("Nenhum log novo encontrado.")

    finally:
        # Fecha a conexão Docker desta execução.
        cliente.close()
