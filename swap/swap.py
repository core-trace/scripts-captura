import utils.exibicao as ex
import math


def ler_valor_swap(registro, campo):
    # O CSV contém texto
    # Campos vazios ou com erro não entram no cálculo
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
    if not math.isfinite(numero) or numero < 0:
        return None
    return numero


def mesmo_servidor(lista):
    # Separar por servidor antes de chamar as funções de análise.
    servidores = set()
    for registro in lista:
        servidores.add(registro.get("ENDERECO_MAC"))
    return len(servidores) == 1


def media_swap(lista, campo):
    if not mesmo_servidor(lista):
        return None
    soma = 0
    quantidade = 0
    for registro in lista:
        valor = ler_valor_swap(registro, campo)
        if valor is not None:
            soma += valor
            quantidade += 1
    if quantidade == 0:
        return None
    return soma / quantidade


def media_swap_percent(lista: list):
    return media_swap(lista, "swap_percent")


def atividade_media_swap(lista, campo):
    if not mesmo_servidor(lista):
        return None
    bytes_totais = 0
    segundos_totais = 0
    for registro in lista:
        taxa = ler_valor_swap(registro, campo)
        segundos = ler_valor_swap(registro, "swap_interval_seconds")
        if taxa is not None and segundos is not None and segundos > 0:
            # A taxa já veio pronta. Recuperamos os bytes de cada intervalo.
            bytes_totais += taxa * segundos
            segundos_totais += segundos
    if segundos_totais == 0:
        return None
    # Intervalos longos têm mais peso do que intervalos curtos.
    return bytes_totais / segundos_totais


def swap_in_por_segundo(lista: list):
    return atividade_media_swap(lista, "swap_sin")


def swap_out_por_segundo(lista: list):
    return atividade_media_swap(lista, "swap_sout")


def exibir_valor_swap(lista, nome, campo, valor, unidade):
    if valor is not None:
        ex.resultado(nome, valor, unidade)
        return
    # ausência de swap n é falha ou aquecimento
    nao_aplicavel = len(lista) > 0
    for registro in lista:
        if registro.get(campo + "_status") != "nao_aplicavel":
            nao_aplicavel = False
    if nao_aplicavel:
        print(f"{nome} Não aplicável")
    else:
        print(f"{nome} Sem dados válidos")


def exibir_swap(dados: list):
    ex.titulo("SWAP")
    if not mesmo_servidor(dados):
        print("Swap: selecione os registros de um único servidor.")
        return
    exibir_valor_swap(dados, "Uso médio:", "swap_percent", media_swap_percent(dados), "%")
    exibir_valor_swap(dados, "Entrada média:", "swap_sin", swap_in_por_segundo(dados), "bytes/s")
    exibir_valor_swap(dados, "Saída média:", "swap_sout", swap_out_por_segundo(dados), "bytes/s")
    exibir_valor_swap(dados, "Swap em uso média:", "swap_used", media_swap(dados, "swap_used"), "GB")
    exibir_valor_swap(dados, "Swap livre média:", "swap_free", media_swap(dados, "swap_free"), "GB")
