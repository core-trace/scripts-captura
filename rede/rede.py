import math


def ler_valor_rede(registro, campo):
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
    if not math.isfinite(numero) or numero < 0:
        return None
    return numero


def coluna_rede(interface, campo):
    return "rede_" + interface + "_" + campo.removeprefix("net_")


def agrupar_rede(lista):
    # Cada linha geral contém várias interfaces, cada uma em suas próprias colunas.
    grupos = {}
    for registro in lista:
        for coluna in registro:
            if not coluna.startswith("rede_") or not coluna.endswith("_isup"):
                continue
            interface = coluna[len("rede_"):-len("_isup")]
            chave = (registro.get("ENDERECO_MAC"), interface)
            if chave not in grupos:
                grupos[chave] = []
            grupos[chave].append(registro)
    return grupos


def taxa_media_rede(lista, campo, interface=None):
    grupos = agrupar_rede(lista)
    if interface is None:
        if len(grupos) != 1:
            return None
        _, interface = next(iter(grupos))
    selecionados = [chave for chave in grupos if chave[1] == interface]
    if len(selecionados) != 1:
        return None
    registros = grupos[selecionados[0]]
    total = 0
    segundos_totais = 0
    for registro in registros:
        taxa = ler_valor_rede(registro, coluna_rede(interface, campo))
        segundos = ler_valor_rede(registro, coluna_rede(interface, "net_interval_seconds"))
        if taxa is not None and segundos is not None and segundos > 0:
            total += taxa * segundos
            segundos_totais += segundos
    if segundos_totais == 0:
        return None
    return total / segundos_totais


def exibir_rede(lista):
    grupos = agrupar_rede(lista)
    if len(grupos) == 0:
        print("Rede: o CSV geral não contém colunas de interfaces.")
        return
    campos = {
        "net_bytes_recv": ("Dados recebidos", "bytes/s"),
        "net_bytes_sent": ("Dados enviados", "bytes/s"),
        "net_errin": ("Erros de recebimento", "eventos/s"),
        "net_errout": ("Erros de envio", "eventos/s"),
        "net_dropin": ("Descartes na entrada", "pacotes/s"),
        "net_dropout": ("Descartes na saída", "pacotes/s"),
        "net_packets_recv": ("Pacotes recebidos", "pacotes/s"),
        "net_packets_sent": ("Pacotes enviados", "pacotes/s"),
    }
    for chave, registros in grupos.items():
        mac, interface = chave
        print(f"\nREDE · {mac} · {interface}")
        # Receber registros em ordem temporal. O estado usa a última linha, sem média.
        ultima = registros[-1]
        estado = ler_valor_rede(ultima, coluna_rede(interface, "net_isup"))
        if estado == 1:
            print("Estado na última coleta: ativa")
        elif estado == 0:
            print("Estado na última coleta: inativa")
        else:
            print("Estado na última coleta: indisponível")
        for campo, (nome, unidade) in campos.items():
            coluna = coluna_rede(interface, campo)
            if coluna not in ultima:
                continue
            valor = taxa_media_rede(registros, campo, interface)
            if valor is not None:
                print(f"{nome}: {valor:.2f} {unidade}")
            elif all(item.get(coluna + "_status") == "nao_aplicavel" for item in registros):
                print(f"{nome}: Não aplicável")
            else:
                print(f"{nome}: Sem dados válidos")
