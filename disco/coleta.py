# Coleta de disco
import psutil as p
import time
import sys
import os
from pathlib import Path
from utils.coleta import numero_valido, salvar_leitura, nova_linha_metricas, diferenca_contador


METRICAS_VOLUME = ["disk_percent", "disk_free", "disk_inodes_percent", "disk_used"]
METRICAS_IO = [
    "disk_io_time", "disk_queue", "disk_read_bytes", "disk_write_bytes",
    "disk_read_count", "disk_write_count",
]
IDS_DISCO = METRICAS_VOLUME + METRICAS_IO + ["disk_total"]
CAMPOS_INODES = ["disk_inodes_percent", "disk_inodes_total", "disk_inodes_free", "disk_inodes_available"]


def ler_fila_linux(caminho="/proc/diskstats"):
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("Fila de I/O disponível nesta fonte Linux.")
    contadores = {}
    for linha in Path(caminho).read_text().splitlines():
        partes = linha.split()
        if len(partes) >= 14:
            dispositivo = partes[2]
            contadores[dispositivo] = int(partes[13])
    return contadores


def preparar_coleta_disco(selecionadas, fonte=p, statvfs=None, ler_fila=ler_fila_linux, relogio=time.monotonic):
    escolhas = []
    for nome in selecionadas:
        if nome in IDS_DISCO and nome not in escolhas:
            escolhas.append(nome)
    for nome in ["disk_percent", "disk_free", "disk_inodes_percent", "disk_io_time", "disk_queue"]:
        if nome not in escolhas:
            escolhas.append(nome)
    if statvfs is None:
        statvfs = getattr(os, "statvfs", None)
    montagens, dispositivos = descobrir_alvos_disco(fonte)
    return {
        "selecionadas": escolhas,
        "montagens": montagens,
        "dispositivos": dispositivos,
        "novos_avisados": [],
        "io_anterior": {},
        "fonte": fonte,
        "statvfs": statvfs,
        "ler_fila": ler_fila,
        "relogio": relogio,
    }


def campos_volumes(estado):
    # Capacidade e contagens de inodes acompanham o histórico para a futura previsão
    campos = ["disk_total"]
    for nome in METRICAS_VOLUME:
        if nome in estado["selecionadas"] and nome != "disk_inodes_percent":
            campos.append(nome)
    campos.extend(CAMPOS_INODES)
    return campos


def colunas_volumes(estado):
    colunas = ["TIMESTAMP", "ENDERECO_MAC", "MONTAGEM", "DISPOSITIVO", "TIPO_SISTEMA", "COLETA_STATUS"]
    for campo in campos_volumes(estado):
        colunas.append(campo)
        colunas.append(campo + "_status")
    return colunas


def campos_io(estado):
    campos = []
    for nome in METRICAS_IO:
        if nome not in estado["selecionadas"]:
            continue
        if nome == "disk_io_time":
            # Uma opção na tela, duas opcoes leitura e escrita
            campos.extend(["disk_read_time_ms", "disk_write_time_ms"])
        else:
            campos.append(nome)
    return campos


def colunas_io(estado):
    colunas = ["TIMESTAMP", "ENDERECO_MAC", "DISPOSITIVO", "COLETA_STATUS", "disk_interval_seconds"]
    for campo in campos_io(estado):
        colunas.append(campo)
        colunas.append(campo + "_status")
    return colunas


def descobrir_alvos_disco(fonte):
    # Esses alvos definem o cabeçalho fixo do CSV
    montagens = []
    dispositivos = []
    try:
        for particao in fonte.disk_partitions(all=False):
            if particao.mountpoint not in montagens:
                montagens.append(particao.mountpoint)
    except (OSError, ValueError, AttributeError, NotImplementedError):
        pass
    try:
        dispositivos = list(fonte.disk_io_counters(perdisk=True, nowrap=False) or {})
    except (OSError, ValueError, AttributeError, NotImplementedError):
        pass
    return sorted(montagens), sorted(dispositivos)


