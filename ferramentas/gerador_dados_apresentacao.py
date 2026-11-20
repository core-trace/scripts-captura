"""Gera os dados do cenário Black Friday e sobe de verdade para o S3 (prefixo raw/).

Este é o gerador "de produção" da apresentação final. Ele é a evolução de
`ferramentas/gerador_cenario_black_friday.py` (que só rodava a esteira Silver/Gold
com uma pasta local e não subia nada para a AWS). As diferenças em relação ao
gerador antigo são duas:

1. Esquema completo de métricas: o gerador antigo escrevia só as 20 colunas que
   importavam para a narrativa (cpu_percent, ram_percent, ...). Este escreve
   TODAS as colunas que o agente real (`capturas/captura.py`) escreveria para o
   perfil de cada servidor. Para isso o cabeçalho é montado chamando as próprias
   funções `colunas_cpu`, `colunas_ram`, `colunas_swap`, `colunas_carga`,
   `colunas_rede` e `colunas_disco` do agente - assim o nome de cada coluna
   (e a ordem delas) bate com o CSV real por construção, sem precisar copiar
   nenhuma lista "na mão".

2. Upload real: os CSVs são enviados com boto3 para o bucket indicado pela
   variável de ambiente BUCKET_DESTINO, dentro do prefixo `raw/`, de onde a
   Silver (`silver.py`) lê e classifica cada arquivo pelo conteúdo das colunas.
   As métricas e os processos vão para `raw/`, e os containers para
   `raw/docker/metricas/` - é o mesmo desenho dos agentes reais (o de métricas
   sobe em `raw/`, o de Docker sobe em `raw/docker/<tipo>/`). A Silver varre
   `raw/` inteiro, então funciona dos dois jeitos.

O QUE É CENÁRIO E O QUE É PREENCHIMENTO
--------------------------------------
A fonte da verdade dos números é `Contexto/cenario-mestre-black-friday.md`:

* Os 4 servidores "narrados" (pay-api-04, cart-api-07, cart-api-12 e pay-db-02)
  usam EXATAMENTE as curvas de CPU/RAM já definidas no gerador antigo (as âncoras
  estão copiadas aqui, sem nenhuma alteração).
* Todo o resto - disco, rede, swap, carga média, os sub-campos de CPU/RAM
  (cpu_user, cpu_system, cpu_idle, memória disponível/livre/usada, frequência,
  uso por núcleo lógico, inodes, etc.) e os 6 servidores sem papel na narrativa -
  é PREENCHIMENTO REALISTA: fica sempre abaixo de qualquer limite de atenção ou
  crítico configurado no perfil, com um jitter pequeno e aleatório para não
  parecer uma linha reta artificial. Esses valores não fazem parte do que o
  cenário mestre narra; eles existem só para o CSV ficar completo e crível, do
  jeito que um agente real produziria. Se algum deles cruzar um limite, a
  severidade do servidor na Gold mudaria - e a conferência no fim do script
  (`conferir_cenario`) avisa se isso acontecer.

PROCESSOS E CONTAINERS
----------------------
* Os 5 processos do cenário mestre (java, worker, node, python, nginx) são usados
  exatamente como estão, inclusive o PID e a evolução de CPU/RAM. O CSV guarda
  a memória do processo em PORCENTAGEM (é a coluna do psutil), então o RSS em GB
  do cenário mestre é convertido usando a RAM total do servidor.
* Os 6 servidores que o cenário mestre NÃO define ganham um processo plausível
  e coerente com o perfil (node no Carrinho, python no Catálogo, mysqld no
  pay-db-02, java leve no pay-api-06). Isso é EXTRAPOLAÇÃO, está marcado como
  tal no código e não deve ser lido como número oficial do cenário.
* Containers: só o pay-api-04 (3) e o cart-api-07 (4) geram containers, como no
  cenário mestre. Os outros 8 servidores não geram - o perfil Catálogo tem
  monitorar_docker = FALSE e, nos outros, dado de container é específico demais
  para ser inventado.

REGRAS QUE O CSV PRECISA CUMPRIR (e que o script respeita)
-----------------------------------------------------------
* Toda linha de métrica tem TOKEN_INSTALACAO e TOKEN_PERFIL_SERVIDOR preenchidos,
  porque a Gold (branch feat/tempo-no-estado-gold) usa esses tokens para buscar
  os limiares do perfil na API e calcular "tempo no estado" e "severidade".
* Nenhum servidor pode ter OOM (ram_oom_kills = 0), porque qualquer OOM do dia
  força severidade = "critico" no resumo (regra em `calcular_severidade`).
* Os limites de atenção/crítico ficam no banco (database/script.sql), não no
  código. As tabelas abaixo servem só para a conferência final do script.

COMO USAR
---------
    # gera os CSVs e sobe para o S3 (precisa de BUCKET_DESTINO no ambiente)
    BUCKET_DESTINO=coretrace-teste-marcelly-757158332486 python ferramentas/gerador_dados_apresentacao.py

    # gera os CSVs localmente, sem mexer na AWS (bom pra conferir antes)
    python ferramentas/gerador_dados_apresentacao.py --somente-local

    # gera, confere e mostra algumas linhas no console
    python ferramentas/gerador_dados_apresentacao.py --somente-local --amostras 3
"""

import argparse
import csv
import hashlib
import io
import os
import random
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A raiz do projeto entra no path para conseguir importar os módulos do próprio
# agente (cpu.coleta, ram.coleta, ...) e reaproveitar as funções que montam o
# cabeçalho do CSV.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from carga.coleta import colunas_carga, preparar_coleta_carga
from cpu.coleta import colunas_cpu, preparar_coleta_cpu
from disco.coleta import colunas_disco, preparar_coleta_disco
from processos.processos import COLUNAS_PROCESSOS
from ram.coleta import colunas_ram, preparar_coleta_ram
from rede.coleta import colunas_rede, preparar_coleta_rede
from swap.coleta import colunas_swap, preparar_coleta_swap

# As colunas do CSV de containers são as do coletor de Docker do próprio projeto
# (capturas/containers/metricas.py). Importar (e não copiar) garante que o
# arquivo gerado tem exatamente o mesmo formato que o agente real produz.
from capturas.containers.metricas import COLUNAS_METRICAS as COLUNAS_CONTAINERS

# Para a conferência final usamos as MESMAS funções que a Gold usa
# (resumos_gold.py). Assim o script não reimplementa a regra de classificação:
# se a regra mudar lá, a conferência daqui continua dizendo a verdade.
from resumos_gold import calcular_severidade, calcular_tempo_no_estado, classificar_estado


# ===========================================================================
# 1. Identificação do cenário (Contexto/cenario-mestre-black-friday.md)
# ===========================================================================

DIA = "2026-11-27"

# Janela de tempo: uma leitura por minuto, das 18h45 às 20h30 (fim incluso).
INICIO = datetime(2026, 11, 27, 18, 45)
FIM = datetime(2026, 11, 27, 20, 30)
INTERVALO_SEGUNDOS = 60

# Empresa iFood (database/script.sql). Os três perfis pertencem à mesma empresa,
# por isso o token de instalação é o mesmo; o token de perfil é que define os
# limites de cada um.
TOKEN_INSTALACAO_IFOOD = "123"
TOKENS_PERFIL = {
    "Carrinho": "321",
    "Financeiro": "322",
    "Catálogo": "323",
}

# Prefixos do S3. O agente de métricas/processos sobe em `raw/` e o agente de
# Docker sobe em `raw/docker/<tipo>/`. A Silver varre `raw/` inteiro, então
# qualquer um dos dois funciona - mantivemos o mesmo desenho dos agentes reais.
PREFIXO_S3 = "raw"
PREFIXO_S3_CONTAINERS = "raw/docker/metricas"

# Semente fixa do gerador: a mesma execução produz sempre os mesmos números.
# Importante para o dia da apresentação - dá para comparar duas execuções e
# depurar sem os números "mudarem sozinhos".
SEMENTE = 20261127

# Horário de Brasília (UTC-3). O Brasil não tem horário de verão desde 2019,
# então o deslocamento é fixo. Usado só para escrever o timestamp dos containers
# em UTC, que é o formato do agente de Docker.
FUSO_BRASILIA = timezone(timedelta(hours=-3))

# Status "aquecendo" é o que o agente real grava na primeira leitura de uma
# métrica que depende de uma leitura anterior (taxas e contadores). Ver
# `utils/coleta.py` -> salvar_leitura e `cpu/coleta.py` -> coletar_percentuais.
STATUS_AQUECENDO = "aquecendo"
STATUS_OK = "ok"


# ===========================================================================
# 2. Limiares do cenário (database/script.sql)
# ===========================================================================
#
# Estes valores NÃO são usados para gerar os dados: quem decide o que é
# atenção/crítico é o banco, lido pela API /empresa/metricas na hora em que a
# Gold monta o resumo. Eles estão aqui só para a conferência do fim do script
# (e para deixar claro qual é o limite de cada métrica).
#
# Query que gera a lista de métricas de cada perfil:
#
#   SELECT c.nome_coluna, cp.limite_atencao, cp.limite_critico, cp.operador
#   FROM componentes_perfil cp
#   JOIN componentes c ON c.id_componente = cp.fk_componente
#   WHERE cp.fk_perfil_servidor = ?;   -- 1=Carrinho, 2=Financeiro, 3=Catálogo
#
# O operador é '>' (padrão da coluna), ou seja, o limite é "estourado" quando o
# valor é ESTRICTAMENTE maior que o número - é por isso que 92% de CPU no
# Financeiro ainda não é crítico (o crítico é > 92).

