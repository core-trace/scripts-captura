# Coleta de carga
import psutil as p
import time
import sys
from utils.coleta import numero_valido, salvar_leitura


IDS_CARGA = ["load_avg_1", "load_avg_5", "load_avg_15"]


def preparar_coleta_carga(selecionadas, fonte=p, relogio=time.monotonic, plataforma=None):
    escolhas = []
    for nome in selecionadas:
        if nome in IDS_CARGA and nome not in escolhas:
            escolhas.append(nome)
    for nome in ["load_avg_5", "load_avg_15"]:
        if nome not in escolhas:
            escolhas.append(nome)
    if plataforma is None:
        plataforma = sys.platform
    return {"selecionadas": escolhas, "fonte": fonte, "relogio": relogio,
            "plataforma": plataforma, "inicio_windows": None}


def colunas_carga(estado):
    colunas = []
    for nome in IDS_CARGA:
        if nome in estado["selecionadas"]:
            colunas.extend([nome, nome + "_status"])
    return colunas


def coletar_carga(estado):
    dados: dict[str, str | int | float | None] = {}
    for coluna in colunas_carga(estado):
        dados[coluna] = None
    try:
        cargas = estado["fonte"].getloadavg()
        if len(cargas) != 3:
            raise ValueError("Esperados três valores de carga média.")
        aquecendo = False
        if estado["plataforma"] == "win32":
            agora = estado["relogio"]()
            if estado["inicio_windows"] is None:
                estado["inicio_windows"] = agora
            # No Windows o psutil precisa iniciar em segundo plano
            aquecendo = agora - estado["inicio_windows"] <= 5
        for indice, nome in enumerate(IDS_CARGA):
            if nome not in estado["selecionadas"]:
                continue
            valor = cargas[indice]
            if aquecendo:
                salvar_leitura(dados, nome, None, "aquecendo")
            elif valor is None or not numero_valido(valor) or valor < 0:
                salvar_leitura(dados, nome, None, "indisponivel")
            else:
                # Zero é válido
                salvar_leitura(dados, nome, valor)
    except NotImplementedError:
        for nome in estado["selecionadas"]:
            salvar_leitura(dados, nome, None, "nao_aplicavel")
    except (OSError, ValueError, AttributeError, TypeError):
        for nome in estado["selecionadas"]:
            salvar_leitura(dados, nome, None, "erro_fonte")
    return dados