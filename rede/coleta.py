# Coleta de rede: preparação, cabeçalho e leitura das métricas.
import psutil as p
import time
import sys
from utils.coleta import salvar_leitura, nova_linha_metricas, diferenca_contador


GRUPOS_REDE = {
    "net_traffic": ["net_bytes_recv", "net_bytes_sent"],
    "net_errors": ["net_errin", "net_errout"],
    "net_drops": ["net_dropin", "net_dropout"],
    "net_isup": ["net_isup"],
    "net_packets_recv": ["net_packets_recv"],
    "net_packets_sent": ["net_packets_sent"],
}
ATRIBUTOS_REDE = {
    "net_bytes_recv": "bytes_recv", "net_bytes_sent": "bytes_sent",
    "net_errin": "errin", "net_errout": "errout",
    "net_dropin": "dropin", "net_dropout": "dropout",
    "net_packets_recv": "packets_recv", "net_packets_sent": "packets_sent",
}
IDS_REDE = list(GRUPOS_REDE) + list(ATRIBUTOS_REDE)


def descobrir_interfaces_rede(fonte):
    # O cabeçalho do CSV fica fixo
    nomes = []
    try:
        nomes.extend(fonte.net_if_stats())
    except (OSError, ValueError, AttributeError, TypeError, NotImplementedError, p.AccessDenied):
        pass
    try:
        contadores = fonte.net_io_counters(pernic=True, nowrap=False) or {}
        for nome in contadores:
            if nome not in nomes:
                nomes.append(nome)
    except (OSError, ValueError, AttributeError, TypeError, NotImplementedError, p.AccessDenied):
        pass
    return sorted(nomes)


def preparar_coleta_rede(selecionadas, fonte=p, relogio=time.monotonic, plataforma=None):
    escolhas = []
    for nome in selecionadas:
        if nome in GRUPOS_REDE and nome not in escolhas:
            escolhas.append(nome)
    for nome in ["net_traffic", "net_errors", "net_drops", "net_isup"]:
        if nome not in escolhas:
            escolhas.append(nome)
    if plataforma is None:
        plataforma = sys.platform
    return {"selecionadas": escolhas, "anteriores": {}, "fonte": fonte,
            "relogio": relogio, "plataforma": plataforma,
            "interfaces": descobrir_interfaces_rede(fonte), "novas_avisadas": []}


def campos_rede(estado):
    campos = []
    for grupo in GRUPOS_REDE:
        if grupo in estado["selecionadas"]:
            campos.extend(GRUPOS_REDE[grupo])
    return campos


def colunas_interface_rede(estado):
    colunas = ["TIMESTAMP", "ENDERECO_MAC", "INTERFACE", "COLETA_STATUS", "net_interval_seconds"]
    for campo in campos_rede(estado):
        colunas.extend([campo, campo + "_status"])
    return colunas


def coluna_rede(interface, campo):
    # Ex: net_bytes_recv da eth0 vira rede_eth0_bytes_recv
    return "rede_" + interface + "_" + campo.removeprefix("net_")


def colunas_rede(estado):
    colunas = ["rede_coleta_status"]
    for interface in estado["interfaces"]:
        colunas.append(coluna_rede(interface, "net_interval_seconds"))
        for campo in campos_rede(estado):
            nome = coluna_rede(interface, campo)
            colunas.extend([nome, nome + "_status"])
    return colunas


def coletar_rede_geral(estado):
    # Organiza as interfaces na mesma linha que CPU, RAM, Swap e Carga
    dados: dict[str, str | int | float | None] = {}
    for coluna in colunas_rede(estado):
        dados[coluna] = None
    dados["rede_coleta_status"] = "ok"
    for registro in coletar_rede(estado, "", ""):
        if registro["COLETA_STATUS"] != "ok":
            dados["rede_coleta_status"] = registro["COLETA_STATUS"]
        interface = registro["INTERFACE"]
        if interface not in estado["interfaces"]:
            continue
        dados[coluna_rede(interface, "net_interval_seconds")] = registro["net_interval_seconds"]
        for campo in campos_rede(estado):
            nome = coluna_rede(interface, campo)
            dados[nome] = registro[campo]
            dados[nome + "_status"] = registro[campo + "_status"]
    return dados