LIMITES_POR_METRICA = {
    "cpu_percent": {"Carrinho": (70, 90), "Financeiro": (75, 92), "Catálogo": (75, 90)},
    "ram_percent": {"Carrinho": (75, 90), "Financeiro": (75, 90), "Catálogo": (75, 90)},
    "disk_percent": {"Carrinho": (80, 95), "Financeiro": (80, 95), "Catálogo": (80, 95)},
    "swap_percent": {"Carrinho": (50, 80), "Financeiro": (50, 80), "Catálogo": (50, 80)},
    "load_avg_15": {"Carrinho": (5, 15), "Financeiro": (5, 15), "Catálogo": (5, 15)},
    "net_traffic": {"Carrinho": (10, 30), "Financeiro": (10, 30), "Catálogo": (10, 30)},
}

# Métricas que a Gold compara contra os limites acima: são as que definem a
# severidade do servidor.
METRICAS_COM_LIMITE = list(LIMITES_POR_METRICA.keys())

# Liga o nome da métrica no catálogo (que é o campo "metrica" de cada leitura
# no JSON da Gold) com a coluna correspondente do CSV de métricas.
COLUNA_CSV_POR_METRICA = {
    "cpu_percent": "cpu_percent",
    "ram_percent": "ram_percent",
    "disk_percent": "disco_volume_0_percent",
    "swap_percent": "swap_percent",
    "load_avg_15": "load_avg_15",
}


# ===========================================================================
# 3. Métricas configuradas em cada perfil (database/script.sql)
# ===========================================================================
#
# Os três perfis do cenário (Carrinho=1, Financeiro=2, Catálogo=3) têm
# exatamente o mesmo conjunto de métricas configuradas - são os componentes 1 a
# 25, 30 a 33 e 39 a 44 da tabela componentes_perfil. A lista abaixo é a
# coluna nome_coluna de cada um deles, na mesma ordem que a API devolve
# (ORDER BY c.id_componente).
#
# O que a API devolve é o que o agente usa para montar o CSV: as métricas cujo
# nome está em IDS_CPU/IDS_RAM/IDS_DISCO/IDS_SWAP/IDS_CARGA/IDS_REDE viram
# colunas do grupo correspondente (colunas_cpu, colunas_ram, ...); nenhuma
# delas sobra como coluna solta.

METRICAS_DO_PERFIL = [
    "cpu_percent",
    "cpu_user",
    "cpu_system",
    "cpu_idle",
    "cpu_freq_current",
    "cpu_freq_min",
    "cpu_freq_max",
    "cpu_count_physical",
    "cpu_count_logical",
    "ram_total",
    "ram_available",
    "ram_used",
    "ram_free",
    "ram_percent",
    "disk_total",
    "disk_used",
    "disk_free",
    "disk_percent",
    "swap_total",
    "swap_used",
    "swap_free",
    "swap_percent",
    "swap_sin",
    "swap_sout",
    "cpu_percent_percpu",
    "ram_oom_kills",
    "disk_inodes_percent",
    "disk_io_time",
    "disk_queue",
    "load_avg_5",
    "load_avg_15",
    "net_traffic",
    "net_errors",
    "net_drops",
    "net_isup",
]


# ===========================================================================
# 4. Curvas do cenário (as mesmas do gerador antigo, sem alteração)
# ===========================================================================


def minuto_do_dia(hora, minuto):
    """Usado só para escrever as âncoras de forma legível (ex.: (19, 30, 68))."""
    return hora * 60 + minuto


def interpolar(ancoras, minuto_atual):
    """Interpolação linear entre as âncoras.

    Antes da primeira âncora ou depois da última, o valor fica no da ponta mais
    próxima. É exatamente a mesma conta do gerador antigo.
    """
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
    """Recebe âncoras (hora, minuto, valor) e devolve a função que devolve o
    valor interpolado no horário pedido."""
    ancoras = [
        (minuto_do_dia(hora, minuto), valor)
        for hora, minuto, valor in ancoras_horario
    ]

    def calcular(timestamp):
        return interpolar(ancoras, minuto_do_dia(timestamp.hour, timestamp.minute))

    return calcular


def estavel(base, amplitude, aleatorio):
    """Fica balançando em torno da base, sem seguir tendência de subida.

    É o preenchimento "realista e normal" das métricas que não fazem parte da
    narrativa: fica longe dos limites e ganha um jitter pequeno para não
    parecer uma linha reta.
    """

    def calcular(timestamp):
        return base + aleatorio.uniform(-amplitude, amplitude)

    return calcular


# ===========================================================================
# 5. Os 10 servidores do cenário
# ===========================================================================
#
# MAC, perfil e tokens vêm de database/script.sql (tabela servidores) e do
# cenário mestre. Núcleos (8) e RAM total também vêm do cenário mestre
# (seção 2 do documento).
#
# As curvas de CPU/RAM dos 4 servidores narrados são as do gerador antigo,
# copiadas sem nenhuma alteração.

SERVIDORES = [
    {
        "nome": "cart-api-07",
        "mac": "AA:BB:CC:DD:EE:07",
        "perfil": "Carrinho",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        # Narrado: Carrinho é crítico por CPU a partir de 20h05 (crítico > 90).
        "cpu_ancoras": [(18, 30, 44), (19, 0, 44), (19, 15, 63), (19, 30, 79),
                        (19, 45, 83), (20, 0, 88), (20, 5, 92), (20, 30, 94)],
        "ram_ancoras": [(18, 30, 48), (19, 0, 48), (19, 15, 52), (19, 30, 59),
                        (19, 45, 62), (20, 0, 65), (20, 5, 66), (20, 30, 69)],
        "disco_estavel": (50, 6),
    },
    {
        "nome": "cart-api-12",
        "mac": "AA:BB:CC:DD:EE:12",
        "perfil": "Carrinho",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        # Narrado: sobe só até a atenção (70) e estabiliza, sem NUNCA chegar no
        # crítico (90). É de propósito - é o caso de "previsão de limite".
        "cpu_ancoras": [(18, 30, 40), (19, 0, 45), (19, 15, 50), (19, 30, 58),
                        (19, 45, 65), (19, 50, 70), (20, 0, 73), (20, 5, 75),
                        (20, 30, 76)],
        "ram_estavel": (57, 2),
        "disco_estavel": (50, 6),
    },
    {
        "nome": "cart-api-02",
        "mac": "AA:BB:CC:DD:EE:02",
        "perfil": "Carrinho",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        "cpu_estavel": (40, 5),
        "ram_estavel": (50, 5),
        "disco_estavel": (50, 6),
    },
    {
        "nome": "cart-api-09",
        "mac": "AA:BB:CC:DD:EE:09",
        "perfil": "Carrinho",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        "cpu_estavel": (40, 5),
        "ram_estavel": (50, 5),
        "disco_estavel": (50, 6),
    },
    {
        "nome": "pay-api-04",
        "mac": "AA:BB:CC:DD:EF:04",
        "perfil": "Financeiro",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 32,
        "disco_gb": 200,
        # Narrado: crítico desde 19h45 (CPU > 92 e RAM > 90).
        "cpu_ancoras": [(18, 30, 40), (19, 0, 42), (19, 15, 55), (19, 30, 68),
                        (19, 45, 92), (20, 0, 94), (20, 5, 95), (20, 30, 95)],
        "ram_ancoras": [(18, 30, 50), (19, 0, 55), (19, 15, 70), (19, 30, 83),
                        (19, 45, 91), (20, 0, 92), (20, 5, 93), (20, 30, 93)],
        "disco_estavel": (50, 6),
    },
    {
        "nome": "pay-api-06",
        "mac": "AA:BB:CC:DD:EF:06",
        "perfil": "Financeiro",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        "cpu_estavel": (40, 3),
        "ram_estavel": (50, 3),
        "disco_estavel": (50, 6),
    },
    {
        "nome": "pay-db-02",
        "mac": "AA:BB:CC:DD:EF:02",
        "perfil": "Financeiro",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 32,
        "disco_gb": 200,
        # Narrado: só a RAM sobe, e fica presa na atenção (75) o resto da janela,
        # sem virar crítico.
        "cpu_estavel": (35, 5),
        "ram_ancoras": [(18, 30, 70), (19, 0, 74), (19, 15, 76), (19, 30, 78),
                        (19, 45, 79), (20, 0, 79), (20, 5, 80), (20, 30, 80)],
        "disco_estavel": (50, 6),
    },
    {
        "nome": "catalog-api-03",
        "mac": "AA:BB:CC:DD:F0:03",
        "perfil": "Catálogo",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        "cpu_estavel": (45, 3),
        "ram_estavel": (50, 3),
        "disco_estavel": (50, 6),
        # Esse servidor "para de falar" depois das 18h50 - é o caso de "sem dados
        # recentes" da tela Geral. Por isso ele não gera linha até o fim da janela.
        "fim_captura": datetime(2026, 11, 27, 18, 50),
    },
    {
        "nome": "catalog-api-05",
        "mac": "AA:BB:CC:DD:F0:05",
        "perfil": "Catálogo",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 64,
        "disco_gb": 500,
        "cpu_estavel": (35, 5),
        "ram_estavel": (50, 5),
        # Disco um pouco mais ocupado que os outros, ainda dentro da faixa normal
        # (77% no fim da janela, bem abaixo da atenção que é 80).
        "disco_ancoras": [(18, 30, 60), (20, 30, 77)],
    },
    {
        "nome": "catalog-api-08",
        "mac": "AA:BB:CC:DD:F0:08",
        "perfil": "Catálogo",
        "nucleos_logicos": 8,
        "nucleos_fisicos": 4,
        "ram_gib": 16,
        "disco_gb": 200,
        "cpu_estavel": (35, 5),
        "ram_estavel": (50, 5),
        "disco_estavel": (50, 6),
    },
]

