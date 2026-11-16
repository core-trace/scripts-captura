"""Gerador do cenario "Black Friday" para testar a esteira Silver + Gold localmente.

Esse script NAO captura nada de verdade (nao usa psutil, nao sobe nada pra AWS).
Ele fabrica linhas de CSV com o MESMO formato que o captura.py real gera - mesmas
colunas, mesmo jeito de marcar o "_status" ao lado de cada metrica - simulando
10 servidores durante uma Black Friday. Depois manda esses CSVs pela Silver e
pela Gold originais (sem alterar nenhuma das duas) usando o mesmo truque de
pasta local que o teste_local.py ja usa, e confere se o JSON final faz sentido.

Importante: isso prova que a esteira Silver -> Gold funciona com dados no
formato certo. Nao aciona a api-java, nem Jira, nem Slack - isso fica pra quando
o grupo subir esse mesmo resultado pro S3 de verdade.
"""

import csv
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from teste_local import testar_etl  # reaproveita o "S3 de mentira" local
from processos.processos import COLUNAS_PROCESSOS

# Semente fixa: assim o jitter sai igual em toda execucao, o que facilita
# comparar dois resultados ou depurar um problema sem ele "mudar sozinho".
ALEATORIO = random.Random(20261127)

DIA = "2026-11-27"
INICIO = datetime(2026, 11, 27, 18, 45)
FIM = datetime(2026, 11, 27, 20, 30)

# So as colunas que realmente importam pra esse cenario entram no CSV.
# O captura.py de verdade gera muito mais colunas (cpu_user, ram_total,
# disco_io_0_*, rede_eth0_errin, etc.), mas a Silver e a Gold so reclamam
# de uma coluna quando ela EXISTE e esta mal formada - coluna que nao
# existe e simplesmente ignorada. Por isso dar so o que move o cenario
# (cpu, ram, swap, carga, disco, rede) ja e um CSV valido e realista.
COLUNAS_METRICAS = [
    "TIMESTAMP", "ENDERECO_MAC",
    "TOKEN_INSTALACAO", "TOKEN_PERFIL_SERVIDOR",
    "cpu_percent", "cpu_percent_status",
    "ram_percent", "ram_percent_status",
    "swap_percent", "swap_percent_status",
    "load_avg_15", "load_avg_15_status",
    "disco_volumes_status",
    "disco_volume_0_montagem", "disco_volume_0_dispositivo", "disco_volume_0_tipo_sistema",
    "disco_volume_0_coleta_status",
    "disco_volume_0_percent", "disco_volume_0_percent_status",
    "rede_coleta_status",
    "rede_eth0_bytes_recv", "rede_eth0_bytes_recv_status",
    "rede_eth0_bytes_sent", "rede_eth0_bytes_sent_status",
]


def minuto_do_dia(hora, minuto):
    # Usado so pra poder escrever as "ancoras" da curva de um jeito legivel (ex: 19:30).
    return hora * 60 + minuto


def interpolar(ancoras, minuto_atual):
    # ancoras: lista de (minuto_do_dia, valor) ja ordenada do mais cedo pro mais tarde.
    # Antes da primeira ancora ou depois da ultima, mantem o valor da ponta mais proxima.
    if minuto_atual <= ancoras[0][0]:
        return ancoras[0][1]
    if minuto_atual >= ancoras[-1][0]:
        return ancoras[-1][1]
    for (minuto_a, valor_a), (minuto_b, valor_b) in zip(ancoras, ancoras[1:]):
        if minuto_a <= minuto_atual <= minuto_b:
            fracao = (minuto_atual - minuto_a) / (minuto_b - minuto_a)
            return valor_a + (valor_b - valor_a) * fracao
    return ancoras[-1][1]


def curva(ancoras_horario):
    # Recebe ancoras como (hora, minuto, valor) e devolve uma funcao que,
    # dado um horario, calcula o valor interpolado entre as ancoras.
    ancoras = [(minuto_do_dia(hora, minuto), valor) for hora, minuto, valor in ancoras_horario]

    def calcular(timestamp):
        return interpolar(ancoras, minuto_do_dia(timestamp.hour, timestamp.minute))

    return calcular


