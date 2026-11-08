import docker
import csv
import uuid

from datetime import datetime, timezone
from pathlib import Path


COLUNAS_METRICAS = [
    # Identificação
    "TIMESTAMP",
    "ENDERECO_MAC",
    "CONTAINER_ID",
    "CONTAINER",
    "IMAGEM",

    # Estado do container
    "STATUS",
    "HEALTH_STATUS",
    "RESTART_COUNT",
    "STARTED_AT",
    "FINISHED_AT",
    "EXIT_CODE",
    "OOM_KILLED",

    # CPU
    "CPU_PERCENT",
    "CPU_TOTAL_USAGE",
    "CPU_SYSTEM_USAGE",
    "CPU_ONLINE_CPUS",
    "CPU_THROTTLED_TIME",
    "CPU_THROTTLED_PERIODS",

    # Memória
    "RAM_PERCENT",
    "RAM_USAGE_BYTES",
    "RAM_LIMIT_BYTES",

    # Rede
    "REDE_RECEBIDA_BYTES",
    "REDE_ENVIADA_BYTES",
    "NET_RX_ERRORS",
    "NET_TX_ERRORS",
    "NET_RX_DROPPED",
    "NET_TX_DROPPED",

    # Disco
    "DISCO_LEITURA_BYTES",
    "DISCO_ESCRITA_BYTES",
    "CAMADA_GRAVAVEL_BYTES",
    "IMAGEM_SIZE_BYTES",

    # Processos/threads
    "PIDS_CURRENT"
]

cliente = docker.from_env()

def listar_containers():
    return cliente.containers.list(all=True)        

def capturar_identificacao(container):
    return {
        "CONTAINER_ID": container.id,
        "CONTAINER": container.name,

        # Tem como com container.image.tags, porém, retorna uma lista de tags configuradas, não a atual,
        # Também pode não te nenhuma tag e dar IndexError

        # Obtém a referência da imagem direto pelo atributo do objeto
        "IMAGEM": container.attrs["Config"]["Image"],
        "STATUS": container.status
    }

def capturar_estado(container):
    estado = container.attrs["State"]

    return {
        "HEALTH_STATUS": estado.get("Health", {}).get("Status"),
        "RESTART_COUNT": container.attrs.get("RestartCount"),
        "STARTED_AT": estado.get("StartedAt"),
        "FINISHED_AT": estado.get("FinishedAt"),
        "EXIT_CODE": estado.get("ExitCode"),
        "OOM_KILLED": estado.get("OOMKilled")
    }