SERVIDORES_POR_NOME = {servidor["nome"]: servidor for servidor in SERVIDORES}


# ===========================================================================
# 6. Processos do cenário
# ===========================================================================
#
# Ocenário mestre, seção 8 - processos NARRADOS (usar exatamente estes, sem
# mudar nada):
#
#   java   PID 4012  pay-api-04      40% -> 98% CPU, 16GB -> 28,5GB RSS
#   worker PID 6220  pay-api-04      30% -> 94% CPU,  8GB -> 18GB RSS
#   node   PID 5122  cart-api-07     10% -> 35% CPU, 1,2GB -> 2,8GB RSS
#   python PID 3301  catalog-api-05   4% ->  5% CPU, 210MB -> 230MB RSS
#   nginx  PID 1050  cart-api-02     10% ->  2% CPU, 180MB -> 128MB RSS
#
# Atenção: o CSV de processos guarda a MEMÓRIA EM PORCENTAGEM (a coluna
# MEMORIA_PERCENT do psutil), e não o RSS em GB. O RSS em GB é o número oficial
# do cenário mestre, então ele é convertido para porcentagem usando a RAM total
# do servidor (32 GiB no pay-api-04, 16 GiB no cart-api-07, 64 GiB no
# catalog-api-05, 16 GiB no cart-api-02).
#
# As âncoras de CPU/RAM abaixo definem COMO o processo chega nos números finais
# do cenário mestre, respeitando os marcos da linha do tempo (seção 4):
# 19h15 o java dá o salto de memória, 19h45 ele passa de 24GB e fica crítico,
# 19h45 o pay-api-04 vira crítico (CPU e RAM).

PROCESSOS_NARRADOS = [
    {
        "servidor": "pay-api-04",
        "pid": 4012,
        "nome": "java",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 40), (19, 0, 45), (19, 15, 58), (19, 30, 75),
                        (19, 45, 90), (20, 0, 94), (20, 15, 96), (20, 30, 98)],
        "rss_ancoras_gb": [(18, 45, 16.0), (19, 0, 16.4), (19, 15, 20.0),
                           (19, 30, 22.5), (19, 45, 24.5), (20, 0, 26.5),
                           (20, 15, 27.8), (20, 30, 28.5)],
    },
    {
        "servidor": "pay-api-04",
        "pid": 6220,
        "nome": "worker",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 30), (19, 0, 35), (19, 15, 48), (19, 30, 64),
                        (19, 45, 82), (20, 0, 88), (20, 15, 91), (20, 30, 94)],
        "rss_ancoras_gb": [(18, 45, 8.0), (19, 0, 8.6), (19, 15, 11.0),
                           (19, 30, 13.2), (19, 45, 15.0), (20, 0, 16.4),
                           (20, 15, 17.2), (20, 30, 18.0)],
    },
    {
        "servidor": "cart-api-07",
        "pid": 5122,
        "nome": "node",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 10), (19, 0, 11), (19, 15, 13), (19, 30, 16),
                        (19, 45, 19), (20, 0, 24), (20, 5, 27), (20, 30, 35)],
        "rss_ancoras_gb": [(18, 45, 1.2), (19, 0, 1.25), (19, 15, 1.4),
                           (19, 30, 1.6), (19, 45, 1.8), (20, 0, 2.2),
                           (20, 5, 2.4), (20, 30, 2.8)],
    },
    {
        "servidor": "catalog-api-05",
        "pid": 3301,
        "nome": "python",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 4.0), (19, 30, 4.4), (20, 0, 4.7), (20, 30, 5.0)],
        "rss_ancoras_gb": [(18, 45, 0.210), (19, 30, 0.218), (20, 0, 0.224),
                            (20, 30, 0.230)],
    },
    {
        "servidor": "cart-api-02",
        "pid": 1050,
        "nome": "nginx",
        "usuario": "www-data",
        "cpu_ancoras": [(18, 45, 10), (19, 0, 8), (19, 30, 5), (20, 0, 3),
                        (20, 30, 2)],
        "rss_ancoras_gb": [(18, 45, 0.180), (19, 0, 0.168), (19, 30, 0.145),
                           (20, 0, 0.134), (20, 30, 0.128)],
    },
]

# Processos EXTRAPOLADOS: os 6 servidores que o cenário mestre NÃO define.
#
# ATENÇÃO - isto aqui NÃO é dado do cenário mestre. É uma extrapolação coerente
# com o perfil de cada servidor, feita só para o CSV de processos não vir com
# buracos (um servidor de verdade tem processo rodando). Os valores são
# normais e estáveis, sem nenhum alerta:
#   - os outros servidores de Carrinho rodam algo parecido com o node do
#     cart-api-07;
#   - os de Catálogo rodam algo parecido com o python do catalog-api-05;
#   - o pay-api-06 (API de pagamento) roda um java mais leve que o do pay-api-04;
#   - o pay-db-02 é banco de dados, então roda mysqld (faz mais sentido que java).
#
# Nenhum PID repetido e nenhum PID do processo "redis-server" (2144), que o
# cenário mestre removeu da Black Friday.

PROCESSOS_EXTRAPOLADOS = [
    {
        "servidor": "cart-api-12",
        "pid": 5128,
        "nome": "node",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 12), (20, 30, 13)],
        "rss_ancoras_gb": [(18, 45, 1.50), (20, 30, 1.58)],
    },
    {
        "servidor": "cart-api-09",
        "pid": 5131,
        "nome": "node",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 11), (20, 30, 12)],
        "rss_ancoras_gb": [(18, 45, 1.40), (20, 30, 1.47)],
    },
    {
        "servidor": "pay-db-02",
        "pid": 2101,
        "nome": "mysqld",
        "usuario": "mysql",
        "cpu_ancoras": [(18, 45, 22), (19, 30, 23), (20, 30, 26)],
        "rss_ancoras_gb": [(18, 45, 9.2), (19, 30, 9.8), (20, 30, 10.6)],
    },
    {
        "servidor": "pay-api-06",
        "pid": 4025,
        "nome": "java",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 17), (20, 30, 19)],
        "rss_ancoras_gb": [(18, 45, 4.10), (20, 30, 4.35)],
    },
    {
        "servidor": "catalog-api-03",
        "pid": 3315,
        "nome": "python",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 4.3), (20, 30, 4.6)],
        "rss_ancoras_gb": [(18, 45, 0.215), (20, 30, 0.225)],
    },
    {
        "servidor": "catalog-api-08",
        "pid": 3322,
        "nome": "python",
        "usuario": "appuser",
        "cpu_ancoras": [(18, 45, 4.0), (20, 30, 4.3)],
        "rss_ancoras_gb": [(18, 45, 0.210), (20, 30, 0.220)],
    },
]


# ===========================================================================
# 7. Containers do cenário (cenário mestre, seção 7)
# ===========================================================================
#
# Só DOIS servidores têm containers no cenário mestre: pay-api-04 (3) e
# cart-api-07 (4). Os outros 8 servidores NÃO geram containers.csv - nem
# cart-api-12, cart-api-02, cart-api-09, pay-db-02 e pay-api-06 (embora o perfil
# deles tenha monitorar_docker = TRUE no banco), nem catalog-api-03,
# catalog-api-05 e catalog-api-08 (o perfil Catálogo tem monitorar_docker =
# FALSE, então container nenhum é esperado lá).
#
# Os valores de CPU/RAM abaixo são os do cenário mestre (às 20h30). Os outros
# campos do CSV de container (contadores acumulados de rede/disco, tamanho da
# imagem, número de processos) são preenchimento realista, no mesmo espírito do
# resto do script.