def estavel(base, amplitude):
    # Fica balancando em torno da base, sem seguir nenhuma tendencia de subida.
    def calcular(timestamp):
        return base + ALEATORIO.uniform(-amplitude, amplitude)

    return calcular


# Empresa iFood (database/script.sql) e os tokens de cada perfil de servidor.
# Os 3 perfis tem o mesmo token de instalacao (mesma empresa), mas cada um
# tem seu proprio token de perfil - e por isso seus proprios limites.
TOKEN_INSTALACAO_IFOOD = "123"
TOKENS_PERFIL = {
    "Carrinho": "321",
    "Financeiro": "322",
    "Catálogo": "323",
}

# 10 servidores do cenario, com MAC e perfil conforme database/script.sql
# (perfil_servidor 1=Carrinho, 2=Financeiro, 3=Catalogo).
SERVIDORES = [
    {
        "nome": "pay-api-04", "mac": "AA:BB:CC:DD:EF:04", "perfil": "Financeiro",
        # Critico do Financeiro: cpu 92 / ram 90 (database/script.sql). Cruza os dois as 19:45.
        "cpu": curva([(18, 30, 40), (19, 0, 42), (19, 15, 55), (19, 30, 68),
                       (19, 45, 92), (20, 0, 94), (20, 5, 95), (20, 30, 95)]),
        "ram": curva([(18, 30, 50), (19, 0, 55), (19, 15, 70), (19, 30, 83),
                       (19, 45, 91), (20, 0, 92), (20, 5, 93), (20, 30, 93)]),
        "disco": estavel(50, 6),
    },
    {
        "nome": "cart-api-07", "mac": "AA:BB:CC:DD:EE:07", "perfil": "Carrinho",
        # Critico do Carrinho: cpu 90. Cruza as 20:05.
        "cpu": curva([(18, 30, 44), (19, 0, 44), (19, 15, 63), (19, 30, 79),
                       (19, 45, 83), (20, 0, 88), (20, 5, 92), (20, 30, 94)]),
        "ram": curva([(18, 30, 48), (19, 0, 48), (19, 15, 52), (19, 30, 59),
                       (19, 45, 62), (20, 0, 65), (20, 5, 66), (20, 30, 69)]),
        "disco": estavel(50, 6),
    },
    {
        "nome": "cart-api-12", "mac": "AA:BB:CC:DD:EE:12", "perfil": "Carrinho",
        # Caso de teste da "previsao de limite": sobe so até a atencao (70) e estabiliza,
        # sem NUNCA chegar no critico (90). E de proposito.
        "cpu": curva([(18, 30, 40), (19, 0, 45), (19, 15, 50), (19, 30, 58), (19, 45, 65),
                       (19, 50, 70), (20, 0, 73), (20, 5, 75), (20, 30, 76)]),
        "ram": estavel(57, 2),
        "disco": estavel(50, 6),
    },
    {
        "nome": "pay-db-02", "mac": "AA:BB:CC:DD:EF:02", "perfil": "Financeiro",
        # So RAM sobe, e fica presa na atencao (75) o resto da janela, sem virar critico.
        "cpu": estavel(35, 5),
        "ram": curva([(18, 30, 70), (19, 0, 74), (19, 15, 76), (19, 30, 78),
                       (19, 45, 79), (20, 0, 79), (20, 5, 80), (20, 30, 80)]),
        "disco": estavel(50, 6),
    },
    {
        "nome": "catalog-api-03", "mac": "AA:BB:CC:DD:F0:03", "perfil": "Catálogo",
        "cpu": estavel(45, 3),
        "ram": estavel(50, 3),
        "disco": estavel(50, 6),
        # Esse servidor "para de falar" depois das 18:50 - e o caso de teste de
        # "sem dados recentes". Por isso ele nao gera linha ate o fim da janela.
        "fim_captura": datetime(2026, 11, 27, 18, 50),
    },
    {
        "nome": "cart-api-02", "mac": "AA:BB:CC:DD:EE:02", "perfil": "Carrinho",
        "cpu": estavel(40, 5), "ram": estavel(50, 5), "disco": estavel(50, 6),
    },
    {
        "nome": "cart-api-09", "mac": "AA:BB:CC:DD:EE:09", "perfil": "Carrinho",
        "cpu": estavel(40, 5), "ram": estavel(50, 5), "disco": estavel(50, 6),
    },
    {
        "nome": "pay-api-06", "mac": "AA:BB:CC:DD:EF:06", "perfil": "Financeiro",
        "cpu": estavel(40, 3), "ram": estavel(50, 3), "disco": estavel(50, 6),
    },
    {
        "nome": "catalog-api-05", "mac": "AA:BB:CC:DD:F0:05", "perfil": "Catálogo",
        "cpu": estavel(35, 5), "ram": estavel(50, 5),
        # Disco um pouco mais ocupado que os outros so pra variar o cenario -
        # ainda dentro da faixa normal (bem abaixo da atencao, que e 80).
        "disco": curva([(18, 30, 60), (20, 30, 77)]),
    },
    {
        "nome": "catalog-api-08", "mac": "AA:BB:CC:DD:F0:08", "perfil": "Catálogo",
        "cpu": estavel(35, 5), "ram": estavel(50, 5), "disco": estavel(50, 6),
    },
]

