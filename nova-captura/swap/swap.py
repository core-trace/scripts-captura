import utils.exibicao as ex
import utils.funcoes_gerais as fg
from datetime import datetime

def media_swap_percent(lista: list):
    return fg.media_uso_componente(
        lista,
        "SWAP_PERCENT"
    )


def swap_in_por_segundo(lista: list):
    if len(lista) < 2:
        return 0

    swap_inicio = float(
        lista[0]["SWAP_IN"]
    )

    swap_fim = float(
        lista[-1]["SWAP_IN"]
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
        swap_fim - swap_inicio
    ) / segundos


def swap_out_por_segundo(lista: list):
    if len(lista) < 2:
        return 0

    swap_inicio = float(
        lista[0]["SWAP_OUT"]
    )

    swap_fim = float(
        lista[-1]["SWAP_OUT"]
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
        swap_fim - swap_inicio
    ) / segundos


def exibir_swap(dados: list):
    ex.titulo("SWAP")

    ex.resultado(
        "Uso médio:",
        media_swap_percent(dados),
        "%"
    )

    ex.resultado(
        "Swap IN:",
        swap_in_por_segundo(dados),
        "bytes/s"
    )

    ex.resultado(
        "Swap OUT:",
        swap_out_por_segundo(dados),
        "bytes/s"
    )
