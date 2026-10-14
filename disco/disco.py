import utils.exibicao as ex
import math


def ler_valor_disco(registro, campo):
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


def extrair_registros_disco(lista, identificador):
    # Converte as colunas gerais em registros internos por volume ou dispositivo
    registros = []
    grupo = "disco_volume_" if identificador == "MONTAGEM" else "disco_io_"
    final = "_montagem" if identificador == "MONTAGEM" else "_dispositivo"
    for linha in lista:
        if identificador in linha:
            # As funções de apresentação também passam registros internos entre si
            registros.append(linha)
            continue
        for coluna in linha:
            if not coluna.startswith(grupo) or not coluna.endswith(final):
                continue
            prefixo = coluna[:-len(final)] + "_"
            registro = {"TIMESTAMP": linha.get("TIMESTAMP"),
                        "ENDERECO_MAC": linha.get("ENDERECO_MAC"),
                        "DISPOSITIVO": linha.get(prefixo + "dispositivo")}
            if identificador == "MONTAGEM":
                registro["MONTAGEM"] = linha.get(prefixo + "montagem")
            for nome, valor in linha.items():
                if nome.startswith(prefixo):
                    campo = nome[len(prefixo):]
                    if campo not in ["montagem", "dispositivo", "tipo_sistema", "coleta_status"]:
                        registro["disk_" + campo] = valor
            registros.append(registro)
    return registros


def agrupar_disco(lista, identificador):
    # Nunca juntar dois servidores, volumes ou dispositivos na mesma média
    grupos = {}
    for registro in extrair_registros_disco(lista, identificador):
        alvo = registro.get(identificador)
        if alvo is None or alvo == "":
            continue
        mac = registro.get("ENDERECO_MAC")
        dispositivo = registro.get("DISPOSITIVO")
        chave = (mac, alvo, dispositivo)
        if chave not in grupos:
            grupos[chave] = []
        grupos[chave].append(registro)
    return grupos


def media_disco(lista, campo, identificador="MONTAGEM"):
    grupos = agrupar_disco(lista, identificador)
    if len(grupos) != 1:
        # Receber vários alvos tem q separaar antes de calcular a média
        return None
    soma = 0
    quantidade = 0
    registros = next(iter(grupos.values()))
    for registro in registros:
        valor = ler_valor_disco(registro, campo)
        if valor is not None:
            soma += valor
            quantidade += 1
    if quantidade == 0:
        return None
    return soma / quantidade


def media_disco_percent(lista: list):
    return media_disco(lista, "disk_percent")


def media_disco_livre(lista: list):
    return media_disco(lista, "disk_free")


def media_disco_usado(lista: list):
    return media_disco(lista, "disk_used")


def crescimento_espaco_disco(lista: list):
    # variação percentual do espaço usado não previsão de dias
    grupos = agrupar_disco(lista, "MONTAGEM")
    if len(grupos) != 1:
        return None
    leituras = []
    for registro in next(iter(grupos.values())):
        valor = ler_valor_disco(registro, "disk_used")
        if valor is not None:
            leituras.append(valor)
    if len(leituras) < 2 or leituras[0] == 0:
        return None
    usado_inicio = leituras[0]
    usado_fim = leituras[-1]
    return (usado_fim - usado_inicio) * 100 / usado_inicio


def exibir_valor_disco(nome, valor, unidade=""):
    if valor is None:
        print(f"{nome} Sem dados válidos")
    else:
        ex.resultado(nome, valor, unidade)


def exibir_disco(dados: list):
    grupos = agrupar_disco(dados, "MONTAGEM")
    if len(grupos) == 0:
        print("Disco: o CSV geral não contém colunas de volumes.")
        return
    for chave in grupos:
        mac, montagem, dispositivo = chave
        registros = grupos[chave]
        ex.titulo(f"DISCO · {mac} · {montagem} · {dispositivo}")
        exibir_valor_disco("Ocupação média:", media_disco_percent(registros), "%")
        exibir_valor_disco("Espaço livre médio:", media_disco_livre(registros), "GB")
        exibir_valor_disco("Espaço usado médio:", media_disco_usado(registros), "GB")
        exibir_valor_disco("Uso médio de inodes:", media_disco(registros, "disk_inodes_percent"), "%")
        exibir_valor_disco("Variação do espaço usado:", crescimento_espaco_disco(registros), "%")


def exibir_io_disco(dados: list):
    grupos = agrupar_disco(dados, "DISPOSITIVO")
    if len(grupos) == 0:
        print("Disco: o CSV geral não contém colunas de I/O.")
        return
    campos = {
        "disk_read_bytes": ("Leitura média:", "bytes/s"),
        "disk_write_bytes": ("Escrita média:", "bytes/s"),
        "disk_read_count": ("Operações médias de leitura:", "IOPS"),
        "disk_write_count": ("Operações médias de escrita:", "IOPS"),
        "disk_read_time_ms": ("Tempo estimado de leitura:", "ms"),
        "disk_write_time_ms": ("Tempo estimado de escrita:", "ms"),
        "disk_queue": ("Fila média de I/O:", "operações"),
    }
    for chave in grupos:
        mac, dispositivo, _ = chave
        registros = grupos[chave]
        ex.titulo(f"I/O · {mac} · {dispositivo}")
        for campo in campos:
            nome, unidade = campos[campo]
            valor = media_disco(registros, campo, "DISPOSITIVO")
            exibir_valor_disco(nome, valor, unidade)
