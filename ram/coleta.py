# Coleta de ram: preparação, cabeçalho e leitura das métricas.
import psutil as p
import time
import sys
from pathlib import Path
from utils.coleta import numero_valido, salvar_leitura


IDS_RAM = [
    "ram_percent", "ram_available", "ram_total", "ram_oom_kills",
    "ram_used", "ram_free", "ram_cached",
]
ATRIBUTOS_RAM = {
    "ram_percent": "percent",
    "ram_available": "available",
    "ram_total": "total",
    "ram_used": "used",
    "ram_free": "free",
    "ram_cached": "cached",
}


def ler_oom_linux():
    # O psutil não tem OOM. Ler do contador do linux
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("OOM disponível apenas nesta fonte Linux.")

    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    linhas = Path("/proc/vmstat").read_text().splitlines()
    for linha in linhas:
        partes = linha.split()
        if partes[0] == "oom_kill":
            return {"boot_id": boot_id, "contador": int(partes[1])}
    raise ValueError("A fonte Linux não forneceu oom_kill.")


def preparar_coleta_ram(selecionadas, fonte=p, ler_oom=ler_oom_linux, relogio=time.monotonic):
    escolhas = []
    for nome in selecionadas:
        if nome in IDS_RAM and nome not in escolhas:
            escolhas.append(nome)

    # Mantém os dados básicos de RAM necessários aos indicadores do produto
    for nome in ["ram_percent", "ram_available", "ram_total", "ram_oom_kills"]:
        if nome not in escolhas:
            escolhas.append(nome)

    return {
        "selecionadas": escolhas,
        "oom_anterior": None,
        "fonte": fonte,
        "ler_oom": ler_oom,
        "relogio": relogio,
    }


def colunas_ram(estado):
    colunas = []
    for nome in IDS_RAM:
        if nome in estado["selecionadas"]:
            colunas.append(nome)
            colunas.append(nome + "_status")
    # Permitem identificar a máquina reiniciada e o período de cada contagem OOM
    colunas.extend(["ram_oom_boot_id", "ram_oom_interval_seconds"])
    return colunas


def coletar_ram(estado):
    dados: dict[str, str | int | float | None] = {}
    for coluna in colunas_ram(estado):
        dados[coluna] = None

    try:
        # Todos os valores de memória vêm da mesma consulta
        memoria = estado["fonte"].virtual_memory()
        for nome in ATRIBUTOS_RAM:
            if nome not in estado["selecionadas"]:
                continue
            atributo = ATRIBUTOS_RAM[nome]
            valor = getattr(memoria, atributo, None)
            if valor is None or not numero_valido(valor) or valor < 0:
                salvar_leitura(dados, nome, None, "indisponivel")
            elif nome == "ram_percent":
                if valor <= 100:
                    salvar_leitura(dados, nome, valor)
                else:
                    salvar_leitura(dados, nome, None, "indisponivel")
            else:
                # GB decimal: 1 GB = 1.000.000.000 bytes
                valor_gb = valor / 1_000_000_000
                salvar_leitura(dados, nome, valor_gb)
    except (OSError, ValueError, AttributeError, NotImplementedError):
        for nome in ATRIBUTOS_RAM:
            if nome in estado["selecionadas"]:
                salvar_leitura(dados, nome, None, "erro_fonte")

    # Uma falha na leitura de memória não impede tentar ler o contador OOM
    coletar_oom(estado, dados)
    return dados


def coletar_oom(estado, dados):
    try:
        atual = estado["ler_oom"]()
        agora = estado["relogio"]()
        contador = atual["contador"]
        boot_id = atual["boot_id"]
        anterior = estado["oom_anterior"]
        if not isinstance(contador, int) or isinstance(contador, bool) or contador < 0 or not boot_id:
            raise ValueError("Leitura OOM inválida.")
        dados["ram_oom_boot_id"] = boot_id

        if anterior is None:
            # Primeira leitura: guarda a referência, sem contar eventos antigos.
            salvar_leitura(dados, "ram_oom_kills", None, "aquecendo")
        elif boot_id != anterior["boot_id"]:
            salvar_leitura(dados, "ram_oom_kills", None, "maquina_reiniciada")
        elif contador < anterior["contador"]:
            salvar_leitura(dados, "ram_oom_kills", None, "contador_reiniciado")
        else:
            intervalo = agora - anterior["horario"]
            if intervalo <= 0:
                salvar_leitura(dados, "ram_oom_kills", None, "intervalo_invalido")
            else:
                # Exemplo: contador 12 antes e 14 agora significa 2 novos registros.
                novos_registros = contador - anterior["contador"]
                salvar_leitura(dados, "ram_oom_kills", novos_registros)
                dados["ram_oom_interval_seconds"] = intervalo

        estado["oom_anterior"] = {
            "contador": contador, "boot_id": boot_id, "horario": agora,
        }
    except NotImplementedError:
        salvar_leitura(dados, "ram_oom_kills", None, "nao_aplicavel")
        estado["oom_anterior"] = None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        salvar_leitura(dados, "ram_oom_kills", None, "erro_fonte")
        # Ao recuperar a fonte, criar outra referência sem contar o período desconhecido.
        estado["oom_anterior"] = None


# COLETA DISCO
