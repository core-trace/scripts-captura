import utils.exibicao as ex
import utils.funcoes_gerais as fg

def media_ram_percent(lista: list):
    return fg.media_uso_componente(
        lista,
        "RAM_PERCENT"
    )


def media_ram_disponivel(lista: list):
    return fg.media_uso_componente(
        lista,
        "RAM_AVAILABLE"
    )

def media_ram_usada(lista: list):
    return fg.media_uso_componente(
        lista,
        "RAM_USED"
    )

def exibir_ram(dados: list):
    ex.titulo("RAM")

    ex.resultado(
        "Uso médio:",
        media_ram_percent(dados),
        "%"
    )

    ex.resultado(
        "RAM disponível média:",
        media_ram_disponivel(dados),
        "bytes"
    )

    ex.resultado(
        "RAM usada média:",
        media_ram_usada(dados),
        "bytes"
    )
    