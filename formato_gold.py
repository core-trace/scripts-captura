"""Organização e resumos das capturas. Não acessa AWS, API ou banco."""
import math
import re

# Esses mapas ligam o nome que usamos no JSON à coluna da Silver e à unidade.
# Na CPU, o nome da métrica do banco é a própria coluna em minúsculas
GRUPOS_CPU = {
    "inventario": {
        "quantidade_logicas": ("CPU_COUNT_LOGICAL", "núcleos"),
        "quantidade_fisicas": ("CPU_COUNT_PHYSICAL", "núcleos")
    },
    "modos": {
        "usuario": ("CPU_USER", "%"),
        "sistema": ("CPU_SYSTEM", "%"),
        "ociosa": ("CPU_IDLE", "%"),
        "espera_io": ("CPU_IOWAIT", "%")
    },
    "frequencia": {
        "atual": ("CPU_FREQ_CURRENT", "MHz"),
        "minima": ("CPU_FREQ_MIN", "MHz"),
        "maxima": ("CPU_FREQ_MAX", "MHz")
    },
    "taxas": {
        "interrupcoes": ("CPU_INTERRUPTS_PER_SECOND", "eventos/s"),
        "trocas_contexto": ("CPU_CONTEXT_SWITCHES_PER_SECOND", "eventos/s")
    }
}

def leitura(registro, coluna, unidade, metrica, tipo="medicao", percentual_limitado=True):
    # Toda medida sai no mesmo formato, com o valor e o estado juntos.
    valor = registro[coluna]
    informado = coluna + "_STATUS" in registro
    if informado:
        status = registro[coluna + "_STATUS"]
    elif valor is not None:
        status = "ok"
    else:
        status = "indisponivel"

    if status:
        status = str(status).strip().lower()
    else:
        status = "indisponivel"
    status_original = status

    # Alguns CSVs podem trazer números como texto. Tentamos converter só a medida.
    if isinstance(valor, str):
        try:
            valor = float(valor)
        except ValueError:
            pass

    # bool também é considerado inteiro pelo Python, mas True não é uma leitura de CPU/RAM.
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        invalido = not math.isfinite(valor) or valor < 0
        if unidade == "%" and percentual_limitado and valor > 100:
            invalido = True
        if unidade == "MHz" and valor == 0:
            invalido = True
        if unidade == "núcleos" and valor <= 0:
            invalido = True
        if unidade in {"núcleos", "inodes", "ocorrências"} and valor % 1 != 0:
            invalido = True
        if tipo == "estado" and valor not in (0, 1):
            invalido = True
    else:
        invalido = True

    # Um estado de erro invalida o número mesmo que ele esteja preenchido no CSV.
    # Ausência fica None (null no JSON), sem trocar por zero.
    if status != "ok":
        valor = None
    elif invalido:
        if valor is None:
            status = "indisponivel"
        else:
            status = "valor_invalido"
        valor = None
    elif isinstance(valor, (int, float)):
        if unidade in {"núcleos", "inodes", "ocorrências"} or tipo == "estado":
            valor = int(valor)

    if status != status_original:
        origem_status = "validado_etl"
    elif informado:
        origem_status = "coletado"
    else:
        origem_status = "inferido"

    return {
        "metrica": metrica, "campo_origem": coluna, "unidade": unidade,
        "tipo": tipo, "valor": valor, "status": status, "origem_status": origem_status
    }


def leitura_cpu(registro, coluna, unidade):
    # Quantidade de núcleos é inventário. Não queremos calcular média disso.
    tipo = "medicao"
    if unidade == "núcleos":
        tipo = "inventario"
    return leitura(registro, coluna, unidade, coluna.lower(), tipo)


def indice_da_cpu(leitura):
    return leitura["indice"]