# Os dois processos da tela de "Processos" do pay-api-04, subindo junto com
# a transicao pra critico do proprio servidor.
PROCESSOS_PAY_API_04 = [
    {
        "pid": 4012, "nome": "java", "usuario": "appuser",
        "cpu": curva([(18, 30, 40), (19, 45, 92), (20, 30, 98)]),
        "memoria": curva([(18, 30, 35), (19, 45, 80), (20, 30, 90)]),
    },
    {
        "pid": 6220, "nome": "worker", "usuario": "appuser",
        "cpu": curva([(18, 30, 30), (19, 45, 80), (20, 30, 94)]),
        "memoria": curva([(18, 30, 25), (19, 45, 60), (20, 30, 75)]),
    },
]


def gerar_horarios():
    # Uma captura por minuto, de 18:45 a 20:30 (fim incluso).
    horarios = []
    atual = INICIO
    while atual <= FIM:
        horarios.append(atual)
        atual += timedelta(minutes=1)
    return horarios


def linha_metricas(servidor, horario, swap, carga15, disco):
    return {
        "TIMESTAMP": horario.strftime("%Y-%m-%d %H:%M:%S"),
        "ENDERECO_MAC": servidor["mac"],
        "TOKEN_INSTALACAO": TOKEN_INSTALACAO_IFOOD,
        "TOKEN_PERFIL_SERVIDOR": TOKENS_PERFIL[servidor["perfil"]],
        "cpu_percent": round(servidor["cpu"](horario), 2),
        "cpu_percent_status": "ok",
        "ram_percent": round(servidor["ram"](horario), 2),
        "ram_percent_status": "ok",
        "swap_percent": swap,
        "swap_percent_status": "ok",
        "load_avg_15": carga15,
        "load_avg_15_status": "ok",
        "disco_volumes_status": "ok",
        "disco_volume_0_montagem": "/",
        "disco_volume_0_dispositivo": "/dev/sda1",
        "disco_volume_0_tipo_sistema": "ext4",
        "disco_volume_0_coleta_status": "ok",
        "disco_volume_0_percent": round(disco, 2),
        "disco_volume_0_percent_status": "ok",
        "rede_coleta_status": "ok",
        # Tráfego de rede fica baixo e parado de propósito: o limite critico (30 bytes/s)
        # e trivialmente estourado por qualquer trafego real, entao nao faz parte do cenario.
        "rede_eth0_bytes_recv": 2.5,
        "rede_eth0_bytes_recv_status": "ok",
        "rede_eth0_bytes_sent": 2.5,
        "rede_eth0_bytes_sent_status": "ok",
    }


def gerar_metricas():
    linhas = []
    horarios = gerar_horarios()
    for servidor in SERVIDORES:
        fim_captura = servidor.get("fim_captura", FIM)
        for horario in horarios:
            if horario > fim_captura:
                continue
            swap = round(ALEATORIO.uniform(0.5, 4.5), 2)
            carga15 = round(ALEATORIO.uniform(0.5, 2.0), 2)
            disco = servidor["disco"](horario)
            linhas.append(linha_metricas(servidor, horario, swap, carga15, disco))
    return linhas


