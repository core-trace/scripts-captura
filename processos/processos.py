import psutil as p
from utils.coleta import numero_valido, salvar_leitura

COLUNAS_PROCESSOS = [
    "TIMESTAMP",
    "ENDERECO_MAC",
    "PID",
    "NOME",
    "P_CPU_PERCENT",
    "P_MEMORIA_PERCENT",
    "P_CPU_PERCENT_STATUS",
    "P_MEMORIA_PERCENT_STATUS",
    
]

ATRIBUTOS = [
    "pid",
    "name",
    "cpu_percent",
    "memory_percent",
    "create_time",
]

IDS_PROCESSOS = [
    "p_cpu_porcent",
    "p_memory_porcent"
]

# Estado usado quando chamamos com a assinatura antiga.
_ESTADO = {"aquecidos": {}, "limite": 50}


def preparar_coleta_processos(selecionadas=None, limite=50):
    """Cria o estado de aquecimento por PID, como em cpu/ram.

    `selecionadas` é aceito só para manter a mesma assinatura das outras
    capturas; aqui as duas métricas sempre são coletadas.
    """
    return {"aquecidos": {}, "limite": limite}


def coletar_processos(timestamp, endereco_mac, limite=50, estado=None):
    # [REAPROVEITADO] a assinatura antiga continua funcionando.
    estado = estado if estado is not None else _ESTADO
    estado["limite"] = limite

    linhas = []
    pids_vistos = set()
    aquecidos = estado["aquecidos"]

    # [REAPROVEITADO] o mesmo process_iter(ATRIBUTOS) de antes.
    for proc in p.process_iter(ATRIBUTOS):
        try:
            info = proc.info
            pid = info.get("pid")
            nome = info.get("name")
            create_time = info.get("create_time")

            if pid is None:
                continue

            pids_vistos.add(pid)

            if nome is None or not str(nome).strip():
                continue

            # [REAPROVEITADO] montagem original da linha.
            linha = {
                "TIMESTAMP": timestamp,
                "ENDERECO_MAC": endereco_mac,
                "PID": pid,
                "NOME": nome,
            }

            ja_aquecido = (
                pid in aquecidos and aquecidos[pid] == create_time
            )
            valor_cpu = info.get("cpu_percent")

            if not ja_aquecido:
                # 1ª leitura do psutil só cria a referência.
                salvar_leitura(
                    linha, "P_CPU_PERCENT", valor_cpu, "aquecendo"
                )
            elif numero_valido(valor_cpu) and valor_cpu >= 0:
                # CPU de processo pode passar de 100% em multinúcleo.
                salvar_leitura(
                    linha, "P_CPU_PERCENT", round(valor_cpu, 2)
                )
            else:
                salvar_leitura(
                    linha, "P_CPU_PERCENT", None, "indisponivel"
                )

            valor_memoria = info.get("memory_percent")
            if numero_valido(valor_memoria) and 0 <= valor_memoria <= 100:
                salvar_leitura(
                    linha, "P_MEMORIA_PERCENT", round(valor_memoria, 2)
                )
            else:
                salvar_leitura(
                    linha, "P_MEMORIA_PERCENT", None, "indisponivel"
                )

            aquecidos[pid] = create_time
            linhas.append(linha)

        # [REAPROVEITADO] mesmas exceções de antes.
        except (p.NoSuchProcess, p.AccessDenied, p.ZombieProcess):
            continue

    # Não guarda estado de processos que já saíram.
    for pid in list(aquecidos):
        if pid not in pids_vistos:
            del aquecidos[pid]

    # [REAPROVEITADO] mesma ideia de ordenar e limitar.
    # [AJUSTE] a chave agora é P_CPU_PERCENT.
    linhas.sort(
        key=lambda linha: (
            linha["P_CPU_PERCENT"]
            if numero_valido(linha["P_CPU_PERCENT"]) else -1,
            linha["P_MEMORIA_PERCENT"]
            if numero_valido(linha["P_MEMORIA_PERCENT"]) else -1,
            -linha["PID"],
        ),
        reverse=True,
    )

    return linhas[:estado["limite"]]

