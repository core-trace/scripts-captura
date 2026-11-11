
import csv
import json
import uuid
import hashlib
import docker

from datetime import datetime, timezone
from pathlib import Path

# Reutiliza o identificador do servidor usado em metricas.py.
from .metricas import obter_endereco_mac


COLUNAS_EVENTOS = [
    "TIMESTAMP",
    "ENDERECO_MAC",
    "EVENT_ID",
    "CONTAINER_ID",
    "CONTAINER",
    "EVENT_TYPE",
    "EVENT_ACTION",
    "EVENT_DETAILS",
    "EXIT_CODE"
]


# Todos os caminhos são relativos à pasta deste script.
DIRETORIO_BASE = Path(__file__).resolve().parent
DIRETORIO_CSV = DIRETORIO_BASE / "csv"
CAMINHO_ESTADO = DIRETORIO_BASE / "estado" / "estado_eventos.json"

# Primeira execução: tenta recuperar a última hora de eventos.
# O Docker mantém um histórico limitado de eventos.
JANELA_INICIAL_SEGUNDOS = 3600

cliente = docker.from_env()


def carregar_estado():
    # Se ainda não temos checkpoint, esta será a
    # primeira execução do agente de eventos.
    if not CAMINHO_ESTADO.exists():
        return {}

    with open(CAMINHO_ESTADO, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def salvar_estado(ultimo_segundo):
    CAMINHO_ESTADO.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    estado = {
        "ultimo_segundo": ultimo_segundo,
        "atualizado_em": datetime.now(
            timezone.utc
        ).isoformat()
    }

    # Escrevemos em um arquivo temporário para evitar
    # corromper o checkpoint caso a gravação seja interrompida.
    temporario = CAMINHO_ESTADO.with_suffix(".tmp")

    with open(temporario, "w", encoding="utf-8") as arquivo:
        json.dump(
            estado,
            arquivo,
            indent=4,
            ensure_ascii=False
        )

    temporario.replace(CAMINHO_ESTADO)


def formatar_timestamp(evento):
    # O Docker pode fornecer timeNano, que preserva
    # nanossegundos, ou time, com precisão de segundos.
    nanos = evento.get("timeNano")

    if nanos is None:
        segundos = evento.get("time")

        if segundos is None:
            return None

        nanos = int(segundos) * 1_000_000_000

    segundos, fracao = divmod(
        int(nanos),
        1_000_000_000
    )

    # Convertemos para UTC, preservando a fração
    # original de nanossegundos.
    data = datetime.fromtimestamp(
        segundos,
        tz=timezone.utc
    )

    return (
        data.strftime("%Y-%m-%dT%H:%M:%S")
        + f".{fracao:09d}Z"
    )


def gerar_id_evento(evento):
    # O Docker não fornece um identificador único
    # explícito para todos os eventos.
    #
    # Criamos uma identificação determinística com base
    # no horário, container, ação e atributos.
    #
    # A ETL poderá utilizar este ID para detectar
    # registros repetidos entre diferentes CSVs.
    ator = evento.get("Actor") or {}

    identificacao = {
        "timestamp": evento.get("timeNano", evento.get("time")),
        "container_id": ator.get("ID") or evento.get("id"),
        "acao": evento.get("Action") or evento.get("status"),
        "atributos": ator.get("Attributes") or {}
    }

    conteudo = json.dumps(
        identificacao,
        sort_keys=True,
        ensure_ascii=False
    )

    return hashlib.sha256(
        conteudo.encode("utf-8")
    ).hexdigest()


def descrever_evento(acao, atributos):
    # Gera uma descrição simples para apresentar
    # no histórico de eventos da dashboard.
    #
    # Não inventamos causas que o Docker não informa.
    # Exemplo: restart não indica necessariamente
    # uma reinicialização manual.

    if acao == "create":
        return "Container criado"

    if acao == "start":
        return "Container iniciado"

    if acao == "stop":
        return "Container parado"

    if acao == "die":
        codigo = atributos.get("exitCode")

        if codigo is not None:
            return f"Container encerrado com código {codigo}"

        return "Container encerrado"

    if acao == "restart":
        return "Reinicialização do container"

    if acao == "oom":
        return "Evento de falta de memória (OOM)"

    if acao.startswith("health_status:"):
        status = acao.split(":", 1)[1].strip()
        return f"Healthcheck: {status}"

    if acao == "kill":
        sinal = atributos.get("signal")

        if sinal is not None:
            return f"Sinal de encerramento recebido: {sinal}"

        return "Sinal de encerramento recebido"

    if acao == "destroy":
        return "Container removido"

    if acao == "pause":
        return "Container pausado"

    if acao == "unpause":
        return "Container retomado"

    if acao == "rename":
        return "Container renomeado"

    if acao == "update":
        return "Configuração do container atualizada"

    # Eventos não reconhecidos continuam sendo registrados.
    return f"Evento Docker: {acao}"


def normalizar_evento(evento, endereco_mac):
    # O Docker retorna eventos com estruturas como:
    # Type, Action, Actor, time e timeNano.
    #
    # Algumas versões também apresentam os campos
    # legados status e id.

    ator = evento.get("Actor") or {}
    atributos = ator.get("Attributes") or {}

    container_id = (
        ator.get("ID")
        or evento.get("id")
    )

    container_nome = (
        atributos.get("name")
        or (container_id[:12] if container_id else None)
    )

    tipo = evento.get("Type") or "container"

    acao = (
        evento.get("Action")
        or evento.get("status")
        or "unknown"
    )

    return {
        "TIMESTAMP": formatar_timestamp(evento),
        "ENDERECO_MAC": endereco_mac,
        "EVENT_ID": gerar_id_evento(evento),
        "CONTAINER_ID": container_id,
        "CONTAINER": container_nome,
        "EVENT_TYPE": tipo,
        "EVENT_ACTION": acao,
        "EVENT_DETAILS": descrever_evento(acao, atributos),
        "EXIT_CODE": atributos.get("exitCode")
    }


def capturar_eventos(endereco_mac=None):
    # Utilizamos o MAC da máquina monitorada.
    # Também podemos receber o endereço do agente principal.
    if endereco_mac is None:
        endereco_mac = obter_endereco_mac()

    estado = carregar_estado()
    ultimo_segundo = estado.get("ultimo_segundo")

    # Definimos o fim do intervalo antes da consulta.
    # Não usamos um fluxo infinito: esta função deve terminar
    # e devolver os registros de uma coleta.
    #
    # O instante atual em segundos também é a fronteira
    # exclusiva de leitura. Assim não avançamos o cursor
    # sobre o segundo que ainda está em andamento.
    fim_exclusivo = int(
        datetime.now(timezone.utc).timestamp()
    )

    if ultimo_segundo is None:
        # Primeira execução: tenta ler a última hora.
        inicio = fim_exclusivo - JANELA_INICIAL_SEGUNDOS

    else:
        # Voltamos um segundo antes do checkpoint.
        # A filtragem abaixo descarta eventos já processados,
        # evitando problemas de inclusão nos limites da API.
        inicio = max(0, int(ultimo_segundo) - 1)

        # Se não existe um segundo completo novo,
        # não precisamos consultar o Docker.
        if fim_exclusivo <= int(ultimo_segundo) + 1:
            return [], int(ultimo_segundo)

    registros = []

    # Consulta somente eventos de containers.
    # O filtro evita trazer eventos de imagens, redes e volumes.
    fluxo = cliente.events(
        since=inicio,
        until=fim_exclusivo,
        filters={"type": "container"},
        decode=True
    )

    try:
        for evento in fluxo:
            # Utilizamos o instante real do evento para
            # verificar se ele pertence ao intervalo.
            nanos = evento.get("timeNano")

            if nanos is not None:
                segundo_evento = int(nanos) // 1_000_000_000
            else:
                segundo_evento = evento.get("time")

            if segundo_evento is None:
                continue

            segundo_evento = int(segundo_evento)

            # Os segundos anteriores já foram processados.
            if (
                ultimo_segundo is not None
                and segundo_evento <= int(ultimo_segundo)
            ):
                continue

            # A fronteira final é exclusiva.
            # Eventos desse segundo serão capturados
            # na próxima execução.
            if segundo_evento >= fim_exclusivo:
                continue

            registro = normalizar_evento(
                evento,
                endereco_mac
            )

            registros.append(registro)

    finally:
        # Fecha o fluxo de eventos, liberando a conexão.
        fluxo.close()

    # Ordena os eventos cronologicamente.
    registros.sort(key=lambda item: item["TIMESTAMP"])

    # O checkpoint avança apenas até o último
    # segundo completo consultado com sucesso.
    novo_checkpoint = fim_exclusivo - 1

    return registros, novo_checkpoint


def gerar_csv(dados, diretorio=DIRETORIO_CSV):
    # Sem novos eventos, não precisamos produzir
    # arquivos CSV vazios.
    if not dados:
        return None

    pasta = Path(diretorio)
    pasta.mkdir(
        parents=True,
        exist_ok=True
    )

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    # Identificador adicional para evitar colisões
    # de nomes entre coletas.
    identificador = uuid.uuid4().hex[:8]

    caminho = pasta / (
        f"eventos_{timestamp}_{identificador}.csv"
    )

    temporario = caminho.with_suffix(".tmp")

    # O arquivo é escrito primeiro como temporário.
    # Só depois de concluído ele recebe a extensão CSV.
    with open(
        temporario,
        "w",
        newline="",
        encoding="utf-8"
    ) as arquivo:

        escritor = csv.DictWriter(
            arquivo,
            fieldnames=COLUNAS_EVENTOS,
            delimiter=";"
        )

        escritor.writeheader()
        escritor.writerows(dados)

    temporario.replace(caminho)

    return caminho


if __name__ == "__main__":
    try:
        # Busca os novos eventos desde a última coleta.
        registros, novo_checkpoint = capturar_eventos()

        # Primeiro persistimos os eventos.
        caminho = gerar_csv(registros)

        # Depois confirmamos a posição processada.
        # Futuramente, com o S3, poderemos avançar
        # o checkpoint após confirmar o envio do lote.
        salvar_estado(novo_checkpoint)

        if caminho:
            print(f"CSV gerado em: {caminho}")
            print(f"Eventos capturados: {len(registros)}")

            for registro in registros:
                print(
                    f"{registro['CONTAINER']} | "
                    f"{registro['EVENT_ACTION']} | "
                    f"{registro['EVENT_DETAILS']}"
                )
        else:
            print("Nenhum evento novo encontrado.")

    finally:
        cliente.close()
