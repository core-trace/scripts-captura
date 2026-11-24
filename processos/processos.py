import psutil
from datetime import datetime

COLUNAS_PROCESSOS = [
    "TIMESTAMP",
    "ENDERECO_MAC",
    "PID",
    "NOME",
    "USUARIO",
    "P_CPU_PERCENT",
    "P_MEMORIA_PERCENT",
    "STATUS"
    
]

ATRIBUTOS = ["pid", "name", "username", "p_cpu_percent", "p_memory_percent"]


def coletar_processos(timestamp, endereco_mac, limite=50):
    linhas = []

    # process_iter reaproveita os objetos entre as chamadas, então o cpu_percent
    # só fica válido a partir da 2ª rodada (na 1ª vem 0.0)
    for proc in psutil.process_iter(ATRIBUTOS):
        try:
            info = proc.info
            mem = info.get("memory_info")
            rss_bytes = mem.rss if mem else None;

            create_time = info.get("create_time")
            if create_time is not None:
                create_time = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")

            linhas.append({
                "TIMESTAMP": timestamp,
                "ENDERECO_MAC": endereco_mac,
                "PID": info["pid"],
                "NOME": info["name"],
                "USUARIO": info.get("username"),
                "P_CPU_PERCENT": info["cpu_percent"],
                "P_MEMORIA_PERCENT": round(info["memory_percent"] or 0, 2)
            })

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Grava só os N mais pesados, senão seriam centenas de linhas por segundo
    linhas.sort(key=lambda linha: linha["CPU_PERCENT"] or 0, reverse=True)

    return linhas[:limite]