def organizar_cpu(registro):
    # Primeiro copiamos o registro. O que não é CPU será organizado nas próximas funções.
    resultado = registro.copy()
    resultado["timestamp"] = resultado.pop("TIMESTAMP")
    resultado.pop("ENDERECO_MAC", None)  # O MAC já existe na parte principal do documento.
    cpu = {}

    if "CPU_PERCENT" in registro:
        cpu["total"] = leitura_cpu(registro, "CPU_PERCENT", "%")
    for grupo, campos in GRUPOS_CPU.items():
        for nome, (coluna, unidade) in campos.items():
            if coluna in registro:
                if grupo not in cpu:
                    cpu[grupo] = {}
                cpu[grupo][nome] = leitura_cpu(registro, coluna, unidade)

    # Reconhecemos o padrão CPU_LOGICAL_0_PERCENT, CPU_LOGICAL_1_PERCENT etc.
    # O número pode variar de máquina pra máquina, então não fixamos uma quantidade.
    logicas = []
    for coluna in registro:
        correspondencia = re.fullmatch(r"CPU_LOGICAL_(\d+)_PERCENT", coluna)
        if correspondencia:
            medida = leitura_cpu(registro, coluna, "%")
            medida["indice"] = int(correspondencia[1])
            medida["metrica"] = "cpu_percent_percpu"
            logicas.append(medida)

    if logicas:
        logicas.sort(key=indice_da_cpu)
        cpu["logicas"] = {"metrica": "cpu_percent_percpu", "unidade": "%", "leituras": logicas}

    for origem, destino in [("CPU_SCHEMA_VERSION", "schema_coleta"),
                            ("CPU_INTERVAL_SECONDS", "intervalo_segundos")]:
        if origem in registro:
            cpu[destino] = registro[origem]

    # As colunas que já viraram objetos dentro de cpu não precisam ficar duplicadas fora dela.
    colunas = {"CPU_PERCENT", "CPU_SCHEMA_VERSION", "CPU_INTERVAL_SECONDS"}
    for campos in GRUPOS_CPU.values():
        for coluna, unidade in campos.values():
            colunas.add(coluna)
    for coluna in registro:
        if re.fullmatch(r"CPU_LOGICAL_\d+_PERCENT", coluna):
            colunas.add(coluna)
    for coluna in colunas:
        resultado.pop(coluna, None)
        resultado.pop(coluna + "_STATUS", None)
    if cpu:
        resultado["cpu"] = cpu
    return resultado


# Cada entrada abaixo tem: coluna do CSV, nome no catálogo do banco, unidade e tipo de leitura.
# None no nome da métrica significa que esse dado auxiliar não tem componente no catálogo atual.
# Inventário guarda o último valor; medicao calcula média; eventos_intervalo soma o que foi observado.
CAMPOS_RAM = {
    'uso': ('RAM_PERCENT', 'ram_percent', '%', 'medicao'),
    'capacidade_total': ('RAM_TOTAL', 'ram_total', 'GB', 'inventario'),
    'disponivel': ('RAM_AVAILABLE', 'ram_available', 'GB', 'medicao'),
    'usada': ('RAM_USED', 'ram_used', 'GB', 'medicao'),
    'livre': ('RAM_FREE', 'ram_free', 'GB', 'medicao'),
    'cache': ('RAM_CACHED', 'ram_cached', 'GB', 'medicao'),
    'oom': ('RAM_OOM_KILLS', 'ram_oom_kills', 'ocorrências', 'eventos_intervalo')
}
CAMPOS_SWAP = {
    'uso': ('SWAP_PERCENT', 'swap_percent', '%', 'medicao'),
    'capacidade_total': ('SWAP_TOTAL', 'swap_total', 'GB', 'inventario'),
    'usada': ('SWAP_USED', 'swap_used', 'GB', 'medicao'),
    'livre': ('SWAP_FREE', 'swap_free', 'GB', 'medicao'),
    'entrada': ('SWAP_SIN', 'swap_sin', 'bytes/s', 'medicao'),
    'saida': ('SWAP_SOUT', 'swap_sout', 'bytes/s', 'medicao')
}
CAMPOS_CARGA = {
    'um_minuto': ('LOAD_AVG_1', 'load_avg_1', 'carga', 'medicao'),
    'cinco_minutos': ('LOAD_AVG_5', 'load_avg_5', 'carga', 'medicao'),
    'quinze_minutos': ('LOAD_AVG_15', 'load_avg_15', 'carga', 'medicao')
}
# O prefixo DISCO_VOLUME_<indice>_ é acrescentado quando organizamos cada volume.
CAMPOS_VOLUME = {
    'capacidade_total': ('TOTAL', 'disk_total', 'GB', 'inventario'),
    'usado': ('USED', 'disk_used', 'GB', 'medicao'),
    'livre': ('FREE', 'disk_free', 'GB', 'medicao'),
    'ocupacao': ('PERCENT', 'disk_percent', '%', 'medicao'),
    'inodes_ocupacao': ('INODES_PERCENT', 'disk_inodes_percent', '%', 'medicao'),
    'inodes_total': ('INODES_TOTAL', None, 'inodes', 'inventario'),
    'inodes_livres': ('INODES_FREE', None, 'inodes', 'medicao'),
    'inodes_disponiveis': ('INODES_AVAILABLE', None, 'inodes', 'medicao')
}
# As duas latências usam disk_io_time, mas continuam separadas por leitura e escrita.
CAMPOS_IO = {
    'leitura_bytes': ('READ_BYTES', 'disk_read_bytes', 'bytes/s', 'medicao'),
    'escrita_bytes': ('WRITE_BYTES', 'disk_write_bytes', 'bytes/s', 'medicao'),
    'leituras': ('READ_COUNT', 'disk_read_count', 'IOPS', 'medicao'),
    'escritas': ('WRITE_COUNT', 'disk_write_count', 'IOPS', 'medicao'),
    'latencia_leitura': ('READ_TIME_MS', 'disk_io_time', 'ms', 'medicao'),
    'latencia_escrita': ('WRITE_TIME_MS', 'disk_io_time', 'ms', 'medicao'),
    'fila': ('QUEUE', 'disk_queue', 'operações', 'medicao')
}
# CPU do container não usa o limite da CPU total do servidor. O catálogo atual não tem esse vínculo.
# O coletor divide os campos chamados MB por 1024², então a unidade real aqui é MiB.
CAMPOS_CONTAINER = {
    'cpu': {'uso': ('CPU_PERCENT', None, '%', 'medicao')},
    'memoria': {'uso': ('RAM_PERCENT', None, '%', 'medicao')},
    'rede': {
        'recebidos': ('REDE_RECEBIDA_MB', None, 'MiB', 'delta_intervalo'),
        'enviados': ('REDE_ENVIADA_MB', None, 'MiB', 'delta_intervalo')
    },
    'disco': {
        'leitura_acumulada': ('DISCO_LEITURA_MB', None, 'MiB', 'contador_acumulado'),
        'escrita_acumulada': ('DISCO_ESCRITA_MB', None, 'MiB', 'contador_acumulado'),
        'camada_gravavel': ('CAMADA_GRAVAVEL_PERCENT', None, '%', 'medicao'),
        'imagem': ('IMAGEM_PERCENT', None, '%', 'medicao')
    }
}

