import utils.exibicao as ex
import math


def ler_valor_ram(registro, campo):
    # Os valores do CSV chegam como texto. Leitura vazia ou inválida não entra na média.
    valor = registro.get(campo)
    if valor is None or valor == "":
        return None
    status = registro.get(campo + "_status")
    if status is not None and status != "ok":
        return None
    try:
        numero = float(valor)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(numero):
        return None
    return numero


def media_ram(lista, campo):
    soma = 0
    quantidade = 0
    for registro in lista:
        valor = ler_valor_ram(registro, campo)
        if valor is not None:
            soma += valor
            quantidade += 1
    if quantidade == 0:
        return None
    return soma / quantidade


def media_ram_percent(lista: list):
    return media_ram(lista, "ram_percent")


def media_ram_disponivel(lista: list):
    return media_ram(lista, "ram_available")


def media_ram_total(lista: list):
    return media_ram(lista, "ram_total")


def media_ram_usada(lista: list):
    return media_ram(lista, "ram_used")


def media_ram_livre(lista: list):
    return media_ram(lista, "ram_free")


def media_ram_cache(lista: list):
    return media_ram(lista, "ram_cached")


def total_encerramentos_oom(lista: list):
    # Cada linha traz novos registros desde a coleta anterior. Somamos, não fazemos média.
    total = 0
    quantidade = 0
    for registro in lista:
        valor = ler_valor_ram(registro, "ram_oom_kills")
        if valor is not None:
            total += valor
            quantidade += 1
    if quantidade == 0:
        return None
    return total


def exibir_valor_ram(nome, valor, unidade=""):
    if valor is None:
        print(f"{nome} Sem dados válidos")
    else:
        ex.resultado(nome, valor, unidade)


def exibir_ram(dados: list):
    ex.titulo("RAM")
    exibir_valor_ram("Uso médio:", media_ram_percent(dados), "%")
    exibir_valor_ram("RAM disponível média:", media_ram_disponivel(dados), "GB")
    exibir_valor_ram("Capacidade total média:", media_ram_total(dados), "GB")
    exibir_valor_ram("Novos registros OOM observados:", total_encerramentos_oom(dados), "ocorrências")
    exibir_valor_ram("RAM usada média:", media_ram_usada(dados), "GB")
    exibir_valor_ram("RAM livre média:", media_ram_livre(dados), "GB")
    exibir_valor_ram("RAM em cache média:", media_ram_cache(dados), "GB")