def capturar_metricas(incluir_armazenamento=False, endereco_mac=None):
    containers = listar_containers()

    # Identifica a máquina responsável pela coleta.
    # Se o MAC vier do agente principal, utilizamos o valor recebido.
    if endereco_mac is None:
        endereco_mac = obter_endereco_mac()

    # Registramos o horário em UTC.
    # Uma mesma rodada de captura terá um timestamp de referência.
    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    )

    # Busca os tamanhos dos containers somente quando solicitado.
    # Caso contrário, utiliza um dicionário vazio.
    tamanhos_por_id = (
        listar_tamanhos_containers()
        if incluir_armazenamento
        else {}
    )

    dados = []

    for container in containers:
        # Primeiro capturamos a identificação e o estado.
        dado = capturar_identificacao(container)

        # Identificação temporal e da máquina monitorada.
        dado["TIMESTAMP"] = timestamp
        dado["ENDERECO_MAC"] = endereco_mac

        estado = capturar_estado(container)

        dado.update(estado)

        # Apenas containers em execução possuem
        # estatísticas de utilização disponíveis.
        if container.status == "running":

            try:
                # Uma única consulta fornece os dados brutos
                # de CPU, memória, rede, disco e processos.
                stats = container.stats(stream=False)

                # Cada função extrai somente os campos
                # relacionados à sua responsabilidade.
                dado.update(capturar_cpu(stats))
                dado.update(capturar_memoria(stats))

                # Rede: bytes enviados, recebidos e erros de transmissão.
                dado.update(capturar_rede(stats))

                # Disco: bytes lidos e escritos pelo container.
                dado.update(capturar_disco(stats))

                # Captura a quantidade atual de processos e threads.
                dado.update(capturar_pids(stats))

            except docker.errors.APIError as erro:
                # Uma falha na captura de um container não deve
                # interromper a coleta dos demais.
                print(f"Erro ao capturar {container.name}: {erro}")

                dado.update(capturar_cpu({}))
                dado.update(capturar_memoria({}))
                
                # Preserva todas as colunas mesmo sem estatísticas disponíveis.
                dado.update(capturar_rede({}))
                dado.update(capturar_disco({}))

                # Mantém a coluna mesmo sem estatísticas disponíveis.
                dado.update(capturar_pids({}))

        else:
            # Para containers parados, registramos o estado,
            # mas não inventamos valores de utilização.
            dado.update(capturar_cpu({}))
            dado.update(capturar_memoria({}))
            dado.update(capturar_rede({}))
            dado.update(capturar_disco({}))            
            dado.update(capturar_pids({}))

        # O armazenamento é independente do status do container.
        # Podemos consultar o tamanho mesmo se ele estiver parado.
        if incluir_armazenamento:
            tamanho_rw = tamanhos_por_id.get(container.id)

            dado.update(
                capturar_armazenamento(container, tamanho_rw)
            )
        else:
            # Mantém as colunas no resultado mesmo quando
            # a consulta de armazenamento está desativada.
            dado.update({
                "CAMADA_GRAVAVEL_BYTES": None,
                "IMAGEM_SIZE_BYTES": None
            })

        dados.append(dado)

    return dados

def capturar_cpu(stats):
    # Informações de CPU da coleta atual e anterior
    cpu_atual = stats.get("cpu_stats", {})
    cpu_anterior = stats.get("precpu_stats", {})

    uso_atual = cpu_atual.get("cpu_usage", {})
    uso_anterior = cpu_anterior.get("cpu_usage", {})

    # Tempo acumulado de CPU consumido pelo container (nanossegundos)
    total_atual = uso_atual.get("total_usage")
    total_anterior = uso_anterior.get("total_usage")

    # Tempo acumulado de CPU do sistema (nanossegundos)
    sistema_atual = cpu_atual.get("system_cpu_usage")
    sistema_anterior = cpu_anterior.get("system_cpu_usage")

    # Quantidade de CPUs lógicas disponíveis
    # Em algumas versões, online_cpus pode estar ausente.
    quantidade_cpus = cpu_atual.get("online_cpus")

    if not quantidade_cpus:
        quantidade_cpus = len(uso_atual.get("percpu_usage") or [])

    # Informações de limitação de CPU (throttling)
    throttling = cpu_atual.get("throttling_data", {})

    tempo_throttled = throttling.get("throttled_time")
    periodos_throttled = throttling.get("throttled_periods")

    cpu_percent = None

    # Precisamos de duas amostras válidas para calcular o percentual.
    # Se a amostra anterior não existir, mantemos None.
    if (
        total_atual is not None
        and total_anterior is not None
        and sistema_atual is not None
        and sistema_anterior is not None
        and sistema_anterior > 0
        and quantidade_cpus > 0
    ):
        diferenca_cpu = total_atual - total_anterior
        diferenca_sistema = sistema_atual - sistema_anterior

        # Evita divisão por zero e diferenças negativas,
        # que podem ocorrer quando os contadores são reiniciados.
        if diferenca_cpu >= 0 and diferenca_sistema > 0:
            cpu_percent = (
                diferenca_cpu / diferenca_sistema
            ) * quantidade_cpus * 100

            cpu_percent = round(cpu_percent, 2)

    return {
        "CPU_PERCENT": cpu_percent,
        "CPU_TOTAL_USAGE": total_atual,
        "CPU_SYSTEM_USAGE": sistema_atual,
        "CPU_ONLINE_CPUS": quantidade_cpus or None,

        # O Docker fornece throttled_time em nanossegundos.
        # Convertemos para segundos para facilitar a análise.
        "CPU_THROTTLED_TIME": (
            tempo_throttled / 1_000_000_000
            if tempo_throttled is not None else None
        ),

        # Quantidade acumulada de períodos com limitação de CPU.
        "CPU_THROTTLED_PERIODS": periodos_throttled
    }