CONTAINERS = {
    "pay-api-04": [
        {
            "nome": "pay-api-java",
            "funcao": "Processamento de pagamentos",
            "imagem": "pay-api-java:1.4.2",
            "cpu_percent": 380,
            "ram_gib": 24.5,
            "limite_ram_gib": 26.0,
            "pids": 62,
            "rede_mb_s": (2.5, 1.2),
            "disco_mb_s": (18.0, 7.0),
            "imagem_mb": 812,
            "camada_gravavel_mb": 640,
        },
        {
            "nome": "pay-gateway-node",
            "funcao": "Comunicação externa",
            "imagem": "pay-gateway-node:2.1.0",
            "cpu_percent": 90,
            "ram_gib": 3.1,
            "limite_ram_gib": 4.0,
            "pids": 18,
            "rede_mb_s": (6.0, 3.0),
            "disco_mb_s": (4.0, 2.0),
            "imagem_mb": 246,
            "camada_gravavel_mb": 180,
        },
        {
            "nome": "pay-retry-worker",
            "funcao": "Filas e tentativas",
            "imagem": "pay-retry-worker:1.0.9",
            "cpu_percent": 50,
            "ram_gib": 2.7,
            "limite_ram_gib": 3.2,
            "pids": 12,
            "rede_mb_s": (0.8, 0.4),
            "disco_mb_s": (1.5, 0.8),
            "imagem_mb": 198,
            "camada_gravavel_mb": 96,
        },
    ],
    "cart-api-07": [
        {
            "nome": "cart-api-java",
            "funcao": "Requisições de carrinho",
            "imagem": "cart-api-java:3.2.1",
            "cpu_percent": 590,
            "ram_gib": 6.9,
            "limite_ram_gib": 7.4,
            "pids": 48,
            "rede_mb_s": (12.0, 6.0),
            "disco_mb_s": (22.0, 9.0),
            "imagem_mb": 905,
            "camada_gravavel_mb": 720,
        },
        {
            "nome": "cart-worker-node",
            "funcao": "Tarefas auxiliares",
            "imagem": "cart-worker-node:1.8.0",
            "cpu_percent": 100,
            "ram_gib": 1.7,
            "limite_ram_gib": 2.0,
            "pids": 14,
            "rede_mb_s": (1.5, 0.7),
            "disco_mb_s": (3.0, 1.2),
            "imagem_mb": 254,
            "camada_gravavel_mb": 150,
        },
        {
            "nome": "redis-cache",
            "funcao": "Cache temporário",
            "imagem": "redis:7.2-alpine",
            "cpu_percent": 20,
            "ram_gib": 0.8,
            "limite_ram_gib": 1.0,
            "pids": 6,
            "rede_mb_s": (3.0, 3.2),
            "disco_mb_s": (0.6, 0.4),
            "imagem_mb": 41,
            "camada_gravavel_mb": 12,
        },
        {
            # Container que falha: 2 reinicializações (20h11 e 20h16) e
            # encerramento final às 20h18 com código de saída 1.
            "nome": "coupon-sync",
            "funcao": "Sincronização de cupons",
            "imagem": "coupon-sync:1.1.0",
            "cpu_percent": 35,
            "ram_gib": 0.45,
            "limite_ram_gib": 1.0,
            "pids": 9,
            "rede_mb_s": (0.6, 0.3),
            "disco_mb_s": (1.2, 0.6),
            "imagem_mb": 188,
            "camada_gravavel_mb": 84,
            "reinicios": [(20, 11), (20, 16)],
            "encerramento": (20, 18),
            "codigo_saida": 1,
        },
    ],
}


# ===========================================================================
# 8. A "máquina" do cenário (fonte falsa para as funções de coleta)
# ===========================================================================


class Particao:
    """Substitui a partição que o psutil devolveria em uma máquina Linux."""

    def __init__(self, device, mountpoint, fstype):
        self.device = device
        self.mountpoint = mountpoint
        self.fstype = fstype


class MaquinaDoCenario:
    """Fonte de dados do cenário, usada só para montar o cabeçalho do CSV.

    O agente real descobre a topologia da máquina (quantos núcleos lógicos,
    quais volumes, quais discos, quais interfaces de rede) e usa essa
    informação para nomear as colunas. Como aqui as máquinas são fictícias,
    esta classe descreve uma máquina padrão do cenário: 8 núcleos lógicos,
    4 físicos, um volume em "/", um disco "sda" e as interfaces "eth0" e "lo".
    """

    def cpu_count(self, logical=True):
        return 8 if logical else 4

    def disk_partitions(self, all=False):
        return [Particao("/dev/sda1", "/", "ext4")]

    def disk_io_counters(self, perdisk=True, nowrap=False):
        return {"sda": None}

    def net_if_stats(self):
        return {"eth0": None, "lo": None}

    def net_io_counters(self, pernic=True, nowrap=False):
        return {"eth0": None, "lo": None}


# ===========================================================================
# 9. Utilidades
# ===========================================================================


def gerador_aleatorio(chave):
    """Cada servidor, processo ou container tem o seu próprio gerador.

    Assim o jitter é reprodutível (a mesma execução gera sempre os mesmos
    números) e não depende da ordem em que as coisas são processadas.
    """
    return random.Random(f"{SEMENTE}-{chave}")


def horarios_da_janela():
    """Uma leitura por minuto, de 18h45 às 20h30 (fim incluso)."""
    horarios = []
    atual = INICIO
    while atual <= FIM:
        horarios.append(atual)
        atual += timedelta(minutes=1)
    return horarios


def para_utc(horario):
    """Converte o horário de Brasília (naive) para UTC, no formato do agente
    de Docker (ISO 8601 com milissegundos e deslocamento)."""
    return horario.replace(tzinfo=FUSO_BRASILIA).astimezone(timezone.utc)


def para_timestamp_iso(horario):
    """Timestamp no formato do agente de Docker."""
    return para_utc(horario).isoformat(timespec="milliseconds")


def id_do_container(mac, nome):
    """Identificador de 64 caracteres, como o Docker gera. É determinístico
    para o mesmo servidor/container."""
    return hashlib.sha256(f"{mac}-{nome}".encode("utf-8")).hexdigest()


def boot_id(mac):
    """Identificador de boot no formato do /proc/sys/kernel/random/boot_id
    (32 caracteres hexadecimais). Fixo por servidor, como no Linux."""
    return hashlib.md5(f"boot-{mac}".encode("utf-8")).hexdigest()


def gib_para_gb(valor_em_gib):
    """Converte GiB (como o cenário mestre escreve) para GB decimal (como o
    agente grava)."""
    return valor_em_gib * (1024 ** 3) / 1_000_000_000


def leitura_com_amostra_anterior(valor, primeira_leitura):
    """Taxas e contadores só valem a partir da segunda leitura.

    É o que o agente real faz: na primeira rodada o status fica "aquecendo"
    (sem valor), porque ainda não existe a leitura anterior para calcular a
    diferença. Ver `utils/coleta.py` -> salvar_leitura.
    """
    if primeira_leitura:
        return None, STATUS_AQUECENDO
    return valor, STATUS_OK


def distribuir_por_nucleo(uso_total, quantidade, aleatorio):
    """Reparte o uso de CPU entre os núcleos lógicos.

    Com a máquina ociosa o uso fica espalhado entre os núcleos; com a máquina
    saturada os núcleos convergem para perto de 100% - é o que acontece de
    verdade quando o servidor está no limite.
    """
    if uso_total <= 0:
        return [0.0] * quantidade

    dispersao = max(0.03, 0.42 * (1 - uso_total / 100))

    valores = []
    for _ in range(quantidade):
        desvio = aleatorio.uniform(-dispersao, dispersao)
        valores.append(max(0.0, min(100.0, uso_total * (1 + desvio))))

    # A soma volta para o total medido no conjunto (é o que o psutil mede).
    soma = sum(valores)
    if soma > 0:
        valores = [valor * uso_total / soma for valor in valores]

    return [round(valor, 2) for valor in valores]


def valores_de_cpu(uso_total, aleatorio):
    """Separa o uso de CPU em usuário/sistema/ociosa.

    O psutil mede o modo usuário e o modo sistema como porcentagem do tempo
    total de CPU, e o ocioso é o que sobrou. Como o total de CPU do agente já
    é a fração ocupada da máquina, o ocioso é 100 - (usuário + sistema).
    """
    fracao_usuario = aleatorio.uniform(0.68, 0.78)
    fracao_sistema = aleatorio.uniform(0.18, 0.26)

    usuario = round(uso_total * fracao_usuario, 2)
    sistema = round(uso_total * fracao_sistema, 2)
    ociosa = round(max(0.0, 100.0 - usuario - sistema), 2)

    return usuario, sistema, ociosa


def valores_de_ram(percentual, ram_total_gb):
    """Deriva os números de memória a partir do percentual de uso.

    O psutil calcula o percentual a partir da memória DISPONÍVEL
    (percentual = (total - disponível) / total), e a memória usada é o que
    sobra depois de descontar a livre e a cache recuperável. Mantemos essa
    conta para que os quatro números batam entre si (used + free + cache =
    total), mesmo com a cache não sendo uma coluna do perfil.
    """
    disponivel = ram_total_gb * (1 - percentual / 100)
    livre = disponivel * 0.35
    cache = disponivel - livre
    usada = ram_total_gb - livre - cache

    return {
        "ram_available": round(disponivel, 3),
        "ram_free": round(livre, 3),
        "ram_used": round(usada, 3),
    }


def carga_media(uso_cpu, nucleos, aleatorio):
    """Carga média coerente com o uso de CPU.

    Uma máquina com 8 núcleos a 100% tem carga ~8. Mantemos um fator 0.55
    (carga média costuma ser menor que o uso bruto, porque nem toda CPU fica
    ocupada o tempo todo) e, principalmente, mantemos abaixo de 5 - que é o
    limite de atenção da carga média de 15 minutos nos três perfis. Se a carga
    cruzasse esse limite, todo servidor apareceria "em atenção" na tela.
    """
    base = (uso_cpu / 100) * nucleos * 0.55
    valor = base + aleatorio.uniform(-0.12, 0.12)
    return round(max(0.05, min(valor, 4.6)), 2)