def coluna_disco(grupo, indice, campo):
    # Exemplos: disco_volume_0_free e disco_io_0_read_bytes
    return "disco_" + grupo + "_" + str(indice) + "_" + campo.removeprefix("disk_")


def colunas_disco(estado):
    colunas = ["disco_volumes_status", "disco_io_status"]
    for indice, montagem in enumerate(estado["montagens"]):
        prefixo = "disco_volume_" + str(indice) + "_"
        colunas.extend([prefixo + "montagem", prefixo + "dispositivo",
                        prefixo + "tipo_sistema", prefixo + "coleta_status"])
        for campo in campos_volumes(estado):
            nome = coluna_disco("volume", indice, campo)
            colunas.extend([nome, nome + "_status"])
    for indice, dispositivo in enumerate(estado["dispositivos"]):
        prefixo = "disco_io_" + str(indice) + "_"
        colunas.extend([prefixo + "dispositivo", prefixo + "coleta_status",
                        prefixo + "interval_seconds"])
        for campo in campos_io(estado):
            nome = coluna_disco("io", indice, campo)
            colunas.extend([nome, nome + "_status"])
    return colunas


def coletar_disco_geral(estado):
    dados: dict[str, str | int | float | None] = {}
    for coluna in colunas_disco(estado):
        dados[coluna] = None
    # As funções internas continuam calculando por volume e por dispositivo
    volumes = coletar_volumes(estado, "", "")
    operacoes = coletar_io_disco(estado, "", "")
    for grupo, alvos, registros, identificador, campos in [
        ("volume", estado["montagens"], volumes, "MONTAGEM", campos_volumes(estado)),
        ("io", estado["dispositivos"], operacoes, "DISPOSITIVO", campos_io(estado)),
    ]:
        status_geral = "ok"
        for registro in registros:
            alvo = registro[identificador]
            if registro["COLETA_STATUS"] != "ok":
                status_geral = registro["COLETA_STATUS"]
            if alvo and alvo not in alvos:
                aviso = (grupo, alvo)
                if aviso not in estado["novos_avisados"]:
                    print(f"Novo alvo de disco {alvo}: reinicie o agente para incluí-lo no CSV.")
                    estado["novos_avisados"].append(aviso)
        for indice, alvo in enumerate(alvos):
            prefixo = "disco_" + grupo + "_" + str(indice) + "_"
            registro = next((item for item in registros if item[identificador] == alvo), None)

            dados[prefixo + ("montagem" if grupo == "volume" else "dispositivo")] = alvo
            if registro is None:
                status = "indisponivel" if status_geral == "ok" else status_geral
                dados[prefixo + "coleta_status"] = status
                for campo in campos:
                    nome = coluna_disco(grupo, indice, campo)
                    salvar_leitura(dados, nome, None, status)
                continue
            dados[prefixo + "coleta_status"] = registro["COLETA_STATUS"]
            if grupo == "volume":
                dados[prefixo + "dispositivo"] = registro["DISPOSITIVO"]
                dados[prefixo + "tipo_sistema"] = registro["TIPO_SISTEMA"]
            else:
                dados[prefixo + "interval_seconds"] = registro["disk_interval_seconds"]
            for campo in campos:
                nome = coluna_disco(grupo, indice, campo)
                dados[nome] = registro[campo]
                dados[nome + "_status"] = registro[campo + "_status"]
        dados["disco_volumes_status" if grupo == "volume" else "disco_io_status"] = status_geral
    return dados




def marcar_campos_disco(dados, campos, status):
    for campo in campos:
        salvar_leitura(dados, campo, None, status)


