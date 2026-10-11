import time
import psutil

COLUNAS_REDE = [
    "REDE_BYTES_ENVIADOS_S",
    "REDE_BYTES_RECEBIDOS_S",
    "REDE_PACOTES_ENVIADOS_S",
    "REDE_PACOTES_RECEBIDOS_S",
    "REDE_ERROS",
    "REDE_CONEXOES_ESTABELECIDAS",
]

_anterior = None

def contar_conexoes_estabelecidas():
    try:
        conexoes = psutil.net_connections(kind="inet")
        return sum(1 for c in conexoes if c.status == psutil.CONN_ESTABLISHED)

    except (psutil.AccessDenied, OSError):
        return None


def coletar_rede():
    global _anterior

    agora = time.time()
    io = psutil.net_io_counters()

    dados = {coluna: None for coluna in COLUNAS_REDE}
    dados["REDE_ERROS"] = io.errin + io.errout
    dados["REDE_CONEXOES_ESTABELECIDAS"] = contar_conexoes_estabelecidas()

    if _anterior is not None:
        intervalo = agora - _anterior["tempo"]
        antes = _anterior["io"]

        if intervalo > 0:
            dados["REDE_BYTES_ENVIADOS_S"] = round((io.bytes_sent - antes.bytes_sent) / intervalo, 2)
            dados["REDE_BYTES_RECEBIDOS_S"] = round((io.bytes_recv - antes.bytes_recv) / intervalo, 2)
            dados["REDE_PACOTES_ENVIADOS_S"] = round((io.packets_sent - antes.packets_sent) / intervalo, 2)
            dados["REDE_PACOTES_RECEBIDOS_S"] = round((io.packets_recv - antes.packets_recv) / intervalo, 2)

    _anterior = {"tempo": agora, "io": io}

    return dados