def cabecalho_metricas():
    """Monta o cabeçalho do CSV de métricas exatamente como o agente real
    monta em `capturas/captura.py` -> main().

    Ordem: as quatro colunas fixas, depois CPU, RAM, Swap, Carga, Rede e Disco.
    As funções `colunas_*` são as do próprio agente, então o nome e a ordem de
    cada coluna batem com o CSV real por construção.
    """
    maquina = MaquinaDoCenario()

    estado_cpu = preparar_coleta_cpu(METRICAS_DO_PERFIL, fonte=maquina)
    estado_ram = preparar_coleta_ram(METRICAS_DO_PERFIL, fonte=maquina)
    estado_disco = preparar_coleta_disco(METRICAS_DO_PERFIL, fonte=maquina)
    estado_swap = preparar_coleta_swap(METRICAS_DO_PERFIL, fonte=maquina)
    estado_carga = preparar_coleta_carga(METRICAS_DO_PERFIL, fonte=maquina)
    estado_rede = preparar_coleta_rede(METRICAS_DO_PERFIL, fonte=maquina)

    colunas = [
        "TIMESTAMP",
        "ENDERECO_MAC",
        "TOKEN_INSTALACAO",
        "TOKEN_PERFIL_SERVIDOR",
    ]
    colunas.extend(colunas_cpu(estado_cpu))
    colunas.extend(colunas_ram(estado_ram))
    colunas.extend(colunas_swap(estado_swap))
    colunas.extend(colunas_carga(estado_carga))
    colunas.extend(colunas_rede(estado_rede))
    colunas.extend(colunas_disco(estado_disco))

    return colunas


# ===========================================================================
# 10. Geração das métricas
# ===========================================================================


def curvas_do_servidor(servidor, aleatorio):
    """Junta a curva (ou o valor estável) de cada métrica do servidor."""
    if "cpu_ancoras" in servidor:
        cpu = curva(servidor["cpu_ancoras"])
    else:
        base, amplitude = servidor["cpu_estavel"]
        cpu = estavel(base, amplitude, aleatorio)

    if "ram_ancoras" in servidor:
        ram = curva(servidor["ram_ancoras"])
    else:
        base, amplitude = servidor["ram_estavel"]
        ram = estavel(base, amplitude, aleatorio)

    if "disco_ancoras" in servidor:
        disco = curva(servidor["disco_ancoras"])
    else:
        base, amplitude = servidor["disco_estavel"]
        disco = estavel(base, amplitude, aleatorio)

    return cpu, ram, disco


def linha_de_metricas(servidor, horario, primeira_leitura, aleatorio, estado):
    """Monta UMA linha do CSV de métricas, com o mesmo formato do agente real.

    `estado` carrega o que é fixo do servidor (RAM total, swap, frequência,
    identificador de boot) e o que precisa continuar entre as leituras
    (as janelas da carga média).
    """
    timestamp = horario.strftime("%Y-%m-%d %H:%M:%S")

    cpu_percent = round(estado["cpu"](horario), 2)
    ram_percent = round(estado["ram"](horario), 2)
    disco_percent = round(estado["disco"](horario), 2)

    # ---------------------------------------------------------------- CPU
    usuario, sistema, ociosa = valores_de_cpu(cpu_percent, aleatorio)
    nucleos = distribuir_por_nucleo(cpu_percent, servidor["nucleos_logicos"], aleatorio)

    # A frequência sobe um pouco com a carga (turbo) e desce com a ociosidade.
    frequencia = round(
        estado["freq_min"]
        + (estado["freq_max"] - estado["freq_min"]) * (0.55 + 0.45 * cpu_percent / 100)
        + aleatorio.uniform(-8, 8),
        2,
    )

    # ---------------------------------------------------------------- RAM
    ram = valores_de_ram(ram_percent, estado["ram_total_gb"])

    # --------------------------------------------------------------- Swap
    swap_percent = round(aleatorio.uniform(0.5, 4.5), 2)
    swap_used = round(estado["swap_total_gb"] * swap_percent / 100, 3)
    swap_free = round(estado["swap_total_gb"] - swap_used, 3)

    # Entrada/saída de swap: valores pequenos (o servidor não está com
    # pressão de memória). São taxas, então dependem da leitura anterior.
    swap_entrada, status_swap_entrada = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0, 800), 2), primeira_leitura
    )
    swap_saida, status_swap_saida = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0, 800), 2), primeira_leitura
    )

    # --------------------------------------------------------------- Carga
    carga5 = carga_media(cpu_percent, servidor["nucleos_logicos"], aleatorio)
    # A carga de 15 minutos é a média das últimas leituras de 5 minutos (a
    # janela fica em `estado["cargas"]`, que guarda no máximo 15 valores).
    estado["cargas"].append(carga5)
    carga15 = round(sum(estado["cargas"]) / len(estado["cargas"]), 2)

    # --------------------------------------------------------------- Rede
    # ATENÇÃO AO LIMITE: no banco, net_traffic tem atenção em 10 e crítico em
    # 30 (bytes/s). Qualquer tráfego real de uma API estouraria esse limite,
    # então o tráfego das interfaces fica em valores baixos e estáveis - é o
    # que o gerador antigo fazia também, pelo mesmo motivo. Mantemos a soma
    # (recebido + enviado) bem abaixo de 10.
    rede_recebida, status_rede_recebida = leitura_com_amostra_anterior(
        round(aleatorio.uniform(2.25, 2.75), 2), primeira_leitura
    )
    rede_enviada, status_rede_enviada = leitura_com_amostra_anterior(
        round(aleatorio.uniform(2.25, 2.75), 2), primeira_leitura
    )
    rede_loop_recebida, status_loop_recebida = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0.05, 0.15), 2), primeira_leitura
    )
    rede_loop_enviada, status_loop_enviada = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0.05, 0.15), 2), primeira_leitura
    )

    # ---------------------------------------------------------------- Disco
    disco_total = estado["disco_total_gb"]
    disco_used = round(disco_total * disco_percent / 100, 3)
    disco_free = round(disco_total - disco_used, 3)

    inodes_percent = round(aleatorio.uniform(11, 19), 2)
    inodes_total = estado["inodes_total"]
    inodes_free = int(inodes_total * (1 - inodes_percent / 100))
    inodes_available = max(0, inodes_free - 1024)

    # Latência média de leitura/escrita e fila de I/O. São valores derivados
    # de contadores, então a primeira leitura fica "aquecendo".
    disco_leitura, status_disco_leitura = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0.6, 3.2), 2), primeira_leitura
    )
    disco_escrita, status_disco_escrita = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0.8, 3.8), 2), primeira_leitura
    )
    disco_fila, status_disco_fila = leitura_com_amostra_anterior(
        round(aleatorio.uniform(0.0, 1.4), 2), primeira_leitura
    )

    # Intervalos entre leituras: só existem a partir da segunda leitura.
    intervalo = None if primeira_leitura else float(INTERVALO_SEGUNDOS)

    linha = {
        "TIMESTAMP": timestamp,
        "ENDERECO_MAC": servidor["mac"],
        "TOKEN_INSTALACAO": TOKEN_INSTALACAO_IFOOD,
        "TOKEN_PERFIL_SERVIDOR": TOKENS_PERFIL[servidor["perfil"]],
        # ------------------------------------------------------------- CPU
        "cpu_percent": cpu_percent,
        "cpu_percent_status": STATUS_OK,
        "cpu_user": usuario,
        "cpu_user_status": STATUS_OK,
        "cpu_system": sistema,
        "cpu_system_status": STATUS_OK,
        "cpu_idle": ociosa,
        "cpu_idle_status": STATUS_OK,
        "cpu_freq_current": frequencia,
        "cpu_freq_current_status": STATUS_OK,
        "cpu_count_logical": servidor["nucleos_logicos"],
        "cpu_count_logical_status": STATUS_OK,
        "cpu_count_physical": servidor["nucleos_fisicos"],
        "cpu_count_physical_status": STATUS_OK,
        "cpu_freq_min": estado["freq_min"],
        "cpu_freq_min_status": STATUS_OK,
        "cpu_freq_max": estado["freq_max"],
        "cpu_freq_max_status": STATUS_OK,
        "cpu_interval_seconds": intervalo,
        "cpu_schema_version": "cpu-1",
        # ------------------------------------------------------------- RAM
        "ram_percent": ram_percent,
        "ram_percent_status": STATUS_OK,
        "ram_available": ram["ram_available"],
        "ram_available_status": STATUS_OK,
        "ram_total": estado["ram_total_gb"],
        "ram_total_status": STATUS_OK,
        # Nenhum OOM no cenário: qualquer OOM do dia forçaria severidade
        # "crítico" no resumo do servidor (regra em resumos_gold.py).
        "ram_oom_kills": 0 if not primeira_leitura else None,
        "ram_oom_kills_status": STATUS_OK if not primeira_leitura else STATUS_AQUECENDO,
        "ram_used": ram["ram_used"],
        "ram_used_status": STATUS_OK,
        "ram_free": ram["ram_free"],
        "ram_free_status": STATUS_OK,
        "ram_oom_boot_id": estado["boot_id"],
        "ram_oom_interval_seconds": intervalo,
        # ------------------------------------------------------------ Swap
        "swap_percent": swap_percent,
        "swap_percent_status": STATUS_OK,
        "swap_sin": swap_entrada,
        "swap_sin_status": status_swap_entrada,
        "swap_sout": swap_saida,
        "swap_sout_status": status_swap_saida,
        "swap_used": swap_used,
        "swap_used_status": STATUS_OK,
        "swap_free": swap_free,
        "swap_free_status": STATUS_OK,
        "swap_total": estado["swap_total_gb"],
        "swap_total_status": STATUS_OK,
        "swap_interval_seconds": intervalo,
        # ------------------------------------------------------------ Carga
        "load_avg_5": carga5,
        "load_avg_5_status": STATUS_OK,
        "load_avg_15": carga15,
        "load_avg_15_status": STATUS_OK,
        # ------------------------------------------------------------- Rede
        "rede_coleta_status": STATUS_OK,
        "rede_eth0_interval_seconds": intervalo,
        "rede_eth0_bytes_recv": rede_recebida,
        "rede_eth0_bytes_recv_status": status_rede_recebida,
        "rede_eth0_bytes_sent": rede_enviada,
        "rede_eth0_bytes_sent_status": status_rede_enviada,
        "rede_eth0_errin": 0,
        "rede_eth0_errin_status": STATUS_OK,
        "rede_eth0_errout": 0,
        "rede_eth0_errout_status": STATUS_OK,
        "rede_eth0_dropin": 0,
        "rede_eth0_dropin_status": STATUS_OK,
        "rede_eth0_dropout": 0,
        "rede_eth0_dropout_status": STATUS_OK,
        "rede_eth0_isup": 1,
        "rede_eth0_isup_status": STATUS_OK,
        "rede_lo_interval_seconds": intervalo,
        "rede_lo_bytes_recv": rede_loop_recebida,
        "rede_lo_bytes_recv_status": status_loop_recebida,
        "rede_lo_bytes_sent": rede_loop_enviada,
        "rede_lo_bytes_sent_status": status_loop_enviada,
        "rede_lo_errin": 0,
        "rede_lo_errin_status": STATUS_OK,
        "rede_lo_errout": 0,
        "rede_lo_errout_status": STATUS_OK,
        "rede_lo_dropin": 0,
        "rede_lo_dropin_status": STATUS_OK,
        "rede_lo_dropout": 0,
        "rede_lo_dropout_status": STATUS_OK,
        "rede_lo_isup": 1,
        "rede_lo_isup_status": STATUS_OK,
        # ------------------------------------------------------------ Disco
        "disco_volumes_status": STATUS_OK,
        "disco_io_status": STATUS_OK,
        "disco_volume_0_montagem": "/",
        "disco_volume_0_dispositivo": "/dev/sda1",
        "disco_volume_0_tipo_sistema": "ext4",
        "disco_volume_0_coleta_status": STATUS_OK,
        "disco_volume_0_total": disco_total,
        "disco_volume_0_total_status": STATUS_OK,
        "disco_volume_0_percent": disco_percent,
        "disco_volume_0_percent_status": STATUS_OK,
        "disco_volume_0_free": disco_free,
        "disco_volume_0_free_status": STATUS_OK,
        "disco_volume_0_used": disco_used,
        "disco_volume_0_used_status": STATUS_OK,
        "disco_volume_0_inodes_percent": inodes_percent,
        "disco_volume_0_inodes_percent_status": STATUS_OK,
        "disco_volume_0_inodes_total": inodes_total,
        "disco_volume_0_inodes_total_status": STATUS_OK,
        "disco_volume_0_inodes_free": inodes_free,
        "disco_volume_0_inodes_free_status": STATUS_OK,
        "disco_volume_0_inodes_available": inodes_available,
        "disco_volume_0_inodes_available_status": STATUS_OK,
        "disco_io_0_dispositivo": "sda",
        "disco_io_0_coleta_status": STATUS_OK,
        "disco_io_0_interval_seconds": intervalo,
        "disco_io_0_read_time_ms": disco_leitura,
        "disco_io_0_read_time_ms_status": status_disco_leitura,
        "disco_io_0_write_time_ms": disco_escrita,
        "disco_io_0_write_time_ms_status": status_disco_escrita,
        "disco_io_0_queue": disco_fila,
        "disco_io_0_queue_status": status_disco_fila,
    }

    # Uma coluna por núcleo lógico, como o agente grava (cpu_logical_0_percent,
    # cpu_logical_1_percent, ...).
    for indice, valor in enumerate(nucleos):
        coluna = f"cpu_logical_{indice}_percent"
        linha[coluna] = valor
        linha[f"{coluna}_status"] = STATUS_OK

    # Na primeira leitura da noite as métricas de CPU ainda não têm amostra
    # anterior (o psutil só devolve um percentual válido na segunda rodada), então
    # elas ficam sem valor e com o status "aquecendo".
    if primeira_leitura:
        for coluna in ["cpu_percent", "cpu_user", "cpu_system", "cpu_idle"] + [
            f"cpu_logical_{indice}_percent" for indice in range(servidor["nucleos_logicos"])
        ]:
            linha[coluna] = None
            linha[f"{coluna}_status"] = STATUS_AQUECENDO

    return linha