def coletar_inodes(estado, dados, montagem):
    if estado["statvfs"] is None:
        marcar_campos_disco(dados, CAMPOS_INODES, "nao_aplicavel")
        return
    try:
        info = estado["statvfs"](montagem)
        total = info.f_files
        livres = info.f_ffree
        disponiveis = info.f_favail
        if total == 0:
            # Alguns sistemas de arquivos não têm uma capacidade fixa de inodes
            marcar_campos_disco(dados, CAMPOS_INODES, "nao_aplicavel")
            return
        if total < 0 or livres < 0 or livres > total or disponiveis < 0 or disponiveis > livres:
            raise ValueError("Contagem de inodes inválida.")
        usados = total - livres
        salvar_leitura(dados, "disk_inodes_percent", usados / total * 100)
        salvar_leitura(dados, "disk_inodes_total", total)
        salvar_leitura(dados, "disk_inodes_free", livres)
        salvar_leitura(dados, "disk_inodes_available", disponiveis)
    except NotImplementedError:
        marcar_campos_disco(dados, CAMPOS_INODES, "nao_aplicavel")
    except (OSError, ValueError, AttributeError, TypeError):
        marcar_campos_disco(dados, CAMPOS_INODES, "erro_fonte")


def coletar_volumes(estado, horario, mac):
    linhas = []
    try:
        particoes = estado["fonte"].disk_partitions(all=False)
    except (OSError, ValueError, AttributeError, NotImplementedError):
        dados = nova_linha_metricas(colunas_volumes(estado), horario, mac)
        dados["COLETA_STATUS"] = "erro_fonte"
        marcar_campos_disco(dados, campos_volumes(estado), "erro_fonte")
        return [dados]

    montagens_vistas = []
    for particao in particoes:
        montagem = particao.mountpoint

        if montagem in montagens_vistas: 
            continue
        montagens_vistas.append(montagem)
        dados = nova_linha_metricas(colunas_volumes(estado), horario, mac)
        dados["MONTAGEM"] = montagem
        dados["DISPOSITIVO"] = particao.device
        dados["TIPO_SISTEMA"] = particao.fstype
        try:
            uso = estado["fonte"].disk_usage(montagem)
            salvar_leitura(dados, "disk_total", uso.total / 1_000_000_000)
            salvar_leitura(dados, "disk_free", uso.free / 1_000_000_000)
            if numero_valido(uso.percent) and 0 <= uso.percent <= 100:
                salvar_leitura(dados, "disk_percent", uso.percent)
            else:
                salvar_leitura(dados, "disk_percent", None, "indisponivel")
            if "disk_used" in estado["selecionadas"]:
                salvar_leitura(dados, "disk_used", uso.used / 1_000_000_000)
        except (OSError, ValueError, AttributeError, TypeError):
            dados["COLETA_STATUS"] = "erro_fonte"
            campos_espaco = ["disk_total", "disk_percent", "disk_free"]
            if "disk_used" in estado["selecionadas"]:
                campos_espaco.append("disk_used")
            marcar_campos_disco(dados, campos_espaco, "erro_fonte")
        coletar_inodes(estado, dados, montagem)
        linhas.append(dados)

    if len(linhas) == 0:
        dados = nova_linha_metricas(colunas_volumes(estado), horario, mac)
        dados["COLETA_STATUS"] = "nenhum_volume"
        marcar_campos_disco(dados, campos_volumes(estado), "indisponivel")
        linhas.append(dados)
    return linhas




def salvar_taxa_disco(dados, campo, atual, antes, intervalo):
    diferenca, status = diferenca_contador(atual, antes)
    if status != "ok" or diferenca is None:
        salvar_leitura(dados, campo, None, status)
    elif intervalo is None or intervalo <= 0:
        salvar_leitura(dados, campo, None, "intervalo_invalido")
    else:
        salvar_leitura(dados, campo, diferenca / intervalo)


def salvar_tempo_disco(dados, campo, tempo, tempo_antes, operacoes, operacoes_antes, intervalo):
    diferenca_tempo, status_tempo = diferenca_contador(tempo, tempo_antes)
    novas_operacoes, status_operacoes = diferenca_contador(operacoes, operacoes_antes)
    if status_tempo != "ok":
        salvar_leitura(dados, campo, None, status_tempo)
    elif status_operacoes != "ok":
        salvar_leitura(dados, campo, None, status_operacoes)
    elif intervalo is None or intervalo <= 0:
        salvar_leitura(dados, campo, None, "intervalo_invalido")
    elif novas_operacoes is None or novas_operacoes == 0:
        # Sem operações, não existe tempo médio para calcular
        salvar_leitura(dados, campo, None, "sem_operacoes")
    elif diferenca_tempo is not None:
        salvar_leitura(dados, campo, diferenca_tempo / novas_operacoes)


