# Coleta de cpu: preparação, cabeçalho e leitura das métricas.
import psutil as p
import time
from utils.coleta import numero_valido, salvar_leitura


METRICAS_SELECIONAVEIS = [
    "cpu_percent", "cpu_percent_percpu", "cpu_user", "cpu_system", "cpu_idle",
    "cpu_iowait", "cpu_interrupts_per_second", "cpu_context_switches_per_second",
    "cpu_freq_current",
]

INVENTARIO_CPU = ["cpu_count_logical", "cpu_count_physical", "cpu_freq_min", "cpu_freq_max"]

IDS_CPU = METRICAS_SELECIONAVEIS + INVENTARIO_CPU

MODOS_CPU = {
    "cpu_user": "user", "cpu_system": "system",
    "cpu_idle": "idle", "cpu_iowait": "iowait",
}

TAXAS_CPU = {
    "cpu_interrupts_per_second": "interrupts",
    "cpu_context_switches_per_second": "ctx_switches",
}

#Prepara as escolhas
def preparar_coleta_cpu(selecionadas, fonte=p, relogio=time.monotonic):
    #chama uma vez só
    quantidade = fonte.cpu_count(logical=True)
    if not isinstance(quantidade, int) or quantidade < 1:
        raise RuntimeError("Não foi possível identificar as CPUs lógicas para montar o CSV.")

    escolhas = []
    for nome in selecionadas:
        if nome in IDS_CPU and nome not in escolhas:
            escolhas.append(nome)

    # Dependências obg do coletor
    for nome in ["cpu_percent", "cpu_percent_percpu"]:
        if nome not in escolhas:
            escolhas.append(nome)

    colunas_por_cpu = []
    for indice in range(quantidade):
        colunas_por_cpu.append(f"cpu_logical_{indice}_percent")

    return {
        "selecionadas": escolhas,
        "quantidade": quantidade,
        "colunas_por_cpu": colunas_por_cpu,
        "aquecidas": [],
        "contadores_anteriores": None,
        "horario_anterior": None,
        "fonte": fonte,
        "relogio": relogio,
    }



# Monta o cabeçalho CPU do CSV
def colunas_cpu(estado):
    # Uma opção "por CPU lógica" pode gerar várias colunas no CSV
    valores = []
    for identificador in METRICAS_SELECIONAVEIS:
        if identificador in estado["selecionadas"] and identificador != "cpu_percent_percpu":
            valores.append(identificador)

    valores.extend(estado["colunas_por_cpu"])
    valores.append("cpu_count_logical")
    for identificador in INVENTARIO_CPU:
        if identificador in estado["selecionadas"] and identificador != "cpu_count_logical":
            valores.append(identificador)

    colunas = valores.copy()
    for nome in valores:
        colunas.append(f"{nome}_status")
    colunas.extend(["cpu_interval_seconds", "cpu_schema_version"])
    return colunas


# Verifica valores se da par usar


# Organiza uma rodada de captura
def coletar_cpu(estado):
    dados = {}
    for coluna in colunas_cpu(estado):
        dados[coluna] = None
    dados["cpu_schema_version"] = "cpu-1"
    agora_rodada = estado["relogio"]()
    if estado["horario_anterior"] is not None:
        intervalo_rodada = agora_rodada - estado["horario_anterior"]
        if intervalo_rodada > 0:
            dados["cpu_interval_seconds"] = intervalo_rodada
    estado["horario_anterior"] = agora_rodada

    try:
        salvar_leitura(dados, "cpu_count_logical", estado["fonte"].cpu_count(logical=True))
    except (OSError, NotImplementedError):
        salvar_leitura(dados, "cpu_count_logical", None, "erro_fonte")
    if dados["cpu_count_logical"] != estado["quantidade"]:
        for coluna in colunas_cpu(estado):
            if coluna.endswith("_status") and coluna != "cpu_count_logical_status":
                dados[coluna] = "topologia_alterada"
        estado["aquecidas"].clear()
        estado["contadores_anteriores"] = None
        return dados

    # Cada etapa consulta uma fonte. Não precisa esperar por metrica
    coletar_percentuais(estado, dados, por_cpu=False)
    coletar_percentuais(estado, dados, por_cpu=True)
    coletar_modos(estado, dados)
    coletar_taxas(estado, dados)
    coletar_frequencias(estado, dados)
    coletar_inventario_fisico(estado, dados)
    return dados


# Guarda o valor e status
# Falha fica vazia, não vira zero


