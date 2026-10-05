import psutil as p
import csv
import time
import os
import json
import requests
from datetime import datetime


# Configurar as variáveis de ambiente pra API pra cahamar commais facilidade posteriormente
API_BASE_URL = "http://localhost:3333"
API_FILTRO_URL = "http://localhost:3334"


# Token de instalação e token do perfil do servidor ficam salvos no arquivo de configuração
ARQUIVO_CONFIG = "config.json"


def obter_endereco_mac():
    interfaces = p.net_if_addrs()

    for nome_interface, enderecos in interfaces.items():
        for endereco in enderecos:

            # family é o tipo de endereço, p.AF_LINK é o tipo de endereço MAC
            if endereco.family == p.AF_LINK:
                if endereco.address and endereco.address != "00:00:00:00:00:00":
                    return endereco.address.upper()

    return None


# Função para carregar o arquivo de configuração
def carregar_config():
    if not os.path.exists(ARQUIVO_CONFIG):
        print(f"Arquivo de configuração '{ARQUIVO_CONFIG}' não encontrado.")
        exit()

    try:
        with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)

    except json.JSONDecodeError:
        print("O arquivo config.json possui um JSON inválido.")
        exit()


# Função para verificar se o node já está registrado
def ativar_agente(
    token_instalacao,
    token_perfil_servidor,
    endereco_mac
):
    dados = {
        "tokenInstalacaoServer": token_instalacao,
        "tokenPerfilServidorServer": token_perfil_servidor,
        "enderecoMacServer": endereco_mac
    }

    try:
        resposta = requests.post(
            f"{API_BASE_URL}/servidor/ativarAgente",
            json=dados
        )

        if resposta.status_code == 200:
            print(f"Agente ativado no MAC {endereco_mac}")
            return True

        print(f"Erro ao ativar agente ({resposta.status_code})")
        print(resposta.text)

        return False

    except requests.RequestException as erro:
        print(f"Falha de conexão com a API de ativação: {erro}")
        return False


def buscar_metricas_cliente(
    token_instalacao,
    token_perfil_servidor
):
    # Usa a nossa API pra pegar os "comando_parametro" que foram cadastrados pro perfil do servidor
    try:
        resposta = requests.get(
            f"{API_FILTRO_URL}/empresa/metricas",
            headers={
                "Authorization": f"Bearer {token_instalacao}",
                "token-perfil": token_perfil_servidor
            }
        )

        if resposta.status_code == 200:
            metricas = resposta.json()

            # Seleciona as métricas que vieram do model da empresa
            print(f"{len(metricas)} métricas carregadas.\n")

            return metricas

        print(
            f"Houve um erro ao consultar as métricas "
            f"({resposta.status_code})."
        )

        print(resposta.text)

        return []

    except requests.RequestException as erro:
        print(f"Falha de conexão com a API de métricas: {erro}")
        return []


# Converte os valores da coluna "argumento_valor" do banco para o que o psutil necessita
def converter_valor(valor):

    # Se não veio argumento, não converte
    if valor is None:
        return None

    # Verifica o tipo
    if not isinstance(valor, str):
        return valor

    # Remove espaços do início e do fim
    valor = valor.strip()

    if valor.lower() == "true":
        return True

    if valor.lower() == "false":
        return False

    if valor.lower() == "none":
        return None

    # Tenta converter para inteiro
    try:
        return int(valor)

    except ValueError:
        pass

    # Tenta convereter para float
    try:
        return float(valor)

    except ValueError:
        pass

    # Caso nenhuma conversão funcione, retorna como texto
    return valor


# {
#    "funcao_psutil": "cpu_count",
#    "argumento_nome": "logical",
#    "argumento_valor": "true"
# }
# Precisa virar -> p.cpu_count(logical=True)
#

# Monta os parâmetros que são enviados para a função do psutil
def montar_argumentos(metrica):
    nome = metrica.get("argumento_nome")

    valor = converter_valor(
        metrica.get("argumento_valor")
    )

    # Retorna um dicionário montado com o nome e o valor do argumento
    if nome is None:
        return {}

    return {
        nome: valor
    }


# Executa a função do psutil de forma dinâmica a partir da montagem dos argumentos
def executar_funcao(metrica, cache):
    nome_funcao = metrica["funcao_psutil"]

    argumentos = montar_argumentos(metrica)

    # Monta uma tupla que armazena a funcao e os argumentos para poder diferenciar a mesma função com argumentos diferentes
    # Exemplo: p.cpu_count(logical=True) e p.cpu_count(logical=False) são chamadas diferentes da mesma função
    chave_cache = (
        nome_funcao,

        # Ordena de forma crescente os valores da lista de argumentos e coloca em uma tupla para poder ser usada como chave do dicionário
        tuple(sorted(argumentos.items()))
    )

    # Caso a função ainda não tenha sido chamada, ele executa
    if chave_cache not in cache:

        # Executa a função de forma conceitual, onde o getattr acesso o atributo de um objeto usando o nome dele em forma de texto
        # nome_funcao = "cpu_percent"
        # funcao = p.cpu_percent
        funcao = getattr(
            p,
            nome_funcao
        )

        # **argumentos é equivalente a
        # argumentos = {
        #     "logical": True
        # }
        # assim funcao(**argumentos) é equivalente a funcao(logical=True)
        cache[chave_cache] = funcao(
            **argumentos
        )

    return cache[chave_cache]