# Mapeamento das métricas Docker coletadas em bytes e contadores.
# Diferenciamos medições instantâneas de contadores acumulados,
# pois não podem receber a mesma regra de agregação.
CAMPOS_CONTAINER_DOCKER = {
    "cpu": {
        "uso": ("CPU_PERCENT", None, "%", "medicao"),
        "tempo_total": ("CPU_TOTAL_USAGE", None, "ns", "contador_acumulado"),
        "tempo_sistema": ("CPU_SYSTEM_USAGE", None, "ns", "contador_acumulado"),
        "nucleos": ("CPU_ONLINE_CPUS", None, "núcleos", "inventario"),
        "tempo_throttled": ("CPU_THROTTLED_TIME", None, "ns", "contador_acumulado"),
        "periodos_throttled": ("CPU_THROTTLED_PERIODS", None, "ocorrências", "contador_acumulado")
    },
    "memoria": {
        "uso": ("RAM_PERCENT", None, "%", "medicao"),
        "utilizada": ("RAM_USAGE_BYTES", None, "bytes", "medicao"),
        "limite": ("RAM_LIMIT_BYTES", None, "bytes", "inventario")
    },
    "rede": {
        "recebidos": ("REDE_RECEBIDA_BYTES", None, "bytes", "contador_acumulado"),
        "enviados": ("REDE_ENVIADA_BYTES", None, "bytes", "contador_acumulado"),
        "erros_recebidos": ("NET_RX_ERRORS", None, "ocorrências", "contador_acumulado"),
        "erros_enviados": ("NET_TX_ERRORS", None, "ocorrências", "contador_acumulado"),
        "descartados_recebidos": ("NET_RX_DROPPED", None, "pacotes", "contador_acumulado"),
        "descartados_enviados": ("NET_TX_DROPPED", None, "pacotes", "contador_acumulado")
    },
    "disco": {
        "leitura_acumulada": ("DISCO_LEITURA_BYTES", None, "bytes", "contador_acumulado"),
        "escrita_acumulada": ("DISCO_ESCRITA_BYTES", None, "bytes", "contador_acumulado"),
        "camada_gravavel": ("CAMADA_GRAVAVEL_BYTES", None, "bytes", "medicao"),
        "imagem": ("IMAGEM_SIZE_BYTES", None, "bytes", "inventario")
    },
    "reinicios": {
        "total": ("RESTART_COUNT", None, "ocorrências", "contador_acumulado")
    },
    "pids": {
        "atuais": ("PIDS_CURRENT", None, "processos", "medicao")
    }
}