def gerar_processos():
    mac_pay_api_04 = next(s["mac"] for s in SERVIDORES if s["nome"] == "pay-api-04")
    linhas = []
    for horario in gerar_horarios():
        for processo in PROCESSOS_PAY_API_04:
            linhas.append({
                "TIMESTAMP": horario.strftime("%Y-%m-%d %H:%M:%S"),
                "ENDERECO_MAC": mac_pay_api_04,
                "PID": processo["pid"],
                "NOME": processo["nome"],
                "USUARIO": processo["usuario"],
                "CPU_PERCENT": round(processo["cpu"](horario), 2),
                "MEMORIA_PERCENT": round(processo["memoria"](horario), 2),
            })
    return linhas


def salvar_csv(caminho, linhas, colunas):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=colunas)
        escritor.writeheader()
        escritor.writerows(linhas)


def conferir_resultado(pasta):
    import json

    print("\nConferencia do cenario Black Friday:")

    total_lotes = len(list((pasta / "client" / "lotes").rglob("*.json")))
    total_resumos = len(list((pasta / "client" / "resumos").rglob("*.json")))
    print(f"  {total_lotes} lote(s) e {total_resumos} resumo(s) diario(s) gerados em client/.")

    mac_pay = next(s["mac"] for s in SERVIDORES if s["nome"] == "pay-api-04")
    caminho_pay = pasta / "client" / "resumos" / f"maquina={mac_pay}" / f"data={DIA}" / "resumo.json"
    if caminho_pay.exists():
        resumo = json.loads(caminho_pay.read_text(encoding="utf-8"))["resumos"]
        cpu = resumo.get("cpu", {}).get("total", {})
        ram = resumo.get("ram", {}).get("uso", {})
        cpu_maximo = cpu.get("maximo")
        ram_maximo = ram.get("maximo")
        print(f"  pay-api-04 (Financeiro): cpu_percent maximo={cpu_maximo} (media={cpu.get('media')}); "
              f"ram_percent maximo={ram_maximo} (media={ram.get('media')})")
        cruzou = cpu_maximo is not None and ram_maximo is not None and cpu_maximo >= 92 and ram_maximo >= 90
        print(f"  -> limite critico Financeiro e cpu>=92 / ram>=90. "
              f"{'Cruzou os dois.' if cruzou else 'NAO cruzou os dois - revisar o gerador.'}")
    else:
        print("  pay-api-04: resumo nao encontrado em", caminho_pay)

    mac_catalog = next(s["mac"] for s in SERVIDORES if s["nome"] == "catalog-api-03")
    caminho_catalog = pasta / "client" / "resumos" / f"maquina={mac_catalog}" / f"data={DIA}" / "resumo.json"
    if caminho_catalog.exists():
        resumo = json.loads(caminho_catalog.read_text(encoding="utf-8"))["resumos"]
        print(f"  catalog-api-03 (Catalogo): campo 'fim' do resumo = {resumo.get('fim')}")
        print("  -> esperado por volta de 2026-11-27 18:50:00 (depois disso o servidor parou de mandar dados)")
    else:
        print("  catalog-api-03: resumo nao encontrado em", caminho_catalog)


def main():
    pasta = RAIZ / "teste-local" / f"black-friday_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')}"

    print("Gerando cenario Black Friday (dados sinteticos, sem AWS).")
    print("Pasta dos resultados:", pasta)

    metricas = gerar_metricas()
    processos = gerar_processos()

    salvar_csv(pasta / "raw" / "metricas_black_friday.csv", metricas, COLUNAS_METRICAS)
    salvar_csv(pasta / "raw" / "processos_black_friday.csv", processos, COLUNAS_PROCESSOS)

    print(f"{len(metricas)} linha(s) de metricas e {len(processos)} linha(s) de processos geradas.")

    testar_etl(pasta)
    conferir_resultado(pasta)


if __name__ == "__main__":
    main()
