import utils.exibicao as ex
from datetime import datetime
import math

def ler_valor_cpu(registro, campo):
    # Lê o campo do CSV. 
    # Se a métrica não foi coletada retorna None
    valor = registro.get(campo)

    # Se campo for vazio ou leitura for inválida não deve entrar na média
    if valor is None or valor == "":
        return None

    status = registro.get(campo + "_status")
    if status is not None and status != "ok":
        return None

    # Tenta transformar o valor em número
    try:
        numero = float(valor)

    except (ValueError, TypeError):
        return None

    # Se for infinito ou Nan retorna none
    if not math.isfinite(numero):
        return None
    return numero


def media_cpu(lista, campo, minutos=None):
    # Soma só as leituras válidas
    # Zero também é uma leitura válida
    if len(lista) == 0:
        return None

    ultima_data = None
    if minutos is not None:
        ultima_data = datetime.strptime(lista[-1]["TIMESTAMP"], "%Y-%m-%d %H:%M:%S")

    soma = 0
    quantidade = 0

    for registro in lista:
        if minutos is not None and ultima_data is not None:
            data = datetime.strptime(registro["TIMESTAMP"], "%Y-%m-%d %H:%M:%S")
            segundos = (ultima_data - data).total_seconds()
            if segundos < 0 or segundos > minutos * 60:
                continue

        valor = ler_valor_cpu(registro, campo)
        if valor is not None:
            soma += valor
            quantidade += 1

    if quantidade == 0:
        return None
    return soma / quantidade


def media_cpu_geral(lista: list):
    return media_cpu(
        lista,
        "cpu_percent"
    )


def media_cpu_30_min(lista: list):
    return media_cpu(
        lista,
        "cpu_percent",
        minutos=30
    )


def media_cpu_1_dia(lista: list):
    return media_cpu(
        lista,
        "cpu_percent",
        minutos=1440
    )


def media_cpu_user(lista: list):
    return media_cpu(
        lista,
        "cpu_user"
    )


def media_cpu_system(lista: list):
    return media_cpu(
        lista,
        "cpu_system"
    )


def media_cpu_idle(lista: list):
    return media_cpu(
        lista,
        "cpu_idle"
    )


def media_cpu_iowait(lista: list):
    return media_cpu(
        lista,
        "cpu_iowait"
    )


def interrupcoes_por_segundo(lista: list):
    # A coleta já calculou eventos/s. Aqui fazemos a média dessas leituras.
    return media_cpu(
        lista,
        "cpu_interrupts_per_second"
    )


def media_frequencia_cpu(lista: list):
    return media_cpu(
        lista,
        "cpu_freq_current"
    )


def media_load_1(lista: list):
    return media_cpu(
        lista,
        "load_avg_1"
    )


def media_load_5(lista: list):
    return media_cpu(
        lista,
        "load_avg_5"
    )


def media_load_15(lista: list):
    return media_cpu(
        lista,
        "load_avg_15"
    )

def carga_relativa(registro, campo="load_avg_5"):
    carga = ler_valor_cpu(registro, campo)
    quantidade = ler_valor_cpu(registro, "cpu_count_logical")
    if carga is None or carga < 0 or quantidade is None or quantidade <= 0 or not quantidade.is_integer():
        return None
    return carga / quantidade


def media_carga_relativa(lista, campo="load_avg_5"):
    # Dividir cada leitura pela capacidade registrada naquela mesma
    servidores = set(registro.get("ENDERECO_MAC") for registro in lista)
    if len(servidores) != 1:
        return None
    soma = 0
    quantidade = 0
    for registro in lista:
        valor = carga_relativa(registro, campo)
        if valor is not None:
            soma += valor
            quantidade += 1
    if quantidade == 0:
        return None
    return soma / quantidade


def exibir_valor_cpu(nome, valor, unidade=""):
    # Tratar ausencia ja feito
    if valor is None:
        print(f"{nome} Sem dados válidos")
    else:
        ex.resultado(nome, valor, unidade)


def exibir_cpu(dados: list):
    ex.titulo("CPU")

    exibir_valor_cpu(
        "Uso médio geral:",
        media_cpu_geral(dados),
        "%"
    )

    exibir_valor_cpu(
        "Uso médio últimos 30 min:",
        media_cpu_30_min(dados),
        "%"
    )

    exibir_valor_cpu(
        "Uso médio último dia:",
        media_cpu_1_dia(dados),
        "%"
    )

    exibir_valor_cpu(
        "CPU em user:",
        media_cpu_user(dados),
        "%"
    )

    exibir_valor_cpu(
        "CPU em system:",
        media_cpu_system(dados),
        "%"
    )

    exibir_valor_cpu(
        "CPU ociosa:",
        media_cpu_idle(dados),
        "%"
    )

    exibir_valor_cpu(
        "CPU aguardando I/O:",
        media_cpu_iowait(dados),
        "%"
    )

    exibir_valor_cpu(
        "Interrupções por segundo:",
        interrupcoes_por_segundo(dados),
        "int/s"
    )

    exibir_valor_cpu(
        "Frequência média:",
        media_frequencia_cpu(dados),
        "MHz"
    )

    exibir_valor_cpu(
        "Load Average 1 min:",
        media_load_1(dados)
    )

    exibir_valor_cpu(
        "Load Average 5 min:",
        media_load_5(dados)
    )

    exibir_valor_cpu(
        "Load Average 15 min:",
        media_load_15(dados)
    )
    exibir_valor_cpu("Carga relativa média · 5 min:", media_carga_relativa(dados))
    exibir_valor_cpu("Carga relativa média · 15 min:", media_carga_relativa(dados, "load_avg_15"))