def organizar_campos(registro, campos, usados, prefixo='', percentual_limitado=True):
    # O mapa diz qual coluna ler e qual nome de métrica entregar pro Java buscar no banco.
    resultado = {}
    for nome, (campo, metrica, unidade, tipo) in campos.items():
        coluna = prefixo + campo
        if coluna in registro:
            limitar = True
            if campo == 'CPU_PERCENT':
                # CPU de processo/container pode passar de 100%, diferente da CPU total do host.
                limitar = percentual_limitado
            resultado[nome] = leitura(registro, coluna, unidade, metrica, tipo, limitar)
            usados.add(coluna)
            usados.add(coluna + '_STATUS')
    return resultado


def metadata(registro, campos, usados):
    # Copia identidades e informações da coleta, como montagem, dispositivo e intervalo.
    # Esses dados não são medidas pra comparar com limite nem pra calcular média.
    resultado = {}
    for coluna, nome in campos.items():
        if coluna in registro:
            resultado[nome] = registro[coluna]
            usados.add(coluna)
    return resultado


def organizar_disco(registro, usados):
    resultado = metadata(registro, {'DISCO_VOLUMES_STATUS': 'status_volumes',
                                    'DISCO_IO_STATUS': 'status_io'}, usados)
    for grupo, nome_lista, campos in [('VOLUME', 'volumes', CAMPOS_VOLUME), ('IO', 'io', CAMPOS_IO)]:
        # Descobrimos quais índices existem no CSV em vez de assumir que sempre tem disco 0 e 1.
        indices = set()
        for coluna in registro:
            correspondencia = re.fullmatch(r'DISCO_' + grupo + r'_(\d+)_.+', coluna)
            if correspondencia:
                indices.add(int(correspondencia[1]))
        if not indices:
            continue

        resultado[nome_lista] = []
        for indice in sorted(indices):
            prefixo = f'DISCO_{grupo}_{indice}_'
            item = {'indice': indice}
            identificacao = metadata(registro, {
                prefixo + 'MONTAGEM': 'montagem', prefixo + 'DISPOSITIVO': 'dispositivo',
                prefixo + 'TIPO_SISTEMA': 'tipo_sistema', prefixo + 'COLETA_STATUS': 'status',
                prefixo + 'INTERVAL_SECONDS': 'intervalo_segundos'}, usados)
            item.update(identificacao)

            # O índice pode mudar em outro CSV. A montagem/dispositivo identifica o recurso de verdade.
            if grupo == 'VOLUME':
                identidade = item.get('montagem')
            else:
                identidade = item.get('dispositivo')
            if not identidade:
                raise ValueError(f'Recurso de disco sem identidade: {prefixo}')
            item.update(organizar_campos(registro, campos, usados, prefixo))
            resultado[nome_lista].append(item)
    return resultado


def somar_direcoes(recebidos, enviados, metrica, unidade):
    partes = []
    for parte in (recebidos, enviados):
        if parte is not None:
            partes.append(parte)

    completa = len(partes) == 2
    estados = set()
    for parte in partes:
        estados.add(parte['status'])
        if parte['status'] != 'ok':
            completa = False

    # Não dá pra somar entrada com uma saída que falhou como se a saída fosse zero.
    valor = None
    if completa:
        status = 'ok'
        valores = []
        for parte in partes:
            valores.append(parte['valor'])
        valor = sum(valores)
    elif len(estados) == 1 and len(partes) == 2:
        status = next(iter(estados))
    else:
        status = 'fonte_incompleta'

    return {'metrica': metrica, 'unidade': unidade, 'tipo': 'medicao',
            'valor': valor, 'status': status, 'origem_status': 'calculado',
            'calculo': 'soma_entrada_saida'}