def gerar_linhas_metricas():
    """Gera todas as linhas de métricas dos 10 servidores."""
    linhas = []

    for servidor in SERVIDORES:
        aleatorio = gerador_aleatorio(f"metricas-{servidor['nome']}")
        cpu, ram, disco = curvas_do_servidor(servidor, aleatorio)

        ram_total_gb = round(gib_para_gb(servidor["ram_gib"]), 3)
        # Swap: 8 GB quando a máquina tem 32 GB ou mais de RAM, 4 GB caso
        # contrário (mesma regra que o mock do front usa).
        swap_total_gb = 8.0 if ram_total_gb >= 32 else 4.0

        estado = {
            "cpu": cpu,
            "ram": ram,
            "disco": disco,
            "ram_total_gb": ram_total_gb,
            "swap_total_gb": swap_total_gb,
            "disco_total_gb": float(servidor["disco_gb"]),
            "inodes_total": 6553600 if servidor["disco_gb"] <= 200 else 13107200,
            "freq_min": 800.0,
            "freq_max": 3600.0,
            "boot_id": boot_id(servidor["mac"]),
            "cargas": deque(maxlen=15),
        }

        fim_captura = servidor.get("fim_captura", FIM)

        for indice, horario in enumerate(horarios_da_janela()):
            if horario > fim_captura:
                continue
            linhas.append(
                linha_de_metricas(
                    servidor,
                    horario,
                    indice == 0,
                    aleatorio,
                    estado,
                )
            )

    return linhas


# ===========================================================================
# 11. Geração dos processos
# ===========================================================================