def coletar_io_disco(estado, horario, mac):
    try:
        # nowrap=False deixa reconhecer reinício dos contadores em vez de ocultar
        dispositivos = estado["fonte"].disk_io_counters(perdisk=True, nowrap=False)
    except (OSError, ValueError, AttributeError, NotImplementedError):
        linhas = []
        nomes = list(estado["io_anterior"])
        if len(nomes) == 0:
            nomes = [""]
        for nome in nomes:
            dados = nova_linha_metricas(colunas_io(estado), horario, mac)
            dados["DISPOSITIVO"] = nome
            dados["COLETA_STATUS"] = "erro_fonte"
            marcar_campos_disco(dados, campos_io(estado), "erro_fonte")
            linhas.append(dados)
        estado["io_anterior"] = {}
        return linhas

    fila = {}
    status_fila = "ok"
    try:
        fila = estado["ler_fila"]()
    except NotImplementedError:
        status_fila = "nao_aplicavel"
    except (OSError, ValueError, AttributeError, TypeError):
        status_fila = "erro_fonte"
    agora = estado["relogio"]()
    anteriores_novos = {}
    linhas = []
    if dispositivos is None:
        dispositivos = {}

    for nome in dispositivos:
        atual = dispositivos[nome]
        anterior = estado["io_anterior"].get(nome)
        contadores_antes = None
        fila_antes = None
        intervalo = None
        if anterior is not None:
            contadores_antes = anterior["contadores"]
            fila_antes = anterior["fila"]
            intervalo = agora - anterior["horario"]
        dados = nova_linha_metricas(colunas_io(estado), horario, mac)
        dados["DISPOSITIVO"] = nome
        if intervalo is not None and intervalo > 0:
            dados["disk_interval_seconds"] = intervalo

        # Os nomes dos quatro contadores coincidem com os atributos do psutil
        atributos = {
            "disk_read_bytes": "read_bytes", "disk_write_bytes": "write_bytes",
            "disk_read_count": "read_count", "disk_write_count": "write_count",
        }
        for campo in atributos:
            if campo in estado["selecionadas"]:
                atributo = atributos[campo]
                valor = getattr(atual, atributo, None)
                antes = getattr(contadores_antes, atributo, None)
                salvar_taxa_disco(dados, campo, valor, antes, intervalo)

        # Calculamos leitura e escrita de forma separada embora sejam uma opção na tela agrupada
        if "disk_io_time" in estado["selecionadas"]:
            salvar_tempo_disco(dados, "disk_read_time_ms",
                getattr(atual, "read_time", None), getattr(contadores_antes, "read_time", None),
                getattr(atual, "read_count", None), getattr(contadores_antes, "read_count", None), intervalo)
            salvar_tempo_disco(dados, "disk_write_time_ms",
                getattr(atual, "write_time", None), getattr(contadores_antes, "write_time", None),
                getattr(atual, "write_count", None), getattr(contadores_antes, "write_count", None), intervalo)

        if status_fila != "ok":
            salvar_leitura(dados, "disk_queue", None, status_fila)
        else:
            # Tempo ponderado em ms / intervalo em ms = média de operações em andamento
            intervalo_ms = None
            if intervalo is not None:
                intervalo_ms = intervalo * 1000
            salvar_taxa_disco(dados, "disk_queue", fila.get(nome), fila_antes, intervalo_ms)

        anteriores_novos[nome] = {"contadores": atual, "fila": fila.get(nome), "horario": agora}
        linhas.append(dados)

    # Um dispositivo que desapareceu perde sua referência qnd voltar aquece novamente
    estado["io_anterior"] = anteriores_novos
    if len(linhas) == 0:
        dados = nova_linha_metricas(colunas_io(estado), horario, mac)
        dados["COLETA_STATUS"] = "nenhum_dispositivo"
        marcar_campos_disco(dados, campos_io(estado), "indisponivel")
        linhas.append(dados)
    return linhas


# COLETA SWAP