# Lê o uso total ou cada CPU lógica e descarta a primeira leitura
def coletar_percentuais(estado, dados, por_cpu):
    if por_cpu:
        grupo = "por_cpu"
        colunas = estado["colunas_por_cpu"]
    else:
        grupo = "total"
        colunas = ["cpu_percent"]

    try:
        if por_cpu:
            valores = estado["fonte"].cpu_percent(interval=None, percpu=True)
        else:
            valores = [estado["fonte"].cpu_percent(interval=None)]
        if len(valores) != len(colunas):
            raise ValueError("Quantidade de CPUs mudou durante a leitura")

        # A primeira consulta apenas cria a referência do psutil
        pronta = grupo in estado["aquecidas"]
        for indice in range(len(colunas)):
            valor = valores[indice]
            if not pronta:
                status = "aquecendo"
            elif not numero_valido(valor) or not 0 <= valor <= 100:
                status = "indisponivel"
            else:
                status = "ok"
            salvar_leitura(dados, colunas[indice], valor, status)
        if grupo not in estado["aquecidas"]:
            estado["aquecidas"].append(grupo)
    except (OSError, ValueError, AttributeError, NotImplementedError):
        for coluna in colunas:
            salvar_leitura(dados, coluna, None, "erro_fonte")
        if grupo in estado["aquecidas"]:
            estado["aquecidas"].remove(grupo)


# Uma única consulta dá usuário, sistema, ociosa e espera por I/O
def coletar_modos(estado, dados):
    modos = []
    for nome in MODOS_CPU:
        if nome in estado["selecionadas"]:
            modos.append(nome)
    if modos:
        try:
            tempos = estado["fonte"].cpu_times_percent(interval=None)
            pronta = "modos" in estado["aquecidas"]
            for nome in modos:
                valor = getattr(tempos, MODOS_CPU[nome], None)
                if valor is None:
                    status = "indisponivel"
                elif not pronta:
                    status = "aquecendo"
                else:
                    status = "ok"
                if pronta and valor is not None and (not numero_valido(valor) or not 0 <= valor <= 100):
                    status = "indisponivel"
                salvar_leitura(dados, nome, valor, status)
            if "modos" not in estado["aquecidas"]:
                estado["aquecidas"].append("modos")
        except (OSError, ValueError, AttributeError, NotImplementedError):
            for nome in modos:
                salvar_leitura(dados, nome, None, "erro_fonte")
            if "modos" in estado["aquecidas"]:
                estado["aquecidas"].remove("modos")


# Compara contadores de duas rodadas e divide os novos eventos pelo tempo
def coletar_taxas(estado, dados):
    taxas = []
    for nome in TAXAS_CPU:
        if nome in estado["selecionadas"]:
            taxas.append(nome)
    if taxas:
        try:
            stats = estado["fonte"].cpu_stats()
            agora = estado["relogio"]()
            anterior = estado["contadores_anteriores"]
            intervalo = None
            if anterior is not None:
                intervalo = agora - anterior["horario"]
            for nome in taxas:
                valor = getattr(stats, TAXAS_CPU[nome], None)
                antes = None
                if anterior is not None:
                    antes = getattr(anterior["contadores"], TAXAS_CPU[nome], None)
                if valor is None or not numero_valido(valor):
                    salvar_leitura(dados, nome, None, "indisponivel")
                elif anterior is None or antes is None or not numero_valido(antes):
                    salvar_leitura(dados, nome, None, "aquecendo")
                # Primeiro verifica se existe um intervalo depois compara com zero.
                elif intervalo is None or intervalo <= 0:
                    salvar_leitura(dados, nome, None, "intervalo_invalido")
                elif valor < antes:
                    salvar_leitura(dados, nome, None, "contador_reiniciado")
                else:
                    # Ex: 100 novos eventos em 2 s = 50 eventos/s
                    novos_eventos = valor - antes
                    eventos_por_segundo = novos_eventos / intervalo
                    salvar_leitura(dados, nome, eventos_por_segundo)
            estado["contadores_anteriores"] = {"horario": agora, "contadores": stats}
        except (OSError, ValueError, AttributeError, NotImplementedError):
            for nome in taxas:
                salvar_leitura(dados, nome, None, "erro_fonte")
            estado["contadores_anteriores"] = None


# Lê as frequências escolhidas e trata fonte indisponível
def coletar_frequencias(estado, dados):
    atributos = {
        "cpu_freq_current": "current",
        "cpu_freq_min": "min",
        "cpu_freq_max": "max",
    }
    frequencias = []
    for nome in atributos:
        if nome in estado["selecionadas"]:
            frequencias.append(nome)
    if frequencias:
        try:
            freq = estado["fonte"].cpu_freq()
            for coluna in frequencias:
                valor = getattr(freq, atributos[coluna], None)
                if valor is not None and numero_valido(valor) and valor > 0:
                    salvar_leitura(dados, coluna, valor)
                else:
                    salvar_leitura(dados, coluna, None, "indisponivel")
        except (OSError, ValueError, AttributeError, NotImplementedError):
            for coluna in frequencias:
                salvar_leitura(dados, coluna, None, "erro_fonte")

# Preserva a quantidade de núcleos físicos
def coletar_inventario_fisico(estado, dados):
    if "cpu_count_physical" in estado["selecionadas"]:
        try:
            salvar_leitura(dados, "cpu_count_physical", estado["fonte"].cpu_count(logical=False))
        except (OSError, NotImplementedError):
            salvar_leitura(dados, "cpu_count_physical", None, "erro_fonte")