def gerar_linhas_processos():
    """Gera as linhas do CSV de processos.

    O arquivo tem as colunas reais do agente (COLUNAS_PROCESSOS, de
    `processos/processos.py`): TIMESTAMP, ENDERECO_MAC, PID, NOME, USUARIO,
    CPU_PERCENT, MEMORIA_PERCENT, RSS_BYTES, NUM_THREADS, CREATE_TIME e STATUS.
    RSS_BYTES é bytes inteiro e CREATE_TIME é string "%Y-%m-%d %H:%M:%S", como
    o agente grava a partir do psutil.
    """
    threads_por_nome = {
        "java": 48,
        "worker": 24,
        "node": 12,
        "python": 6,
        "nginx": 4,
        "mysqld": 32,
    }

    linhas = []

    for definicao in PROCESSOS_NARRADOS + PROCESSOS_EXTRAPOLADOS:
        servidor = SERVIDORES_POR_NOME[definicao["servidor"]]
        aleatorio = gerador_aleatorio(f"processo-{definicao['servidor']}-{definicao['pid']}")

        cpu = curva(definicao["cpu_ancoras"])
        rss = curva(definicao["rss_ancoras_gb"])

        fim_captura = servidor.get("fim_captura", FIM)
        # O processo já estava de pé antes da janela; o agente converte o epoch
        # do psutil para esse formato de string.
        create_time = (INICIO - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        threads_base = threads_por_nome[definicao["nome"]]

        for horario in horarios_da_janela():
            if horario > fim_captura:
                continue

            # Jitter pequeno: o número do cenário (40% -> 98%) é o oficial, mas
            # um processo real nunca fica parado exatamente no mesmo valor.
            cpu_percent = round(cpu(horario) + aleatorio.uniform(-0.4, 0.4), 2)
            rss_gb = max(0.001, rss(horario) + aleatorio.uniform(-0.02, 0.02))
            rss_bytes = int(rss_gb * (1024 ** 3))
            # O psutil calcula a porcentagem sobre bytes, então o denominador
            # usa a mesma base GiB do RSS do cenário.
            memoria_percent = round(rss_bytes / (servidor["ram_gib"] * (1024 ** 3)) * 100, 2)
            num_threads = max(1, threads_base + aleatorio.randint(-2, 2))

            linhas.append({
                "TIMESTAMP": horario.strftime("%Y-%m-%d %H:%M:%S"),
                "ENDERECO_MAC": servidor["mac"],
                "PID": definicao["pid"],
                "NOME": definicao["nome"],
                "USUARIO": definicao["usuario"],
                "CPU_PERCENT": max(0.0, cpu_percent),
                "MEMORIA_PERCENT": memoria_percent,
                "RSS_BYTES": rss_bytes,
                "NUM_THREADS": num_threads,
                "CREATE_TIME": create_time,
                "STATUS": "running",
            })

    return linhas


# ===========================================================================
# 12. Geração dos containers
# ===========================================================================


def linhas_do_container(container, servidor, estado):
    """Gera as linhas de um container.

    O comportamento do coupon-sync (2 reinicializações e encerramento final)
    vem do cenário mestre, seção 7. Os contadores acumulados de rede e disco
    zeram a cada reinicialização (o container reinicia, o cgroup recomeça) e o
    container que encerrou aparece uma última linha com o estado "exited" e o
    código de saída - depois disso ele para de reportar.
    """
    aleatorio = gerador_aleatorio(f"container-{servidor['mac']}-{container['nome']}")

    mac = servidor["mac"]
    container_id = id_do_container(mac, container["nome"])
    nucleos = servidor["nucleos_logicos"]
    limite_ram = int(container["limite_ram_gib"] * (1024 ** 3))
    tamanho_imagem = int(container["imagem_mb"] * 1024 * 1024)
    camada_gravavel = int(container["camada_gravavel_mb"] * 1024 * 1024)

    reinicios = container.get("reinicios", [])
    encerramento = container.get("encerramento")

    # Começo do container: antes da janela (o container já estava de pé).
    inicio = INICIO - timedelta(hours=8)

    # Contadores acumulados (o Docker conta desde o start da instância).
    cpu_total = 0
    cpu_sistema = 0
    rede_recebida = 0
    rede_enviada = 0
    disco_leitura = 0
    disco_escrita = 0

    # Tempo de CPU do sistema por minuto (8 núcleos x 60 segundos em ns).
    delta_sistema = nucleos * 1_000_000_000 * INTERVALO_SEGUNDOS

    linhas = []
    for horario in horarios_da_janela():
        if encerramento is not None:
            hora_encerramento = datetime(2026, 11, 27, encerramento[0], encerramento[1])
            if horario > hora_encerramento:
                # Container encerrado: ele não reporta mais nada.
                break

        # Reinicializações: a partir do horário do restart, o container roda
        # com o contador de reinícios maior e os contadores acumulados zeram.
        reinicios_ate_agora = [
            reinicio for reinicio in reinicios
            if datetime(2026, 11, 27, reinicio[0], reinicio[1]) <= horario
        ]
        if reinicios_ate_agora:
            ultimo_reinicio = datetime(
                2026, 11, 27, reinicios_ate_agora[-1][0], reinicios_ate_agora[-1][1]
            )
            if estado["momento_reinicio"] != ultimo_reinicio:
                estado["momento_reinicio"] = ultimo_reinicio
                estado["zera_contadores"] = True
        else:
            estado["momento_reinicio"] = None

        if estado["zera_contadores"]:
            cpu_total = 0
            cpu_sistema = 0
            rede_recebida = 0
            rede_enviada = 0
            disco_leitura = 0
            disco_escrita = 0
            estado["zera_contadores"] = False

        # Estado do container neste momento.
        encerrado = encerramento is not None and horario >= datetime(
            2026, 11, 27, encerramento[0], encerramento[1]
        )

        if encerrado:
            # Container parado: o agente de Docker ainda o lista (containers.list
            # traz os parados), mas sem estatísticas de utilização.
            linhas.append({
                "TIMESTAMP": para_timestamp_iso(horario),
                "ENDERECO_MAC": mac,
                "CONTAINER_ID": container_id,
                "CONTAINER": container["nome"],
                "IMAGEM": container["imagem"],
                "STATUS": "exited",
                "HEALTH_STATUS": None,
                "RESTART_COUNT": len(reinicios),
                "STARTED_AT": para_timestamp_iso(estado["momento_reinicio"] or inicio),
                "FINISHED_AT": para_timestamp_iso(datetime(2026, 11, 27, encerramento[0], encerramento[1])),
                "EXIT_CODE": container.get("codigo_saida", 1),
                "OOM_KILLED": False,
                "CPU_PERCENT": None,
                "CPU_TOTAL_USAGE": cpu_total,
                "CPU_SYSTEM_USAGE": cpu_sistema,
                "CPU_ONLINE_CPUS": nucleos,
                "CPU_THROTTLED_TIME": 0.0,
                "CPU_THROTTLED_PERIODS": 0,
                "RAM_PERCENT": None,
                "RAM_USAGE_BYTES": None,
                "RAM_LIMIT_BYTES": limite_ram,
                "REDE_RECEBIDA_BYTES": rede_recebida,
                "REDE_ENVIADA_BYTES": rede_enviada,
                "NET_RX_ERRORS": 0,
                "NET_TX_ERRORS": 0,
                "NET_RX_DROPPED": 0,
                "NET_TX_DROPPED": 0,
                "DISCO_LEITURA_BYTES": disco_leitura,
                "DISCO_ESCRITA_BYTES": disco_escrita,
                "CAMADA_GRAVAVEL_BYTES": camada_gravavel,
                "IMAGEM_SIZE_BYTES": tamanho_imagem,
                "PIDS_CURRENT": 0,
            })
            continue

        # Container em execução.
        cpu_percent = round(
            container["cpu_percent"] * (1 + aleatorio.uniform(-0.03, 0.03)), 2
        )
        ram_gb = max(0.05, container["ram_gib"] + aleatorio.uniform(-0.06, 0.06))
        ram_bytes = int(ram_gb * (1024 ** 3))
        ram_percent = round(ram_bytes / limite_ram * 100, 2)

        cpu_total += int((cpu_percent / 100) / nucleos * delta_sistema)
        cpu_sistema += delta_sistema
        rede_recebida += int(container["rede_mb_s"][0] * 1024 * 1024 * INTERVALO_SEGUNDOS)
        rede_enviada += int(container["rede_mb_s"][1] * 1024 * 1024 * INTERVALO_SEGUNDOS)
        disco_leitura += int(container["disco_mb_s"][0] * 1024 * 1024 * INTERVALO_SEGUNDOS)
        disco_escrita += int(container["disco_mb_s"][1] * 1024 * 1024 * INTERVALO_SEGUNDOS)

        linhas.append({
            "TIMESTAMP": para_timestamp_iso(horario),
            "ENDERECO_MAC": mac,
            "CONTAINER_ID": container_id,
            "CONTAINER": container["nome"],
            "IMAGEM": container["imagem"],
            "STATUS": "running",
            "HEALTH_STATUS": None,
            "RESTART_COUNT": len(reinicios_ate_agora),
            "STARTED_AT": para_timestamp_iso(estado["momento_reinicio"] or inicio),
            "FINISHED_AT": None,
            "EXIT_CODE": 0,
            "OOM_KILLED": False,
            "CPU_PERCENT": cpu_percent,
            "CPU_TOTAL_USAGE": cpu_total,
            "CPU_SYSTEM_USAGE": cpu_sistema,
            "CPU_ONLINE_CPUS": nucleos,
            "CPU_THROTTLED_TIME": 0.0,
            "CPU_THROTTLED_PERIODS": 0,
            "RAM_PERCENT": ram_percent,
            "RAM_USAGE_BYTES": ram_bytes,
            "RAM_LIMIT_BYTES": limite_ram,
            "REDE_RECEBIDA_BYTES": rede_recebida,
            "REDE_ENVIADA_BYTES": rede_enviada,
            "NET_RX_ERRORS": 0,
            "NET_TX_ERRORS": 0,
            "NET_RX_DROPPED": 0,
            "NET_TX_DROPPED": 0,
            "DISCO_LEITURA_BYTES": disco_leitura,
            "DISCO_ESCRITA_BYTES": disco_escrita,
            "CAMADA_GRAVAVEL_BYTES": camada_gravavel,
            "IMAGEM_SIZE_BYTES": tamanho_imagem,
            "PIDS_CURRENT": container["pids"] + aleatorio.randint(-2, 2),
        })

    return linhas


def gerar_linhas_containers():
    """Gera as linhas de containers (só pay-api-04 e cart-api-07)."""
    linhas = []
    for nome_servidor, containers in CONTAINERS.items():
        servidor = SERVIDORES_POR_NOME[nome_servidor]
        for container in containers:
            estado = {"momento_reinicio": None, "zera_contadores": False}
            linhas.extend(linhas_do_container(container, servidor, estado))
    return linhas


# ===========================================================================
# 13. CSV e upload
# ===========================================================================


def montar_csv(linhas, colunas):
    """Monta o conteúdo do CSV na memória (nada é escrito em disco)."""
    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=colunas, extrasaction="raise")
    escritor.writeheader()
    for linha in linhas:
        faltando = [coluna for coluna in colunas if coluna not in linha]
        sobrando = [coluna for coluna in linha if coluna not in colunas]
        if faltando or sobrando:
            raise ValueError(
                f"Linha fora do formato: faltando {faltando}, sobrando {sobrando}"
            )
        escritor.writerow(linha)
    return buffer.getvalue()


def enviar_para_s3(conteudo, nome_arquivo, prefixo, bucket):
    """Sobe o CSV para o S3 de verdade, no prefixo raw/.

    As credenciais vêm do próprio ambiente (a sessão do AWS CLI / AWS Academy
    Lab). O nome do bucket NUNCA é escrito no código: ele vem da variável de
    ambiente BUCKET_DESTINO.
    """
    import boto3

    cliente = boto3.client("s3")
    chave = f"{prefixo}/{nome_arquivo}"

    cliente.put_object(
        Bucket=bucket,
        Key=chave,
        Body=conteudo.encode("utf-8"),
        ContentType="text/csv; charset=utf-8",
    )

    print(f"[S3] Enviado: s3://{bucket}/{chave}")
    return chave


# ===========================================================================
# 14. Conferência do cenário (a rede de segurança do script)
# ===========================================================================