def organizar_rede(registro, usados):
    resultado = metadata(registro, {'REDE_COLETA_STATUS': 'status'}, usados)
    sufixos = ('BYTES_RECV', 'BYTES_SENT', 'ERRIN', 'ERROUT', 'DROPIN', 'DROPOUT',
               'PACKETS_RECV', 'PACKETS_SENT', 'ISUP', 'INTERVAL_SECONDS', 'COLETA_STATUS')
    padrao = r'REDE_(.+)_(' + '|'.join(sufixos) + r')(?:_STATUS)?'

    # O nome da interface também pode ter _ e :, então procuramos os finais conhecidos das colunas.
    interfaces = set()
    for coluna in registro:
        correspondencia = re.fullmatch(padrao, coluna)
        if correspondencia:
            interfaces.add(correspondencia[1])
    if interfaces:
        resultado['interfaces'] = []

    for nome in sorted(interfaces):
        prefixo = f'REDE_{nome}_'
        item = {'nome': nome}
        item.update(metadata(registro, {prefixo + 'INTERVAL_SECONDS': 'intervalo_segundos',
                                       prefixo + 'COLETA_STATUS': 'status'}, usados))

        # No catálogo, tráfego/erros/descartes representam entrada + saída.
        # Guardamos o total configurável e as duas direções separadas pros gráficos.
        for grupo, entrada, saida, metrica, unidade in [
            ('trafego', 'BYTES_RECV', 'BYTES_SENT', 'net_traffic', 'bytes/s'),
            ('erros', 'ERRIN', 'ERROUT', 'net_errors', 'eventos/s'),
            ('descartes', 'DROPIN', 'DROPOUT', 'net_drops', 'pacotes/s')
        ]:
            campos = {'recebidos': (entrada, None, unidade, 'medicao'),
                      'enviados': (saida, None, unidade, 'medicao')}
            partes = organizar_campos(registro, campos, usados, prefixo)
            if partes:
                agregado = somar_direcoes(partes.get('recebidos'), partes.get('enviados'), metrica, unidade)
                agregado.update(partes)
                item[grupo] = agregado

        item.update(organizar_campos(registro, {
            'estado': ('ISUP', 'net_isup', 'estado', 'estado'),
            'pacotes_recebidos': ('PACKETS_RECV', 'net_packets_recv', 'pacotes/s', 'medicao'),
            'pacotes_enviados': ('PACKETS_SENT', 'net_packets_sent', 'pacotes/s', 'medicao')}, usados, prefixo))
        resultado['interfaces'].append(item)
    return resultado


def extras(registro, usados):
    # Um campo novo não pode sumir sem ninguém perceber. Guardamos ele sem inventar vínculo de banco.
    resultado = []
    for coluna, valor in registro.items():
        if coluna not in usados and not coluna.endswith('_STATUS'):
            resultado.append({
                'campo_origem': coluna, 'metrica': None, 'valor': valor,
                'status': registro.get(coluna + '_STATUS', 'sem_mapeamento')
            })
    return resultado


def organizar_metricas(registro):
    # Essa é a função que junta todas as categorias da captura geral.
    resultado_cpu = organizar_cpu(registro)
    resultado = {}
    for campo in ('timestamp', 'cpu'):
        if campo in resultado_cpu:
            resultado[campo] = resultado_cpu[campo]

    # As colunas que a CPU já retirou da cópia foram usadas. Depois cada categoria marca as suas.
    usados = set()
    for coluna in registro:
        if coluna not in resultado_cpu:
            usados.add(coluna)

    for categoria, campos in [('ram', CAMPOS_RAM), ('swap', CAMPOS_SWAP), ('carga', CAMPOS_CARGA)]:
        organizado = organizar_campos(registro, campos, usados)
        if categoria == 'ram':
            contexto = metadata(registro, {'RAM_OOM_BOOT_ID': 'boot_id',
                                          'RAM_OOM_INTERVAL_SECONDS': 'intervalo_segundos'}, usados)
            if contexto:
                organizado['contexto_oom'] = contexto
        if categoria == 'swap':
            organizado.update(metadata(registro, {'SWAP_INTERVAL_SECONDS': 'intervalo_segundos'}, usados))
        if organizado:
            resultado[categoria] = organizado

    for categoria, organizar in [('disco', organizar_disco), ('rede', organizar_rede)]:
        organizado = organizar(registro, usados)
        if organizado:
            resultado[categoria] = organizado

    contexto = metadata(registro, {'CENARIO': 'cenario', 'EXPERIMENTO': 'experimento',
                                  'COLETA_STATUS': 'status'}, usados)
    if contexto:
        resultado['contexto'] = contexto
    restantes = extras(registro, usados)
    if restantes:
        resultado['extras'] = restantes
    return resultado