def capturar_memoria(stats):
    memoria = stats.get("memory_stats", {})

    # Memória total contabilizada pelo Docker, em bytes
    uso_bruto = memoria.get("usage")

    # Limite de memória disponível para o container
    limite = memoria.get("limit")

    # Estatísticas detalhadas de memória
    detalhes = memoria.get("stats", {})

    # O nome do contador de cache varia conforme o cgroup.
    # Priorizamos os campos de memória inativa utilizados
    # para aproximar o comportamento do docker stats.
    cache = detalhes.get("total_inactive_file")

    if cache is None:
        cache = detalhes.get("inactive_file")

    if cache is None:
        cache = detalhes.get("cache", 0)

    uso_memoria = None
    ram_percent = None

    if uso_bruto is not None:
        # Descontamos o cache para evitar superestimar
        # o consumo efetivo de memória do container.
        uso_memoria = max(0, uso_bruto - cache)

        if limite is not None and limite > 0:
            ram_percent = (uso_memoria / limite) * 100
            ram_percent = round(ram_percent, 2)

    return {
        "RAM_PERCENT": ram_percent,
        "RAM_USAGE_BYTES": uso_memoria,
        "RAM_LIMIT_BYTES": limite
    }

def capturar_rede(stats):
    # Cada container pode ter uma ou mais interfaces de rede.
    redes = stats.get("networks") or {}

    # Quando não existe informação de rede, não podemos
    # afirmar que o tráfego foi zero.
    if not redes:
        return {
            "REDE_RECEBIDA_BYTES": None,
            "REDE_ENVIADA_BYTES": None,
            "NET_RX_ERRORS": None,
            "NET_TX_ERRORS": None,
            "NET_RX_DROPPED": None,
            "NET_TX_DROPPED": None
        }

    # Contadores acumulados desde o início da coleta do Docker.
    recebidos = 0
    enviados = 0
    erros_rx = 0
    erros_tx = 0
    descartados_rx = 0
    descartados_tx = 0

    # Somamos o tráfego de todas as interfaces do container.
    for interface in redes.values():
        recebidos += interface.get("rx_bytes", 0)
        enviados += interface.get("tx_bytes", 0)

        erros_rx += interface.get("rx_errors", 0)
        erros_tx += interface.get("tx_errors", 0)

        descartados_rx += interface.get("rx_dropped", 0)
        descartados_tx += interface.get("tx_dropped", 0)

    return {
        "REDE_RECEBIDA_BYTES": recebidos,
        "REDE_ENVIADA_BYTES": enviados,
        "NET_RX_ERRORS": erros_rx,
        "NET_TX_ERRORS": erros_tx,
        "NET_RX_DROPPED": descartados_rx,
        "NET_TX_DROPPED": descartados_tx
    }

def capturar_disco(stats):
    # Estatísticas de operações de entrada e saída em disco.
    bloco = stats.get("blkio_stats") or {}

    # Lista de operações realizadas nos dispositivos de disco.
    operacoes = bloco.get("io_service_bytes_recursive")

    # A estrutura pode não estar disponível dependendo
    # do sistema operacional, cgroup ou configuração Docker.
    if operacoes is None:
        return {
            "DISCO_LEITURA_BYTES": None,
            "DISCO_ESCRITA_BYTES": None
        }

    leitura = 0
    escrita = 0

    # Uma mesma operação pode aparecer em diferentes
    # dispositivos. Precisamos somar os registros.
    for operacao in operacoes:
        tipo = operacao.get("op", "").lower()
        valor = operacao.get("value", 0)

        if tipo == "read":
            leitura += valor

        elif tipo == "write":
            escrita += valor

        # Ignoramos "Total" para não contabilizar
        # novamente as operações já somadas.

    return {
        "DISCO_LEITURA_BYTES": leitura,
        "DISCO_ESCRITA_BYTES": escrita
    }