def limites_do_perfil(perfil, metrica):
    """Limites no mesmo formato que a Gold usa (operador '>')."""
    atencao, critico = LIMITES_POR_METRICA[metrica][perfil]
    return {
        "operador": ">",
        "limite_atencao": atencao,
        "limite_critico": critico,
        "operador_2": None,
        "limite_atencao_2": None,
        "limite_critico_2": None,
    }


def serie_por_servidor(linhas, mac, metrica):
    """Monta a série (instante, valor) de UMA métrica, como a Gold monta.

    O nome da métrica é o do catálogo (ex.: "disk_percent"); a coluna do CSV tem
    o prefixo do grupo (ex.: "disco_volume_0_percent"). O mapa abaixo liga os
    dois nomes.
    """
    coluna = COLUNA_CSV_POR_METRICA.get(metrica, metrica)

    serie = []
    for linha in linhas:
        if linha["ENDERECO_MAC"] != mac:
            continue
        valor = linha.get(coluna)
        if valor is None:
            continue
        serie.append((linha["TIMESTAMP"], valor))
    serie.sort(key=lambda par: par[0])
    return serie


def serie_rede(linhas, mac):
    """A métrica net_traffic é a soma do recebido e do enviado de cada
    interface (é assim que o formato_gold.py monta)."""
    serie = []
    for linha in linhas:
        if linha["ENDERECO_MAC"] != mac:
            continue
        total = 0.0
        encontrou = False
        for interface in ["eth0", "lo"]:
            recebida = linha.get(f"rede_{interface}_bytes_recv")
            enviada = linha.get(f"rede_{interface}_bytes_sent")
            if recebida is None or enviada is None:
                continue
            total += recebida + enviada
            encontrou = True
        if encontrou:
            serie.append((linha["TIMESTAMP"], round(total, 2)))
    serie.sort(key=lambda par: par[0])
    return serie


def conferir_cenario(linhas_metricas):
    """Confere, com as MESMAS funções da Gold, o que a tela vai mostrar.

    Para cada servidor:
      * o estado de cada métrica com limite configurado no perfil;
      * desde quando ela está em atenção e desde quando está em crítico
        (o "tempo no estado");
      * a severidade do servidor no dia (o pior estado entre as métricas,
        incluindo a regra do OOM).

    Se alguma métrica de preenchimento cruzar um limite, isso aparece aqui
    como aviso - é a forma de garantir que o dado "normal" não está
    inventando alerta na apresentação.
    """
    print("\n" + "=" * 78)
    print("CONFERÊNCIA DO CENÁRIO (mesmas regras da Gold)")
    print("=" * 78)

    problemas = []

    for servidor in SERVIDORES:
        mac = servidor["mac"]
        perfil = servidor["perfil"]
        linhas_servidor = [linha for linha in linhas_metricas if linha["ENDERECO_MAC"] == mac]

        if not linhas_servidor:
            print(f"\n{servidor['nome']}: sem nenhuma linha gerada.")
            problemas.append(f"{servidor['nome']} sem linhas")
            continue

        tempos = {}

        for metrica in METRICAS_COM_LIMITE:
            if metrica == "net_traffic":
                serie = serie_rede(linhas_metricas, mac)
            else:
                serie = serie_por_servidor(linhas_metricas, mac, metrica)

            if not serie:
                continue

            limites = limites_do_perfil(perfil, metrica)
            tempo = calcular_tempo_no_estado(serie, limites)
            if tempo is None:
                continue

            tempos[metrica] = tempo

        # A severidade é calculada pela MESMA função da Gold (inclusive a regra
        # do OOM), montando um resumo no mesmo formato que ela espera.
        oom = sum(linha["ram_oom_kills"] or 0 for linha in linhas_servidor)
        resumo_para_severidade = {"ram": {"oom": {"total_observado": oom}}}
        for metrica, tempo in tempos.items():
            resumo_para_severidade[metrica] = {
                "metrica": metrica,
                "tipo": "medicao",
                "tempo_no_estado": tempo,
            }
        severidade = calcular_severidade(resumo_para_severidade) or "normal"

        ultima = linhas_servidor[-1]
        print(f"\n{servidor['nome']} ({perfil}) - {len(linhas_servidor)} leituras")
        print(f"  última leitura: {ultima['TIMESTAMP']} "
              f"cpu={ultima['cpu_percent']}% ram={ultima['ram_percent']}% "
              f"disco={ultima['disco_volume_0_percent']}%")

        for metrica, tempo in tempos.items():
            if tempo["estado_atual"] == "normal":
                continue
            atencao_desde = tempo.get("em_atencao_desde")
            critico_desde = tempo.get("em_critico_desde")
            detalhe = f"{metrica}: {tempo['estado_atual']}"
            if atencao_desde:
                detalhe += f", em atenção desde {atencao_desde}"
            if critico_desde:
                detalhe += f", em crítico desde {critico_desde}"
            print(f"  {detalhe}")

        print(f"  severidade do dia: {severidade}")

        # O preenchimento "normal" não pode cruzar nenhum limite.
        for metrica in METRICAS_COM_LIMITE:
            if metrica in ("cpu_percent", "ram_percent"):
                continue  # narrados: cruzam de propósito
            serie = (
                serie_rede(linhas_metricas, mac)
                if metrica == "net_traffic"
                else serie_por_servidor(linhas_metricas, mac, metrica)
            )
            if not serie:
                continue
            limites = limites_do_perfil(perfil, metrica)
            for instante, valor in serie:
                estado = classificar_estado(valor, limites)
                if estado != "normal":
                    problemas.append(
                        f"{servidor['nome']}/{metrica} = {valor} em {instante} "
                        f"({estado}) - preenchimento não deveria cruzar limite"
                    )
                    break

    if problemas:
        print("\nATENÇÃO - problemas encontrados:")
        for problema in problemas:
            print(f"  - {problema}")
    else:
        print("\nNenhum problema: o preenchimento não cruza nenhum limite.")

    return problemas


# ===========================================================================
# 15. Programa principal
# ===========================================================================


def main():
    analisador = argparse.ArgumentParser(
        description="Gera os CSVs do cenário Black Friday e sobe para o S3 (prefixo raw/)."
    )
    analisador.add_argument(
        "--somente-local",
        action="store_true",
        help="gera e salva os CSVs localmente, sem mexer na AWS",
    )
    analisador.add_argument(
        "--pasta",
        default=str(Path(__file__).resolve().parent / "saida-local"),
        help="pasta onde os CSVs são salvos quando --somente-local é usado",
    )
    analisador.add_argument(
        "--amostras",
        type=int,
        default=0,
        help="quantas linhas de cada CSV mostrar no console",
    )
    argumentos = analisador.parse_args()

    print("Gerando o cenário Black Friday (27/11/2026, 18h45 - 20h30)...")

    cabecalho = cabecalho_metricas()
    linhas_metricas = gerar_linhas_metricas()
    linhas_processos = gerar_linhas_processos()
    linhas_containers = gerar_linhas_containers()

    conteudo_metricas = montar_csv(linhas_metricas, cabecalho)
    conteudo_processos = montar_csv(linhas_processos, COLUNAS_PROCESSOS)
    conteudo_containers = montar_csv(linhas_containers, COLUNAS_CONTAINERS)

    print(f"  métricas: {len(linhas_metricas)} linhas x {len(cabecalho)} colunas")
    print(f"  processos: {len(linhas_processos)} linhas x {len(COLUNAS_PROCESSOS)} colunas")
    print(f"  containers: {len(linhas_containers)} linhas x {len(COLUNAS_CONTAINERS)} colunas")

    problemas = conferir_cenario(linhas_metricas)

    carimbo = datetime.now().strftime("%Y%m%dT%H%M%S")
    arquivos = [
        ("metricas", conteudo_metricas, f"metricas_{carimbo}.csv", PREFIXO_S3),
        ("processos", conteudo_processos, f"processos_{carimbo}.csv", PREFIXO_S3),
        ("containers", conteudo_containers, f"containers_{carimbo}.csv", PREFIXO_S3_CONTAINERS),
    ]

    if argumentos.amostras > 0:
        for nome, conteudo, arquivo, _ in arquivos:
            print(f"\n--- amostra de {nome} ({arquivo}) ---")
            for linha in conteudo.splitlines()[: argumentos.amostras + 1]:
                print(linha)

    if argumentos.somente_local:
        pasta = Path(argumentos.pasta)
        pasta.mkdir(parents=True, exist_ok=True)
        for nome, conteudo, arquivo, _ in arquivos:
            destino = pasta / arquivo
            destino.write_text(conteudo, encoding="utf-8")
            print(f"[local] Salvo: {destino}")
        print("\nNão houve upload (--somente-local).")
    else:
        bucket = os.environ.get("BUCKET_DESTINO")
        if not bucket:
            print(
                "ERRO: a variável de ambiente BUCKET_DESTINO não está definida.\n"
                "      Exemplo: BUCKET_DESTINO=coretrace-teste-marcelly-757158332486 \\\n"
                "               python ferramentas/gerador_dados_apresentacao.py\n"
                "      (use --somente-local para gerar os CSVs sem subir para a AWS)"
            )
            return 1

        print(f"\nEnviando para o bucket {bucket}...")
        for nome, conteudo, arquivo, prefixo in arquivos:
            enviar_para_s3(conteudo, arquivo, prefixo, bucket)

    if problemas:
        print("\nATENÇÃO: confira os problemas listados acima antes de apresentar.")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
