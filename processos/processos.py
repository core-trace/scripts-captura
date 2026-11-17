import psutil

COLUNAS_PROCESSOS = [
    "TIMESTAMP",
    "ENDERECO_MAC",
    "PID",
    "NOME",
    "USUARIO",
    "CPU_PERCENT",
    "MEMORIA_PERCENT",
    "CRIACAO_PROCESSO",
    "THREADS",
    "RSS",
    "STATUS"
]

ATRIBUTOS = ["pid", "name", "username", "cpu_percent", "memory_percent","create_time","num_threads","status"]


def coletar_processos(timestamp, endereco_mac, limite=50):
    linhas = []

    # process_iter reaproveita os objetos entre as chamadas, então o cpu_percent
    # só fica válido a partir da 2ª rodada (na 1ª vem 0.0)
    for proc in psutil.process_iter(ATRIBUTOS):
        try:
            info = proc.info

            linhas.append({
                "TIMESTAMP": timestamp,
                "ENDERECO_MAC": endereco_mac,
                "PID": info["pid"],
                "NOME": info["name"],
                "USUARIO": info["username"],
                "CPU_PERCENT": info["cpu_percent"],
                "MEMORIA_PERCENT": round(info["memory_percent"] or 0, 2),
                "CRIACAO_PROCESSO": proc.info["create_time"],
                "THREADS": proc.info["num_threads"],
                "RSS": proc.memory.info().rss,
                "STATUS": proc.info["status"]
            })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Grava só os N mais pesados, senão seriam centenas de linhas por segundo
    linhas.sort(key=lambda linha: linha["CPU_PERCENT"] or 0, reverse=True)

    return linhas[:limite]