# Nome/PID/usuário identificam a observação, mas não provam que o processo continua ativo.
# CPU de processo pode passar de 100% e não recebe o limite da CPU total do host.
def organizar_processo(registro):
    usados = {'TIMESTAMP', 'ENDERECO_MAC', 'PID', 'NOME', 'USUARIO'}
    resultado = {'timestamp': registro['TIMESTAMP'], 'pid': int(registro['PID']),
                 'nome': registro['NOME'], 'usuario': registro.get('USUARIO'), 'escopo': 'processo'}
    for grupo, campos in [
        ('cpu', {'uso': ('CPU_PERCENT', None, '%', 'medicao')}),
        ('memoria', {'uso': ('MEMORIA_PERCENT', None, '%', 'medicao')})
    ]:
        medidas = organizar_campos(registro, campos, usados, percentual_limitado=False)
        if medidas:
            resultado[grupo] = medidas
    restantes = extras(registro, usados)
    if restantes:
        resultado['extras'] = restantes
    return resultado


# Mantemos as leituras do container no seu próprio escopo. Não inferimos saúde da aplicação.
def organizar_container(registro):

    # O novo coletor Docker fornece CONTAINER_ID.
    # Isso nos permite distinguir o formato novo do anterior.
    novo_docker = "CONTAINER_ID" in registro

    usados = {"TIMESTAMP", "ENDERECO_MAC", "CONTAINER"}

    resultado = {
        "timestamp": registro["TIMESTAMP"],
        "nome": registro["CONTAINER"],
        "escopo": "container"
    }

    if novo_docker:

        # Preserva a identidade única do contêiner.
        resultado["container_id"] = registro["CONTAINER_ID"]
        usados.add("CONTAINER_ID")

        # Informações de identificação e estado não são
        # medidas numéricas e não devem receber médias.
        resultado.update(
            metadata(registro, {
                "IMAGEM": "imagem",
                "STATUS": "status",
                "HEALTH_STATUS": "health_status",
                "STARTED_AT": "started_at",
                "FINISHED_AT": "finished_at",
                "EXIT_CODE": "exit_code",
                "OOM_KILLED": "oom_killed"
            }, usados)
        )

    # Mantém compatibilidade com o formato anterior.
    if novo_docker:
        campos_container = CAMPOS_CONTAINER_DOCKER
    else:
        campos_container = CAMPOS_CONTAINER

    # Organiza cada grupo utilizando a função já existente.
    for grupo, campos in campos_container.items():

        medidas = organizar_campos(
            registro,
            campos,
            usados,
            percentual_limitado=False
        )

        if medidas:
            resultado[grupo] = medidas

    # Preserva qualquer campo ainda não mapeado.
    restantes = extras(registro, usados)

    if restantes:
        resultado["extras"] = restantes

    return resultado


def resumir_leituras(leituras):
    # Só leituras ok entram nas contas. Um zero válido participa; um null não participa.
    valores = []
    for leitura in leituras:
        if leitura['status'] == 'ok' and leitura['valor'] is not None:
            valores.append(leitura['valor'])

    ultima = leituras[-1]
    tipo = ultima.get('tipo', 'medicao')
    resumo = {}
    for campo in ('metrica', 'unidade', 'tipo', 'calculo', 'indice'):
        if campo in ultima:
            resumo[campo] = ultima[campo]
    resumo['amostras_validas'] = len(valores)
    resumo['amostras_invalidas'] = len(leituras) - len(valores)

    if tipo in ('inventario', 'contador_acumulado', 'estado'):
        # Quantidade de núcleos e contador acumulado ficam com o último valor, sem somar as amostras.
        resumo['ultimo_valor'] = ultima['valor']
        resumo['ultimo_status'] = ultima['status']
        if tipo == 'estado':
            if valores:
                resumo['percentual_amostras_ativas'] = sum(valores) / len(valores) * 100
            else:
                resumo['percentual_amostras_ativas'] = None
    elif tipo in ('eventos_intervalo', 'delta_intervalo'):
        if valores:
            resumo['total_observado'] = sum(valores)
        else:
            resumo['total_observado'] = None
    else:
        if valores:
            resumo['media'] = sum(valores) / len(valores)
            resumo['minimo'] = min(valores)
            resumo['maximo'] = max(valores)
        else:
            resumo['media'] = None
            resumo['minimo'] = None
            resumo['maximo'] = None

    # O grupo de Rede tem o total e também os valores de entrada e saída.
    for direcao in ('recebidos', 'enviados'):
        partes = []
        for leitura in leituras:
            if direcao in leitura:
                partes.append(leitura[direcao])
        if partes:
            resumo[direcao] = resumir_leituras(partes)
    return resumo