def salvar_taxa_rede(dados, campo, atual, antes, intervalo):
    diferenca, status = diferenca_contador(atual, antes)
    if status != "ok" or diferenca is None:
        salvar_leitura(dados, campo, None, status)
    elif intervalo is None or intervalo <= 0:
        salvar_leitura(dados, campo, None, "intervalo_invalido")
    else:
        # Exemplo: 1.000 novos bytes em 2 segundos = 500 bytes/s.
        salvar_leitura(dados, campo, diferenca / intervalo)


def coletar_rede(estado, horario, mac):
    contadores = {}
    interfaces = {}
    status_io = "ok"
    status_interfaces = "ok"
    try:
        contadores = estado["fonte"].net_io_counters(pernic=True, nowrap=False) or {}
    except NotImplementedError:
        status_io = "nao_aplicavel"
    except (OSError, ValueError, AttributeError, TypeError, p.AccessDenied):
        status_io = "erro_fonte"
    try:
        interfaces = estado["fonte"].net_if_stats() or {}
    except NotImplementedError:
        status_interfaces = "nao_aplicavel"
    except (OSError, ValueError, AttributeError, TypeError, p.AccessDenied):
        status_interfaces = "erro_fonte"
    agora = estado["relogio"]()

    encontradas = list(contadores)
    for nome in interfaces:
        if nome not in encontradas:
            encontradas.append(nome)
    for nome in encontradas:
        if nome not in estado["interfaces"] and nome not in estado["novas_avisadas"]:
            print(f"Nova interface {nome}: reinicie o agente para incluí-la no CSV.")
            estado["novas_avisadas"].append(nome)
    nomes = estado["interfaces"]
    novos_anteriores = {}
    linhas = []
    for nome in nomes:
        dados = nova_linha_metricas(colunas_interface_rede(estado), horario, mac)
        dados["INTERFACE"] = nome
        if status_io != "ok" or status_interfaces != "ok":
            dados["COLETA_STATUS"] = "fonte_incompleta"
        atual = contadores.get(nome)
        anterior = estado["anteriores"].get(nome)
        contadores_antes = None
        intervalo = None
        if anterior is not None:
            contadores_antes = anterior["contadores"]
            intervalo = agora - anterior["horario"]
            if intervalo > 0:
                dados["net_interval_seconds"] = intervalo

        for campo in campos_rede(estado):
            if campo == "net_isup":
                info = interfaces.get(nome)
                valor = getattr(info, "isup", None)
                if status_interfaces != "ok":
                    salvar_leitura(dados, campo, None, status_interfaces)
                elif not isinstance(valor, bool):
                    salvar_leitura(dados, campo, None, "indisponivel")
                else:
                    # 1 significa ativa 0 significa inativa
                    salvar_leitura(dados, campo, int(valor))
            elif status_io != "ok":
                salvar_leitura(dados, campo, None, status_io)
            elif campo == "net_dropout" and (estado["plataforma"] == "darwin" or "bsd" in estado["plataforma"]):
                salvar_leitura(dados, campo, None, "nao_aplicavel")
            else:
                atributo = ATRIBUTOS_REDE[campo]
                valor = getattr(atual, atributo, None)
                antes = getattr(contadores_antes, atributo, None)
                salvar_taxa_rede(dados, campo, valor, antes, intervalo)

        if atual is not None:
            novos_anteriores[nome] = {"contadores": atual, "horario": agora}
        linhas.append(dados)

    estado["anteriores"] = novos_anteriores
    if len(linhas) == 0:
        dados = nova_linha_metricas(colunas_interface_rede(estado), horario, mac)
        if status_io == "ok" and status_interfaces == "ok":
            dados["COLETA_STATUS"] = "nenhuma_interface"
            status = "indisponivel"
        else:
            dados["COLETA_STATUS"] = "fonte_incompleta"
            status = status_io if status_io != "ok" else status_interfaces
        for campo in campos_rede(estado):
            salvar_leitura(dados, campo, None, status)
        linhas.append(dados)
    return linhas


# JUNÇÃO DOS DADOS
