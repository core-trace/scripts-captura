import psutil as p
import csv
import time
import os
import json
import uuid # para pegar o endereço mac da máquina
import requests # add pra comunicação com a api
import socket # add pra pegar o nome do pc
from datetime import datetime

# Configurar as variáveis de ambiente pra API pra cahamar commais facilidade posteriormente
API_BASE_URL = "http://localhost:3333"

# Token de instalação ou token do node, que será salvo no arquivo de configuração
ARQUIVO_CONFIG = "config.json"

NOME_MAQUINA = socket.gethostname()
NOME_USUARIO = os.environ.get('USER')

def obter_endereco_mac():
    interfaces = p.net_if_addrs()

    for nome_interface, enderecos in interfaces.items():
        for endereco in enderecos:

            # family é o tipo de endereço, p.AF_LINK é o tipo de endereço MAC
            if endereco.family == p.AF_LINK:
                if endereco.address != "00:00:00:00:00:00":
                    return endereco.address.upper()

    return None

# Função para carregar o arquivo de configuração
def carregar_config():
    if not os.path.exists(ARQUIVO_CONFIG):
        print(f"Arquivo de configuração '{ARQUIVO_CONFIG}' não encontrado.")
        exit()

    with open(ARQUIVO_CONFIG, 'r', encoding='utf-8') as arquivo:
        return json.load(arquivo)

# Função para salvar o token do node no arquivo de configuração
def salvar_config(config):
    with open(ARQUIVO_CONFIG, "w", encoding="utf-8") as arquivo:
        json.dump(config, arquivo, indent=4)

# Função para verificar se o node já está registrado
def ativarAgente(token_instalacao):
    endereco_mac = obter_endereco_mac()

    dados = {
        "tokenInstalacaoServer": token_instalacao,
        "hostnameServer": NOME_MAQUINA,
        "enderecoMacServer": endereco_mac
    }

    resposta = requests.post(f"{API_BASE_URL}/servidor/ativarAgente", json=dados)

    if resposta.status_code == 200:
        retorno = resposta.json()

        print(f"Agente ativado no MAC {endereco_mac}")

        return retorno.get("tokenServidor")

    print("Erro ao ativar agente")
    print(resposta.text)

    return None

def buscar_metricas_cliente(token_servidor):
    # Usa a nossa API pra pegar os "comando_parametro" que foram cadastrados pra empresa através do token do node
    try:
        resposta = requests.get(
            f"http://localhost:3334/empresa/metricas",
            headers={
                "Authorization": f"Bearer {token_servidor}"
            }
        )

        if resposta.status_code == 200:
            metricas = resposta.json()
            # Seleciona as métricas que vieram do model da empresa
            print(f"{len(metricas)} métricas carregadas.\n")
            return metricas
        else:
            print(f"Houve um erro ao consultar as métricas: ({resposta.status_code}).")
            return []

    except Exception as e:
        print(f"Falha de conexão com a API: {e}")
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
# Precisa virar -> p.cpu_count(logica=true)
#
# Monta os parâmetros que são enviados para a função do psutil
def montar_argumentos(metrica):
    nome = metrica.get("argumento_nome")
    valor = converter_valor(metrica.get("argumento_valor"))

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
        funcao = getattr(p, nome_funcao)

        # **argumentos é equivalente a 
        # argumentos = {
        #     "logical": True
        # }
        # assim funcao(**argumentos) é equivalente a funcao(logical=True)
        cache[chave_cache] = funcao(**argumentos)

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

#     (
#         "cpu_count",
#         (
#             ("logical", True),
#         )
#     ): 16,

#     (
#         "cpu_count",
#         (
#             ("logical", False),
#         )
#     ): 8
# }

# Verifica qual campo deve ser retornado da função do psutil
def extrair_retorno(resultado, metrica):
    atributo = metrica.get("atributo_retorno")
    indice = metrica.get("indice_retorno")

    # Equivalente a resultado = resultado.atributo, porque algumas funções do psutil retornam objetos com atributos
    if atributo is not None:
        resultado = getattr(resultado, atributo)

    # Equivalente a resultado = resultado[indice], porque algumas funções do psutil retornam listas ou tuplas
    if indice is not None:
        resultado = resultado[indice]

    return resultado

def capturar_metrica(metrica, cache):
    try:

        # Executa a função do psutil
        resultado = executar_funcao(metrica, cache)

        # Pega o valor específico desejado
        resultado = extrair_retorno(resultado, metrica)

        return resultado

    except (
        AttributeError,
        IndexError,
        TypeError,
        OSError
    ) as erro:

        print(
            f"Não foi possível capturar "
            f"{metrica['nome_coluna']}: {erro}"
        )

        return None

def capturar_dados(metricas):
    # Criar um cache para armazenar os resultados a cada rodada e evitar chamadas repetidas
    cache = {}

    dados = {
        "TIMESTAMP": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "USUARIO": NOME_USUARIO,
        "HOSTNAME": NOME_MAQUINA
    }

    for metrica in metricas:
        coluna = metrica["nome_coluna"]

        dados[coluna] = capturar_metrica(metrica, cache)

    return dados

config = carregar_config()

token_servidor = config.get("token_servidor")

if token_servidor is None:
    token_instalacao = config.get("token_instalacao")

    if token_instalacao is None:
        print("Token de instalação não encontrado no arquivo de configuração.")
        exit()

    print("Primeira execução do agente. Ativando...")

    token_servidor = ativarAgente(token_instalacao)

    if token_servidor is None:
        print("Não foi possível ativar o agente.")
        exit()

    config = {
        "token_servidor": token_servidor
    }

    salvar_config(config)

    print("Agente ativado com sucesso.")

else:
    print("Agente já ativado. Token do node encontrado.")

metricas = buscar_metricas_cliente(token_servidor)

if not metricas:
    print("Nenhuma métrica configurada")
    exit()

# Colunas padrão
colunas = [
        "TIMESTAMP",
        "USUARIO",
        "HOSTNAME"
]

# Colunas das métricas do banco
colunas.extend(metrica["nome_coluna"] for metrica in metricas)

caminho_arquivo = f'./coretrace_coleta_{NOME_MAQUINA}.csv'

arquivo_existe = os.path.exists(caminho_arquivo)
arquivo = open(caminho_arquivo, "a", newline="", encoding="utf-8")

escritor = csv.DictWriter(arquivo, fieldnames=colunas)

if not arquivo_existe:
    escritor.writeheader()

try:
    while True:
        dados = capturar_dados(metricas)

        escritor.writerow(dados)
        arquivo.flush()

        print(f"{dados['TIMESTAMP']} - Captura salva na máquina {NOME_MAQUINA}: {dados}")

        time.sleep(1)

except KeyboardInterrupt:
    print("\nMonitoramento encerrado.")

finally:
    arquivo.close()