# Junta observações do mesmo recurso nos resumos, sem depender da posição dele na lista.
# O coletor não fornece criação do processo/ID do container, então não identificamos toda reinicialização.
def identidade_recurso(item):

    # O ID identifica a instância real do contêiner.
    # Nomes podem ser reutilizados após uma recriação.
    if 'container_id' in item:
        return ('container', item['container_id'])

    if 'pid' in item:
        return ('processo', item['pid'], item.get('nome'), item.get('usuario'))

    if 'montagem' in item:
        return ('volume', item['montagem'], item.get('dispositivo'), item.get('tipo_sistema'))

    if 'dispositivo' in item:
        return ('io', item['dispositivo'])

    if 'nome' in item:
        return ('nome', item['nome'])

    return ('indice', item['indice'])

def resumir_estrutura(observacoes):
    # Percorre o conteúdo até chegar numa medida com valor/status.
    # Quando encontra uma lista, primeiro separa os recursos pra não misturar seus históricos.
    primeira = observacoes[0]
    if isinstance(primeira, dict):
        if 'valor' in primeira and 'status' in primeira:
            return resumir_leituras(observacoes)

        resultado = {}
        chaves = {}
        for observacao in observacoes:
            for campo in observacao:
                chaves[campo] = None  # Guarda os nomes sem repetir, mantendo a ordem de encontro.
        for campo in chaves:
            if campo in ('extras', 'contexto', 'contexto_oom', 'timestamp', 'escopo', 'id_captura'):
                continue
            valores = []
            for observacao in observacoes:
                if campo in observacao:
                    valores.append(observacao[campo])
            resultado[campo] = resumir_estrutura(valores)
        return resultado

    if isinstance(primeira, list):
        grupos = {}
        for lista in observacoes:
            for recurso in lista:
                identidade = identidade_recurso(recurso)
                if identidade not in grupos:
                    grupos[identidade] = []
                grupos[identidade].append(recurso)

        resumos = []
        for grupo in grupos.values():
            resumo = resumir_estrutura(grupo)
            if isinstance(resumo, dict) and 'timestamp' in grupo[0]:
                resumo['primeira_observacao'] = grupo[0]['timestamp']
                resumo['ultima_observacao'] = grupo[-1]['timestamp']
                resumo['amostras_observadas'] = len(grupo)
            resumos.append(resumo)
        return resumos

    # Nome, índice e outros metadados não recebem média. O lote já veio ordenado por horário.
    return observacoes[-1]


def resumir_cpu(capturas):
    cpus = []
    for captura in capturas:
        if 'cpu' in captura:
            cpus.append(captura['cpu'])
    if not cpus:
        return {}
    return resumir_estrutura(cpus)


def resumir_documento(documento):
    # O resumo cobre só o intervalo observado, não uma garantia de coleta do dia inteiro.
    registros = documento['metricas'] + documento['processos'] + documento['containers']
    horarios = []
    for registro in registros:
        horarios.append(registro['timestamp'])
    inicio = None
    fim = None
    if horarios:
        inicio = min(horarios)
        fim = max(horarios)
    resultado = {'inicio': inicio, 'fim': fim, 'metodo_media': 'aritmetica_das_amostras_validas'}

    if documento['metricas']:
        resultado.update(resumir_estrutura(documento['metricas']))
    for categoria in ('processos', 'containers'):
        if documento[categoria]:
            resultado[categoria] = resumir_estrutura([documento[categoria]])
        else:
            resultado[categoria] = []
    return resultado
