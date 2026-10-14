# Coleta de swap: preparação, cabeçalho e leitura das métricas.
import psutil as p
import time
import sys
from utils.coleta import numero_valido, salvar_leitura


IDS_SWAP = ["swap_percent", "swap_sin", "swap_sout", "swap_used", "swap_free", "swap_total"]


def preparar_coleta_swap(selecionadas, fonte=p, relogio=time.monotonic, plataforma=None):
    escolhas = []
    for nome in selecionadas:
        if nome in IDS_SWAP and nome not in escolhas:
            escolhas.append(nome)
    for nome in ["swap_percent", "swap_sin", "swap_sout"]:
        if nome not in escolhas:
            escolhas.append(nome)
    if plataforma is None:
        plataforma = sys.platform
    return {"selecionadas": escolhas, "anterior": None, "fonte": fonte,
            "relogio": relogio, "plataforma": plataforma}


def colunas_swap(estado):
    colunas = []
    for nome in IDS_SWAP:
        # Capacidade é automática mesmo sem checkbox na tela de cardápio
        if nome in estado["selecionadas"] or nome == "swap_total":
            colunas.extend([nome, nome + "_status"])
    colunas.append("swap_interval_seconds")
    return colunas


def coletar_swap(estado):
    dados: dict[str, str | int | float | None] = {}
    for coluna in colunas_swap(estado):
        dados[coluna] = None
    try:
        # Uma consulta fornece espaço e os dois contadores acumulados
        memoria = estado["fonte"].swap_memory()
        agora = estado["relogio"]()
        total = memoria.total
        if total is None or not numero_valido(total) or total < 0:
            raise ValueError("Capacidade de swap inválida.")
        salvar_leitura(dados, "swap_total", total / 1_000_000_000)
        if total == 0:
            for nome in IDS_SWAP:
                if nome != "swap_total" and nome in estado["selecionadas"]:
                    salvar_leitura(dados, nome, None, "nao_aplicavel")
            estado["anterior"] = None
            return dados

        for nome, atributo in [("swap_percent", "percent"), ("swap_used", "used"), ("swap_free", "free")]:
            if nome not in estado["selecionadas"]:
                continue
            valor = getattr(memoria, atributo, None)
            limite = 100 if nome == "swap_percent" else total
            if valor is None or not numero_valido(valor) or valor < 0 or valor > limite:
                salvar_leitura(dados, nome, None, "indisponivel")
            elif nome == "swap_percent":
                salvar_leitura(dados, nome, valor)
            else:
                salvar_leitura(dados, nome, valor / 1_000_000_000)

        coletar_atividade_swap(estado, dados, memoria, agora)
    except (OSError, ValueError, AttributeError, TypeError, NotImplementedError):
        for nome in IDS_SWAP:
            if nome in estado["selecionadas"] or nome == "swap_total":
                salvar_leitura(dados, nome, None, "erro_fonte")
        # Depois de uma falha não calcular uma taxa sobre um período desconhecido
        estado["anterior"] = None
    return dados


def coletar_atividade_swap(estado, dados, memoria, agora):
    # No Windows, o psutil devolve zero para esses contadores sem medi-los
    if not estado["plataforma"].startswith("linux"):
        for nome in ["swap_sin", "swap_sout"]:
            salvar_leitura(dados, nome, None, "nao_aplicavel")
        estado["anterior"] = None
        return

    anterior = estado["anterior"]
    intervalo = None
    if anterior is not None:
        intervalo = agora - anterior["horario"]
        if intervalo > 0:
            dados["swap_interval_seconds"] = intervalo

    for nome, atributo in [("swap_sin", "sin"), ("swap_sout", "sout")]:
        valor = getattr(memoria, atributo, None)
        antes = None
        if anterior is not None:
            antes = getattr(anterior["memoria"], atributo, None)
        if valor is None or not numero_valido(valor) or valor < 0:
            salvar_leitura(dados, nome, None, "indisponivel")
        elif anterior is None or antes is None or not numero_valido(antes) or memoria.total != anterior["memoria"].total:
            salvar_leitura(dados, nome, None, "aquecendo")
        elif intervalo is None or intervalo <= 0:
            salvar_leitura(dados, nome, None, "intervalo_invalido")
        elif valor < antes:
            salvar_leitura(dados, nome, None, "contador_reiniciado")
        else:
            # Exemplo: 1.000 novos bytes em 2 segundos = 500 bytes/s
            novos_bytes = valor - antes
            salvar_leitura(dados, nome, novos_bytes / intervalo)
    estado["anterior"] = {"memoria": memoria, "horario": agora}


# CARGA MÉDIA
