import psutil as p

from utils.coleta import numero_valido, salvar_leitura


IDS_PROCESSOS = ["p_cpu_percent", "p_memory_percent"]

# O ID usado na API é p_memory_percent, mas a coluna do CSV segue o contrato
# em português (P_MEMORIA_PERCENT).
COLUNAS_POR_METRICA = {
    "p_cpu_percent": "P_CPU_PERCENT",
    "p_memory_percent": "P_MEMORIA_PERCENT",
}

# create_time não vai para o CSV: serve só para detectar PID reaproveitado.
ATRIBUTOS_PROCESSOS = [
    "pid",
    "name",
    "cpu_percent",
    "memory_percent",
    "create_time",
]

LIMITE_PROCESSOS = 50


def preparar_coleta_processos(
    selecionadas,
    fonte=p,
    limite=LIMITE_PROCESSOS
):
    escolhas = []
    for nome in selecionadas:
        if nome in IDS_PROCESSOS and nome not in escolhas:
            escolhas.append(nome)

    # Mesmo comportamento de cpu_percent/ram_percent: as métricas básicas
    # sempre são coletadas, mesmo que a API não as tenha relacionado.
    for nome in IDS_PROCESSOS:
        if nome not in escolhas:
            escolhas.append(nome)

    return {
        "selecionadas": escolhas,
        "aquecidos": {},  # pid -> create_time da instância já aquecida
        "fonte": fonte,
        "limite": limite,
    }


def colunas_processos(estado):
    medidas = [
        COLUNAS_POR_METRICA[nome]
        for nome in IDS_PROCESSOS
        if nome in estado["selecionadas"]
    ]

    colunas = ["TIMESTAMP", "ENDERECO_MAC", "PID", "NOME"]
    colunas.extend(medidas)
    colunas.extend(nome + "_status" for nome in medidas)
    return colunas


# Cabeçalho fixo usado pelo gerador de apresentação.
COLUNAS_PROCESSOS = colunas_processos(
    {"selecionadas": IDS_PROCESSOS}
)


def _valor_para_ordem(linha, coluna):
    valor = linha.get(coluna)
    return valor if numero_valido(valor) else -1


def coletar_processos(estado, timestamp, endereco_mac):
    linhas = []
    pids_vistos = set()
    aquecidos = estado["aquecidos"]

    coluna_cpu = COLUNAS_POR_METRICA["p_cpu_percent"]
    coluna_memoria = COLUNAS_POR_METRICA["p_memory_percent"]

    try:
        processos = estado["fonte"].process_iter(ATRIBUTOS_PROCESSOS)
    except (OSError, AttributeError, NotImplementedError) as erro:
        print(f"[PROCESSOS] Falha ao listar processos: {erro}")
        return linhas

    for proc in processos:
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

            linha = {
                "TIMESTAMP": timestamp,
                "ENDERECO_MAC": endereco_mac,
                "PID": pid,
                "NOME": nome,
            }

            if "p_cpu_percent" in estado["selecionadas"]:
                valor_cpu = info.get("cpu_percent")

                # process_iter reaproveita o objeto: a 1ª leitura só cria
                # a referência. create_time diferente = PID reaproveitado.
                ja_aquecido = (
                    pid in aquecidos and aquecidos[pid] == create_time
                )

                if not ja_aquecido:
                    salvar_leitura(
                        linha, coluna_cpu, valor_cpu, "aquecendo"
                    )
                elif numero_valido(valor_cpu) and valor_cpu >= 0:
                    # CPU de processo pode passar de 100% em máquina com
                    # vários núcleos, então não aplicamos o teto.
                    salvar_leitura(
                        linha, coluna_cpu, round(valor_cpu, 2)
                    )
                else:
                    salvar_leitura(
                        linha, coluna_cpu, None, "indisponivel"
                    )

            if "p_memory_percent" in estado["selecionadas"]:
                valor_memoria = info.get("memory_percent")
                if (
                    numero_valido(valor_memoria)
                    and 0 <= valor_memoria <= 100
                ):
                    salvar_leitura(
                        linha, coluna_memoria, round(valor_memoria, 2)
                    )
                else:
                    salvar_leitura(
                        linha, coluna_memoria, None, "indisponivel"
                    )

            aquecidos[pid] = create_time
            linhas.append(linha)

        except (
            p.NoSuchProcess,
            p.AccessDenied,
            p.ZombieProcess,
            KeyError,
        ):
            continue

    # Não guarda estado de processos que já saíram.
    for pid in list(aquecidos):
        if pid not in pids_vistos:
            del aquecidos[pid]

    # Aquecidos/mais pesados primeiro; PID menor desempata.
    linhas.sort(
        key=lambda linha: (
            _valor_para_ordem(linha, coluna_cpu),
            _valor_para_ordem(linha, coluna_memoria),
            -linha["PID"],
        ),
        reverse=True,
    )

    return linhas[:estado["limite"]]
Resultado do cabeçalho:
COLUNAS_PROCESSOS == [
    "TIMESTAMP",
    "ENDERECO_MAC",
    "PID",
    "NOME",
    "P_CPU_PERCENT",
    "P_MEMORIA_PERCENT",
    "P_CPU_PERCENT_STATUS",
    "P_MEMORIA_PERCENT_STATUS",
]