# O retorno seria parecido com isso
# cache = {
#     ("virtual_memory", ()):
#         svmem(
#             total=16777216000,
#             available=8245000000,
#             percent=50.8,
#             used=7450000000,
#             free=2100000000
#         ),
#
#     (
#         "cpu_count",
#         (
#             ("logical", True),
#         )
#     ): 16,
#
#     (
#         "cpu_count",
#         (
#             ("logical", False),
#         )
#     ): 8
# }


# Verifica qual campo deve ser retornado da função do psutil
def extrair_retorno(
    resultado,
    metrica
):
    atributo = metrica.get(
        "atributo_retorno"
    )

    indice = metrica.get(
        "indice_retorno"
    )

    # Equivalente a resultado = resultado.atributo, porque algumas funções do psutil retornam objetos com atributos
    if atributo is not None:
        resultado = getattr(
            resultado,
            atributo
        )

    # Equivalente a resultado = resultado[indice], porque algumas funções do psutil retornam listas ou tuplas
    if indice is not None:
        resultado = resultado[indice]

    return resultado


def capturar_metrica(
    metrica,
    cache
):
    try:

        # Executa a função do psutil
        resultado = executar_funcao(
            metrica,
            cache
        )

        # Pega o valor específico desejado
        resultado = extrair_retorno(
            resultado,
            metrica
        )

        return resultado

    except (
        AttributeError,
        IndexError,
        TypeError,
        OSError
    ) as erro:

        print(
            f"Não foi possível capturar "
            f"{metrica['nome_coluna']}: "
            f"{erro}"
        )

        return None


def capturar_dados(
    metricas,
    endereco_mac
):
    # Criar um cache para armazenar os resultados a cada rodada e evitar chamadas repetidas
    cache = {}

    dados = {
        "TIMESTAMP": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        "ENDERECO_MAC": endereco_mac
    }

    for metrica in metricas:
        coluna = metrica["nome_coluna"]

        dados[coluna] = capturar_metrica(
            metrica,
            cache
        )

    return dados


config = carregar_config()


token_instalacao = config.get(
    "token_instalacao"
)

token_perfil_servidor = config.get(
    "token_perfil_servidor"
)


if token_instalacao is None:
    print(
        "Token de instalação não encontrado "
        "no arquivo de configuração."
    )

    exit()


if token_perfil_servidor is None:
    print(
        "Token do perfil do servidor não encontrado "
        "no arquivo de configuração."
    )

    exit()


endereco_mac = obter_endereco_mac()

if endereco_mac is None:
    print(
        "Não foi possível identificar "
        "o endereço MAC da máquina."
    )

    exit()


agente_ativado = ativar_agente(
    token_instalacao,
    token_perfil_servidor,
    endereco_mac
)

if not agente_ativado:
    print("Não foi possível ativar o agente.")
    exit()


metricas = buscar_metricas_cliente(
    token_instalacao,
    token_perfil_servidor
)

if not metricas:
    print(
        "Nenhuma métrica configurada "
        "para este perfil."
    )

    exit()


# Colunas padrão
colunas = [
    "TIMESTAMP",
    "ENDERECO_MAC"
]


# Colunas das métricas do banco
colunas.extend(
    metrica["nome_coluna"]
    for metrica in metricas
)


mac_arquivo = endereco_mac.replace(
    ":",
    "-"
)

caminho_arquivo = (
    f"./coretrace_coleta_{mac_arquivo}.csv"
)


arquivo_existe = os.path.exists(
    caminho_arquivo
)

arquivo = open(
    caminho_arquivo,
    "a",
    newline="",
    encoding="utf-8"
)


escritor = csv.DictWriter(
    arquivo,
    fieldnames=colunas
)


if not arquivo_existe:
    escritor.writeheader()


try:
    while True:
        dados = capturar_dados(
            metricas,
            endereco_mac
        )

        escritor.writerow(
            dados
        )

        arquivo.flush()

        print(
            f"{dados['TIMESTAMP']} - "
            f"Captura salva para o MAC "
            f"{endereco_mac}: "
            f"{dados}"
        )

        time.sleep(1)


except KeyboardInterrupt:
    print("\nMonitoramento encerrado.")


finally:
    arquivo.close()
