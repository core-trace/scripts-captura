import utils.exibicao as ex
import utils.funcoes_gerais as fg
from datetime import datetime

def media_cpu_geral(lista: list):
    return fg.media_uso_componente(
        lista,
        "CPU_PERCENT"
    )


def media_cpu_30_min(lista: list):
    return fg.media_interv_min(
        lista,
        30,
        "CPU_PERCENT"
    )


def media_cpu_1_dia(lista: list):
    return fg.media_interv_min(
        lista,
        1440,
        "CPU_PERCENT"
    )


def media_cpu_user(lista: list):
    return fg.media_uso_componente(
        lista,
        "CPU_USER_PERCENT"
    )


def media_cpu_system(lista: list):
    return fg.media_uso_componente(
        lista,
        "CPU_SYSTEM_PERCENT"
    )


def media_cpu_idle(lista: list):
    return fg.media_uso_componente(
        lista,
        "CPU_IDLE_PERCENT"
    )


def media_cpu_iowait(lista: list):
    return fg.media_uso_componente(
        lista,
        "CPU_IOWAIT_PERCENT"
    )


def interrupcoes_por_segundo(lista: list):
    if len(lista) < 2:
        return 0

    interrupcoes_inicio = float(
        lista[0]["CPU_INTERRUPTS"]
    )

    interrupcoes_fim = float(
        lista[-1]["CPU_INTERRUPTS"]
    )

    data_inicio = datetime.strptime(
        lista[0]["TIMESTAMP"],
        '%Y-%m-%d %H:%M:%S'
    )

    data_fim = datetime.strptime(
        lista[-1]["TIMESTAMP"],
        '%Y-%m-%d %H:%M:%S'
    )

    segundos = (
        data_fim - data_inicio
    ).total_seconds()

    if segundos == 0:
        return 0

    return (
        interrupcoes_fim - interrupcoes_inicio
    ) / segundos


def media_frequencia_cpu(lista: list):
    return fg.media_uso_componente(
        lista,
        "CPU_FREQ_ATUAL"
    )


def media_load_1(lista: list):
    return fg.media_uso_componente(
        lista,
        "LOAD_AVG_1"
    )


def media_load_5(lista: list):
    return fg.media_uso_componente(
        lista,
        "LOAD_AVG_5"
    )


def media_load_15(lista: list):
    return fg.media_uso_componente(
        lista,
        "LOAD_AVG_15"
    )

def exibir_cpu(dados: list):
    ex.titulo("CPU")

    ex.resultado(
        "Uso médio geral:",
        media_cpu_geral(dados),
        "%"
    )

    ex.resultado(
        "Uso médio últimos 30 min:",
        media_cpu_30_min(dados),
        "%"
    )

    ex.resultado(
        "Uso médio último dia:",
        media_cpu_1_dia(dados),
        "%"
    )

    ex.resultado(
        "CPU em user:",
        media_cpu_user(dados),
        "%"
    )

    ex.resultado(
        "CPU em system:",
        media_cpu_system(dados),
        "%"
    )

    ex.resultado(
        "CPU ociosa:",
        media_cpu_idle(dados),
        "%"
    )

    ex.resultado(
        "CPU aguardando I/O:",
        media_cpu_iowait(dados),
        "%"
    )

    ex.resultado(
        "Interrupções por segundo:",
        interrupcoes_por_segundo(dados),
        "int/s"
    )

    ex.resultado(
        "Frequência média:",
        media_frequencia_cpu(dados),
        "MHz"
    )

    ex.resultado(
        "Load Average 1 min:",
        media_load_1(dados)
    )

    ex.resultado(
        "Load Average 5 min:",
        media_load_5(dados)
    )

    ex.resultado(
        "Load Average 15 min:",
        media_load_15(dados)
    )