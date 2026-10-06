import utils.exibicao as ex
import utils.funcoes_gerais as fg

def media_disco_percent(lista: list):
    return fg.media_uso_componente(
        lista,
        "DISCO_PERCENT"
    )


def media_disco_livre(lista: list):
    return fg.media_uso_componente(
        lista,
        "DISCO_FREE"
    )


def media_disco_usado(lista: list):
    return fg.media_uso_componente(
        lista,
        "DISCO_USED"
    )


def crescimento_espaco_disco(lista: list):
    if len(lista) < 2:
        return 0

    usado_inicio = float(
        lista[0]["DISCO_USED"]
    )

    usado_fim = float(
        lista[-1]["DISCO_USED"]
    )

    if usado_inicio == 0:
        return 0

    return (
        (usado_fim - usado_inicio)
        * 100
        / usado_inicio
    )

def exibir_disco(dados: list):
    ex.titulo("DISCO")

    ex.resultado(
        "Uso médio:",
        media_disco_percent(dados),
        "%"
    )

    ex.resultado(
        "Espaço usado médio:",
        media_disco_usado(dados),
        "bytes"
    )

    ex.resultado(
        "Espaço livre médio:",
        media_disco_livre(dados),
        "bytes"
    )

    ex.resultado(
        "Crescimento:",
        crescimento_espaco_disco(dados),
        "%"
    )