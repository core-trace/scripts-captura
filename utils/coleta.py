# Funções pequenas usadas por mais de uma categoria de coleta.
import math


def numero_valido(valor):
    # bool é considerado inteiro mas não é uma leitura CPU
    if isinstance(valor, bool):
        return False
    if not isinstance(valor, (int, float)):
        return False
    return math.isfinite(valor)



def salvar_leitura(dados, coluna, valor, status="ok"):
    # Campo vazio significa leitura inválida
    # zero continua sendo um valor válido
    if status == "ok" and (not numero_valido(valor) or valor < 0):
        valor = None
        status = "indisponivel"
    if status == "ok":
        dados[coluna] = valor
    else:
        dados[coluna] = None
    dados[f"{coluna}_status"] = status



def nova_linha_metricas(colunas, horario, mac):
    dados: dict[str, str | int | float | None] = {}
    for coluna in colunas:
        dados[coluna] = None
    dados["TIMESTAMP"] = horario
    dados["ENDERECO_MAC"] = mac
    dados["COLETA_STATUS"] = "ok"
    return dados



def diferenca_contador(atual, antes):
    # Contador que diminuiu não vira taxa negativa
    if atual is None or not numero_valido(atual) or atual < 0:
        return None, "indisponivel"
    if antes is None or not numero_valido(antes):
        return None, "aquecendo"
    if atual < antes:
        return None, "contador_reiniciado"
    return atual - antes, "ok"