def capturar_pids(stats):
    # Recupera as estatísticas de processos e threads.
    pids = stats.get("pids_stats") or {}

    # 'current' representa a quantidade atual de tarefas
    # (processos e threads) associadas ao container.
    #
    # Quando não existe informação disponível, retorna None.
    # Isso evita interpretar ausência de dados como zero.
    return {
        "PIDS_CURRENT": pids.get("current")
    }

def capturar_armazenamento(container, tamanho_rw):
    imagem_bytes = None

    try:
        # Obtém o tamanho lógico da imagem Docker.
        # Não representa necessariamente o espaço físico
        # exclusivo ocupado pela imagem no disco.
        imagem_bytes = container.image.attrs.get("Size")

    except docker.errors.APIError as erro:
        # A imagem pode ter sido removida ou a consulta
        # pode falhar. Mantemos o valor indisponível.
        print(f"Erro na imagem de {container.name}: {erro}")

    return {
        "CAMADA_GRAVAVEL_BYTES": tamanho_rw,
        "IMAGEM_SIZE_BYTES": imagem_bytes
    }

def listar_tamanhos_containers():
    try:
        # A consulta padrão não inclui SizeRw.
        # size=True solicita o cálculo dos tamanhos.
        containers = cliente.api.containers(
            all=True,
            size=True
        )

        # Criamos um dicionário indexado pelo ID.
        # Assim, encontramos o tamanho de cada container
        # sem precisar percorrer a lista novamente.
        return {
            container["Id"]: container.get("SizeRw")
            for container in containers
        }

    except docker.errors.APIError as erro:
        print(f"Erro ao consultar tamanhos: {erro}")
        return {}

def obter_endereco_mac():
    # Obtém um identificador MAC disponível no sistema.
    # uuid.getnode() retorna um inteiro de 48 bits.
    mac = uuid.getnode()

    # Converte o inteiro para o formato convencional de MAC.
    # Exemplo: AA:BB:CC:DD:EE:FF
    return ":".join(
        f"{(mac >> deslocamento) & 0xFF:02X}"
        for deslocamento in range(40, -1, -8)
    )

def gerar_csv(dados, diretorio="csv"):
    # Cria o diretório de saída caso ele não exista.
    pasta = Path(diretorio)
    pasta.mkdir(parents=True, exist_ok=True)

    # Cria um nome único por coleta.
    # Os microssegundos ajudam a evitar nomes repetidos.
    timestamp_arquivo = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    caminho = pasta / f"metricas_{timestamp_arquivo}.csv"

    # newline="" evita linhas em branco adicionais.
    # utf-8 garante uma codificação consistente.
    with open(caminho, "w", newline="", encoding="utf-8") as arquivo:

        # As colunas seguem exatamente a ordem definida
        # em COLUNAS_METRICAS.
        escritor = csv.DictWriter(
            arquivo,
            fieldnames=COLUNAS_METRICAS,
            delimiter=";"
        )

        # Escreve a primeira linha com os nomes das colunas.
        escritor.writeheader()

        # Escreve um registro para cada container.
        # get() retorna None caso uma métrica esteja indisponível.
        for dado in dados:
            linha = {
                coluna: dado.get(coluna)
                for coluna in COLUNAS_METRICAS
            }

            escritor.writerow(linha)

    return caminho

if __name__ == "__main__":

    # Captura todas as métricas, incluindo armazenamento.
    # Para as coletas periódicas, podemos desativar
    # o armazenamento para reduzir o custo.
    capturas = capturar_metricas(
        incluir_armazenamento=True
    )

    # Exporta todos os containers para o mesmo CSV.
    caminho = gerar_csv(capturas)

    print(f"CSV gerado em: {caminho}")
    print(f"Containers capturados: {len(capturas)}")
