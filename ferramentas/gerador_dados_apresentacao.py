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

    # envia dias em sequência, aguardando a esteira (sem gatilhos concorrentes)
    python ferramentas/gerador_dados_apresentacao.py --dias-por-lote 1 --lambda-nome NOME

    # retoma mantendo a mesma janela e tamanho de lote
    python ferramentas/gerador_dados_apresentacao.py --lambda-nome NOME --retomar-de 2026-11-10

    # gera, confere e mostra algumas linhas no console
    python ferramentas/gerador_dados_apresentacao.py --somente-local --amostras 3
"""

import argparse
import csv
import hashlib
import io
import math
import json
import time
import os
import random
import sys
from collections import deque
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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

# Idem para logs e eventos de Docker (capturas/containers/logs.py e eventos.py).
# O agente real (capturas/captura_docker.py) escreve os três CSVs de Docker com
# ';' como delimitador - diferente dos CSVs de métricas/processos/containers
# deste gerador, que usam ',' (a Silver detecta o delimitador de cada arquivo
# automaticamente, csv.Sniffer via pandas `sep=None`, então os dois convivem).
from capturas.containers.logs import COLUNAS_LOGS
from capturas.containers.eventos import COLUNAS_EVENTOS, descrever_evento

# Para a conferência final usamos as MESMAS funções que a Gold usa
# (resumos_gold.py). Assim o script não reimplementa a regra de classificação:
# se a regra mudar lá, a conferência daqui continua dizendo a verdade.
from resumos_gold import calcular_severidade, calcular_tempo_no_estado, classificar_estado


# ===========================================================================
# 1. Identificação do cenário (Contexto/cenario-mestre-black-friday.md)
# ===========================================================================

DIA = "2026-11-27"

# Janela narrada: uma leitura por minuto, das 18h45 às 20h30 de 27/11 (fim
# incluso). As âncoras do incidente (seções 4 e seguintes) são relativas a
# este intervalo e não mudam - é a "foto" oficial do cenário mestre.
INICIO = datetime(2026, 11, 27, 18, 45)
FIM = datetime(2026, 11, 27, 20, 30)
INTERVALO_SEGUNDOS = 60

# Histórico: quantos dias ANTES de FIM o gerador cobre (seção 3 do cenário
# Docker V2 - "Janela de 30 dias corridos: 28/10/2026 20h30 a 27/11/2026
# 20h30"). INICIO_HISTORICO é reatribuída em main() a partir de
# --dias-historico; o valor aqui é só o padrão usado quando o módulo é
# importado fora do CLI (por exemplo em teste manual no console).
DIAS_HISTORICO_PADRAO = 30
INICIO_HISTORICO = FIM - timedelta(days=DIAS_HISTORICO_PADRAO)

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
PREFIXO_S3_EVENTOS = "raw/docker/eventos"
PREFIXO_S3_LOGS = "raw/docker/logs"

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
# 4.1. Histórico anterior ao incidente (seção 3 e 4 do cenário Docker V2)
# ===========================================================================
#
# A janela narrada (18h45-20h30 de 27/11) continua usando `curva()`/`estavel()"
# exatamente como antes - isso não muda. O que muda é o que acontece ANTES
# dela: em vez de não existir nenhuma amostra, agora há até 30 dias de
# histórico com perfil diurno (pico de almoço e de jantar), uma variação
# pequena e determinística entre dias, e um punhado de episódios pontuais com
# data completa (seção 4 do documento). Esses episódios são PROPOSTAS de
# calibração ("a duração, horários e valores podem ser calibrados para os
# agregados reais, preservando as descobertas pretendidas" - seção 4, rodapé)
# e não números oficiais do cenário mestre, que só descreve a janela
# 18h45-20h30 e os totais da seção 3.

# Pico de almoço (meio-dia) e de jantar (19h30) - mesma forma pra CPU e RAM
# de todo mundo; a amplitude de cada métrica/servidor é decidida por quem usa
# esse multiplicador, não aqui.
PICOS_DIURNOS = [
    (12.0, 0.22, 1.4),
    (19.5, 0.30, 1.6),
]


def fator_diurno(timestamp):
    """Multiplicador em torno de 1.0 que sobe no almoço/jantar e cai de
    madrugada (0h-6h). É a FORMA da curva diária; a amplitude real de cada
    métrica é aplicada por quem chama (ver `amortecer_fator`)."""
    hora_fracionaria = timestamp.hour + timestamp.minute / 60 + timestamp.second / 3600
    fator = 1.0
    for centro, amplitude, largura in PICOS_DIURNOS:
        distancia = min(abs(hora_fracionaria - centro), 24 - abs(hora_fracionaria - centro))
        fator += amplitude * math.exp(-(distancia ** 2) / (2 * largura ** 2))
    if hora_fracionaria < 6:
        fator *= 0.6 + 0.4 * (hora_fracionaria / 6)
    return fator


def amortecer_fator(fator, intensidade):
    """Aproxima `fator` de 1.0 - usado pra containers/processos, que variam
    menos ao longo do dia do que o host inteiro. intensidade=1 mantém o fator
    cheio; intensidade=0 deixaria tudo flat (sem variação diária)."""
    return 1.0 + intensidade * (fator - 1.0)


def curva_absoluta(ancoras_datetime):
    """Como `curva()`, mas as âncoras são datetimes completos (ano/mês/dia/
    hora/min), não só hora:minuto. Fora do intervalo [primeira, última]
    âncora devolve None - quem chama sabe então que não há episódio ativo
    nesse instante e deve usar o histórico comum (perfil diurno)."""
    ancoras = sorted(ancoras_datetime, key=lambda item: item[0])

    def calcular(timestamp):
        if timestamp < ancoras[0][0] or timestamp > ancoras[-1][0]:
            return None
        for (inicio_a, valor_a), (inicio_b, valor_b) in zip(ancoras, ancoras[1:]):
            if inicio_a <= timestamp <= inicio_b:
                total_segundos = (inicio_b - inicio_a).total_seconds()
                if total_segundos <= 0:
                    return valor_b
                fracao = (timestamp - inicio_a).total_seconds() / total_segundos
                return valor_a + (valor_b - valor_a) * fracao
        return ancoras[-1][1]

    return calcular


def valor_historico(base, timestamp, aleatorio, amplitude_jitter, intensidade_diurna=1.0):
    """Preenchimento do histórico fora da janela narrada e fora de qualquer
    episódio pontual: a base (mesmo "estável" que o cenário já usava) com
    perfil diurno e um jitter pequeno e determinístico (a semente do próprio
    `aleatorio` do servidor/processo/container)."""
    fator = amortecer_fator(fator_diurno(timestamp), intensidade_diurna)
    return base * fator + aleatorio.uniform(-amplitude_jitter, amplitude_jitter)


# Episódios históricos do HOST (CPU/RAM), seção 4 do cenário Docker V2.
# Cada entrada: (datetime_inicio, datetime_fim, valor_de_pico). O valor nas
# pontas é sempre a base histórica do servidor (ver `curva_host_com_historico`),
# então o episódio sobe da base até `valor_de_pico` e desce de volta - não
# precisa repetir a base aqui.
EPISODIOS_HOST_CPU = {
    "cart-api-07": [
        # 12/11: pico curto de CPU e tráfego, recuperação espontânea -
        # contraste deliberado com o incidente prolongado de 27/11.
        (datetime(2026, 11, 12, 18, 55), datetime(2026, 11, 12, 19, 25), 68),
        # 20-23/11: maior frequência de picos de CPU (throttling leve é
        # modelado na camada de containers, não aqui no host).
        (datetime(2026, 11, 20, 11, 30), datetime(2026, 11, 20, 13, 0), 58),
        (datetime(2026, 11, 21, 19, 0), datetime(2026, 11, 21, 20, 0), 60),
        (datetime(2026, 11, 22, 12, 30), datetime(2026, 11, 22, 13, 30), 57),
        (datetime(2026, 11, 23, 19, 15), datetime(2026, 11, 23, 20, 15), 61),
        # 25/11: CPU alta por mais tempo.
        (datetime(2026, 11, 25, 19, 10), datetime(2026, 11, 25, 19, 55), 72),
        # 26/11: mais concorrência, normalização parcial (não volta à base).
        (datetime(2026, 11, 26, 18, 50), datetime(2026, 11, 26, 19, 45), 70),
        (datetime(2026, 11, 26, 19, 45), datetime(2026, 11, 26, 21, 0), 56),
    ],
}

EPISODIOS_HOST_RAM = {
    "pay-api-04": [
        # 25/11: crescimento de RAM e fila, redução parcial após o pico.
        (datetime(2026, 11, 25, 19, 10), datetime(2026, 11, 25, 19, 55), 76),
        (datetime(2026, 11, 25, 19, 55), datetime(2026, 11, 25, 21, 0), 68),
        # 26/11: RAM permanece mais elevada após o jantar (sem OOM).
        (datetime(2026, 11, 26, 18, 50), datetime(2026, 11, 26, 19, 45), 74),
        (datetime(2026, 11, 26, 19, 45), datetime(2026, 11, 26, 23, 0), 70),
    ],
}


def curva_host_com_historico(servidor, metrica, ancoras_narradas, base_historica,
                              episodios, aleatorio, amplitude_jitter):
    """Monta a função de valor(timestamp) de uma métrica de HOST (CPU ou RAM)
    cobrindo tanto a janela narrada quanto o histórico de até 30 dias.

    Dentro de [INICIO, FIM]: usa EXATAMENTE `curva(ancoras_narradas)` quando
    o servidor tem âncoras narradas (cart-api-07, cart-api-12, pay-api-04,
    pay-db-02) - sem nenhuma alteração, é a curva oficial do cenário mestre.
    Os servidores "normais" (sem âncora, só `cpu_estavel`/`ram_estavel`)
    continuam EXATAMENTE como antes dentro dessa janela também: valor estável
    com jitter, sem perfil diurno - essa preservação é literal (seção 2 do
    prompt: "não modifique as curvas originais de hosts... entre 18h45 e
    20h30"), o perfil diurno deles só entra no histórico, fora da janela.
    Fora da janela narrada: usa o episódio pontual ativo (se houver,
    interpolando base -> pico -> base com `curva_absoluta`) ou, na ausência
    de episódio, o perfil diurno em torno de `base_historica` (seção 4:
    "picos habituais de almoço e jantar, sem instabilidade persistente").
    """
    curva_narrada = curva(ancoras_narradas) if ancoras_narradas else None
    episodios_do_servidor = episodios.get(servidor["nome"], [])

    def calcular(timestamp):
        if curva_narrada is not None and INICIO <= timestamp <= FIM:
            return curva_narrada(timestamp)

        if curva_narrada is None and INICIO <= timestamp <= FIM:
            return base_historica + aleatorio.uniform(-amplitude_jitter, amplitude_jitter)

        for inicio_ep, fim_ep, pico in episodios_do_servidor:
            if inicio_ep <= timestamp <= fim_ep:
                meio = inicio_ep + (fim_ep - inicio_ep) / 2
                subindo = curva_absoluta([(inicio_ep, base_historica), (meio, pico)])
                descendo = curva_absoluta([(meio, pico), (fim_ep, base_historica)])
                valor_episodio = subindo(timestamp)
                if valor_episodio is None:
                    valor_episodio = descendo(timestamp)
                return valor_episodio + aleatorio.uniform(-amplitude_jitter, amplitude_jitter)

        return valor_historico(base_historica, timestamp, aleatorio, amplitude_jitter)

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

# Episódios de container são (datetime_inicio, datetime_fim, fracao_de_pico)
# - mesma forma dos episódios de host (seção 4.1), só que em FRAÇÃO do valor
# final do cenário mestre (1.0 = valor oficial da tabela acima), não em
# unidade absoluta. "fracao_historica" é o nível de repouso fora de qualquer
# janela especial; dentro da janela narrada (18h45-20h30) o container sobe de
# fracao_historica até 1.0 acompanhando o PROGRESSO da curva do host (seção
# B: "garanta coerência entre containers e hosts em todos os instantes") -
# os números oficiais (cpu_percent/ram_gib da tabela) continuam batendo
# exatamente em FIM (20h30), só a trajetória até lá deixou de ser uma reta
# constante. Isso não é narrado pelo cenário mestre (que só fala do estado em
# 20h30); é calibração deste gerador, documentada aqui.
EPISODIOS_CART_API_JAVA = [
    (datetime(2026, 11, 12, 18, 55), datetime(2026, 11, 12, 19, 25), 0.55),
    (datetime(2026, 11, 20, 11, 30), datetime(2026, 11, 20, 13, 0), 0.45),
    (datetime(2026, 11, 21, 19, 0), datetime(2026, 11, 21, 20, 0), 0.47),
    (datetime(2026, 11, 22, 12, 30), datetime(2026, 11, 22, 13, 30), 0.44),
    (datetime(2026, 11, 23, 19, 15), datetime(2026, 11, 23, 20, 15), 0.48),
    (datetime(2026, 11, 25, 19, 10), datetime(2026, 11, 25, 19, 55), 0.68),
    (datetime(2026, 11, 26, 18, 50), datetime(2026, 11, 26, 19, 45), 0.70),
    (datetime(2026, 11, 26, 19, 45), datetime(2026, 11, 26, 21, 0), 0.52),
]

# Mesmas janelas acima (menos o pico isolado de 12/11, que "recupera
# espontaneamente" e não tem throttling) servem pra ligar o throttling do
# cart-api-java; a janela final é a própria tarde da Black Friday, a partir
# de quando o host cruza a atenção (19h25, cenário mestre seção 4).
JANELAS_THROTTLING_CART_API_JAVA = [
    (datetime(2026, 11, 20, 11, 30), datetime(2026, 11, 20, 13, 0)),
    (datetime(2026, 11, 21, 19, 0), datetime(2026, 11, 21, 20, 0)),
    (datetime(2026, 11, 22, 12, 30), datetime(2026, 11, 22, 13, 30)),
    (datetime(2026, 11, 23, 19, 15), datetime(2026, 11, 23, 20, 15)),
    (datetime(2026, 11, 25, 19, 10), datetime(2026, 11, 25, 19, 55)),
    (datetime(2026, 11, 26, 18, 50), datetime(2026, 11, 26, 21, 0)),
    (datetime(2026, 11, 27, 19, 25), FIM),
]

EPISODIOS_PAY_API_JAVA_RAM = [
    (datetime(2026, 11, 25, 19, 10), datetime(2026, 11, 25, 19, 55), 0.78),
    (datetime(2026, 11, 25, 19, 55), datetime(2026, 11, 25, 21, 0), 0.64),
    (datetime(2026, 11, 26, 18, 50), datetime(2026, 11, 26, 19, 45), 0.74),
    (datetime(2026, 11, 26, 19, 45), datetime(2026, 11, 26, 23, 0), 0.68),
]

EPISODIOS_PAY_RETRY_WORKER_RAM = [
    (datetime(2026, 11, 25, 19, 10), datetime(2026, 11, 25, 19, 55), 0.70),
    (datetime(2026, 11, 26, 18, 50), datetime(2026, 11, 26, 19, 45), 0.68),
]

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
            # CPU acompanha a pressão geral do host (ancora_cpu_host);
            # RAM é o protagonista da pressão de memória do Financeiro,
            # com episódios próprios.
            "fracao_historica_cpu": 0.35,
            "ancora_cpu_host": "cpu_ancoras",
            "fracao_historica_ram": 0.45,
            "ancora_ram_host": "ram_ancoras",
            "episodios_ram": EPISODIOS_PAY_API_JAVA_RAM,
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
            # Tráfego externo: participa um pouco da pressão do host, sem
            # ficar perto do crítico - segue "saudável operacionalmente".
            "fracao_historica_cpu": 0.75,
            "ancora_cpu_host": "cpu_ancoras",
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
            # "Crescimento de filas e tarefas" acompanha a pressão de RAM do
            # host (é o sinal de backlog), com um eco mais discreto dos
            # mesmos episódios do pay-api-java.
            "fracao_historica_cpu": 0.55,
            "ancora_cpu_host": "ram_ancoras",
            "episodios_cpu": EPISODIOS_PAY_RETRY_WORKER_RAM,
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
            # Protagonista de CPU e throttling (seção B do cenário V2):
            # historicamente baixo, com episódios pontuais e throttling
            # correlacionado só nessas janelas (nunca a partir da CPU
            # instantânea - seção 7 do cenário Docker V2).
            "fracao_historica_cpu": 0.30,
            "ancora_cpu_host": "cpu_ancoras",
            "episodios_cpu": EPISODIOS_CART_API_JAVA,
            "janelas_throttling": JANELAS_THROTTLING_CART_API_JAVA,
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
            # "Demanda moderada, permanece ativo": participa pouco da
            # pressão do host, sem episódios próprios.
            "fracao_historica_cpu": 0.65,
            "ancora_cpu_host": "cpu_ancoras",
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
            # "Referência relativamente estável": variação mínima o tempo
            # todo, sem episódios.
            "fracao_historica_cpu": 0.92,
            "ancora_cpu_host": "cpu_ancoras",
        },
        {
            # Container que falha: 2 reinicializações (20h11 e 20h16) e
            # encerramento final às 20h18 com código de saída 1 - as DUAS
            # únicas reinicializações acumuladas do servidor (cenário
            # mestre, seção 7). O episódio de 24/11 (seção 4 do cenário
            # Docker V2) é tratado como falha transitória RECUPERÁVEL, sem
            # reinício de container: fica só nos logs/eventos (ver
            # `gerar_linhas_eventos`/`gerar_linhas_logs`), para não alterar
            # esse total oficial.
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
            "reinicios": [datetime(2026, 11, 27, 20, 11), datetime(2026, 11, 27, 20, 16)],
            "encerramento": datetime(2026, 11, 27, 20, 18),
            "codigo_saida": 1,
            "episodio_recuperavel": (datetime(2026, 11, 24, 19, 12), datetime(2026, 11, 24, 19, 18)),
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
    """Uma leitura a cada INTERVALO_SEGUNDOS, de INICIO_HISTORICO até FIM
    (fim incluso). Com os 30 dias padrão e amostra de 60s isso dá 43.201
    instantes (30*1440 minutos + 1, janela inclusiva nas duas pontas - seção
    3 do cenário Docker V2). Com --dias-historico 0, INICIO_HISTORICO ==
    INICIO e o resultado é idêntico ao do gerador antigo (só a janela
    narrada, 18h45-20h30 de 27/11)."""
    horarios = []
    atual = INICIO_HISTORICO
    passo = timedelta(seconds=INTERVALO_SEGUNDOS)
    while atual <= FIM:
        horarios.append(atual)
        atual += passo
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
    """Junta a curva de cada métrica do servidor: dentro da janela narrada é
    exatamente a curva/valor estável de antes; fora dela (histórico de até
    30 dias) é o perfil diurno + episódios pontuais calibrados (seção 4.1).
    """
    if "cpu_ancoras" in servidor:
        base_cpu = servidor["cpu_ancoras"][0][2]
        cpu = curva_host_com_historico(
            servidor, "cpu", servidor["cpu_ancoras"], base_cpu,
            EPISODIOS_HOST_CPU, aleatorio, amplitude_jitter=1.5,
        )
    else:
        base, amplitude = servidor["cpu_estavel"]
        cpu = curva_host_com_historico(
            servidor, "cpu", None, base, EPISODIOS_HOST_CPU, aleatorio,
            amplitude_jitter=amplitude,
        )

    if "ram_ancoras" in servidor:
        base_ram = servidor["ram_ancoras"][0][2]
        ram = curva_host_com_historico(
            servidor, "ram", servidor["ram_ancoras"], base_ram,
            EPISODIOS_HOST_RAM, aleatorio, amplitude_jitter=1.0,
        )
    else:
        base, amplitude = servidor["ram_estavel"]
        ram = curva_host_com_historico(
            servidor, "ram", None, base, EPISODIOS_HOST_RAM, aleatorio,
            amplitude_jitter=amplitude,
        )

    # Disco não tem episódio de host dedicado no cenário Docker V2 (os picos
    # de disco do incidente ficam na camada de containers - seção 7). Aqui
    # só suaviza a variação diária em torno da curva/valor já existente.
    if "disco_ancoras" in servidor:
        curva_disco_narrada = curva(servidor["disco_ancoras"])

        def disco(timestamp):
            return curva_disco_narrada(timestamp)
    else:
        base, amplitude = servidor["disco_estavel"]

        def disco(timestamp, _base=base, _amplitude=amplitude):
            if INICIO <= timestamp <= FIM:
                return _base + aleatorio.uniform(-_amplitude, _amplitude)
            return valor_historico(_base, timestamp, aleatorio, _amplitude,
                                    intensidade_diurna=0.3)

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
        # O processo já estava de pé antes do início do histórico; o agente
        # converte o epoch do psutil para esse formato de string.
        create_time = (INICIO_HISTORICO - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        threads_base = threads_por_nome[definicao["nome"]]

        for horario in horarios_da_janela():
            if horario > fim_captura:
                continue

            # Fora da janela narrada, as âncoras de CPU/RSS ficam "grudadas"
            # no primeiro valor (é o que `interpolar` já fazia antes de
            # existir histórico). Aplicamos aqui o mesmo perfil diurno do
            # host, bem amortecido: a CPU do processo acompanha um pouco o
            # movimento de almoço/jantar; a RSS fica só com o jitter (memória
            # de um processo estável não "respira" com o tráfego do jeito
            # que a CPU respira).
            if INICIO <= horario <= FIM:
                cpu_base = cpu(horario)
            else:
                cpu_base = cpu(horario) * amortecer_fator(fator_diurno(horario), 0.3)

            # Jitter pequeno: o número do cenário (40% -> 98%) é o oficial, mas
            # um processo real nunca fica parado exatamente no mesmo valor.
            cpu_percent = round(cpu_base + aleatorio.uniform(-0.4, 0.4), 2)
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


def progresso_host(ancoras, timestamp):
    """0.0 no primeiro valor da curva do host, 1.0 no último - usado pra
    mapear "onde o host está no incidente" num container que o acompanha."""
    valor = curva(ancoras)(timestamp)
    base, topo = ancoras[0][2], ancoras[-1][2]
    if topo == base:
        return 0.0
    return max(0.0, min(1.0, (valor - base) / (topo - base)))


def fator_atividade_container(servidor, chave_ancora_host, fracao_historica, episodios, timestamp):
    """Fração do valor FINAL do container (cenário mestre, seção 7; 1.0 =
    valor oficial em 20h30) que vale neste instante.

    - Dentro da janela narrada, se o container acompanha uma âncora do host
      (`chave_ancora_host`): sobe de `fracao_historica` até 1.0 seguindo o
      PROGRESSO da curva do host (seção B do cenário Docker V2: "garanta
      coerência entre containers e hosts em todos os instantes"). Em 20h30
      o progresso é sempre 1.0, então o valor oficial nunca muda.
    - Dentro de um episódio histórico pontual (seção 4.1): sobe/desce entre
      `fracao_historica` e a fração de pico do episódio.
    - Fora dos dois casos: `fracao_historica`, com um perfil diurno leve
      quando o container de fato acompanha alguma âncora do host (senão
      fica estável - é o caso do coupon-sync, que não tem história de
      pressão, só de falha).
    """
    ancoras_host = servidor.get(chave_ancora_host) if chave_ancora_host else None

    if ancoras_host and INICIO <= timestamp <= FIM:
        progresso = progresso_host(ancoras_host, timestamp)
        return fracao_historica + progresso * (1 - fracao_historica)

    for inicio_ep, fim_ep, fracao_pico in episodios:
        if inicio_ep <= timestamp <= fim_ep:
            meio = inicio_ep + (fim_ep - inicio_ep) / 2
            subindo = curva_absoluta([(inicio_ep, fracao_historica), (meio, fracao_pico)])
            descendo = curva_absoluta([(meio, fracao_pico), (fim_ep, fracao_historica)])
            valor = subindo(timestamp)
            return valor if valor is not None else descendo(timestamp)

    if not ancoras_host:
        return fracao_historica

    return fracao_historica * amortecer_fator(fator_diurno(timestamp), 0.5)


def throttling_ativo(janelas, timestamp):
    return any(inicio <= timestamp <= fim for inicio, fim in janelas)


def linhas_do_container(container, servidor, estado):
    """Gera as linhas de um container ao longo de toda a janela (histórico
    de até 30 dias + a janela narrada).

    O comportamento do coupon-sync (2 reinicializações e encerramento final
    em 27/11, seção 7 do cenário mestre) continua vindo só desses dois
    horários - o episódio recuperável de 24/11 (seção 4 do cenário Docker
    V2) não entra aqui, só em `gerar_linhas_eventos`/`gerar_linhas_logs`, pra
    não alterar o total de 2 reinicializações acumuladas. Os contadores
    acumulados de rede/disco/throttling zeram a cada reinicialização (o
    cgroup recomeça) e o container que encerrou aparece uma última linha com
    o estado "exited" e o código de saída - depois disso ele para de
    reportar.
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

    chave_ancora_cpu = container.get("ancora_cpu_host")
    fracao_historica_cpu = container.get("fracao_historica_cpu", 1.0)
    episodios_cpu = container.get("episodios_cpu", [])

    chave_ancora_ram = container.get("ancora_ram_host")
    fracao_historica_ram = container.get("fracao_historica_ram")
    episodios_ram = container.get("episodios_ram", [])

    janelas_throttling = container.get("janelas_throttling", [])

    # Começo do container: antes do início do histórico (ele já estava de
    # pé havia tempo).
    inicio = INICIO_HISTORICO - timedelta(hours=8)

    # Contadores acumulados (o Docker conta desde o start da instância).
    cpu_total = 0
    cpu_sistema = 0
    rede_recebida = 0
    rede_enviada = 0
    disco_leitura = 0
    disco_escrita = 0
    cpu_throttled_periods = 0
    cpu_throttled_time = 0.0

    # Tempo de CPU do sistema por minuto (núcleos x INTERVALO_SEGUNDOS em ns).
    delta_sistema = nucleos * 1_000_000_000 * INTERVALO_SEGUNDOS

    linhas = []
    for horario in horarios_da_janela():
        if encerramento is not None and horario > encerramento:
            # Container encerrado: ele não reporta mais nada.
            break

        # Reinicializações: a partir do horário do restart, o container roda
        # com o contador de reinícios maior e os contadores acumulados zeram.
        reinicios_ate_agora = [reinicio for reinicio in reinicios if reinicio <= horario]
        if reinicios_ate_agora:
            ultimo_reinicio = reinicios_ate_agora[-1]
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
            cpu_throttled_periods = 0
            cpu_throttled_time = 0.0
            estado["zera_contadores"] = False

        # Estado do container neste momento.
        encerrado = encerramento is not None and horario >= encerramento

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
                "FINISHED_AT": para_timestamp_iso(encerramento),
                "EXIT_CODE": container.get("codigo_saida", 1),
                "OOM_KILLED": False,
                "CPU_PERCENT": None,
                "CPU_TOTAL_USAGE": cpu_total,
                "CPU_SYSTEM_USAGE": cpu_sistema,
                "CPU_ONLINE_CPUS": nucleos,
                "CPU_THROTTLED_TIME": round(cpu_throttled_time, 6),
                "CPU_THROTTLED_PERIODS": cpu_throttled_periods,
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

        # Container em execução: a fração de atividade escala CPU, PIDs,
        # rede e disco juntos (curvas correlacionadas, mas com jitter
        # independente - seção B: "distintas, mas correlacionadas"). RAM usa
        # sua própria fração quando o container tem episódios de memória
        # próprios (pay-api-java); os demais seguem a mesma fração da CPU,
        # bem amortecida (memória reage mais devagar que CPU).
        fator_cpu = fator_atividade_container(
            servidor, chave_ancora_cpu, fracao_historica_cpu, episodios_cpu, horario
        )
        if chave_ancora_ram or fracao_historica_ram is not None:
            fator_ram = fator_atividade_container(
                servidor, chave_ancora_ram, fracao_historica_ram, episodios_ram, horario
            )
        else:
            fator_ram = amortecer_fator(fator_cpu, 0.25)

        cpu_percent = round(
            container["cpu_percent"] * fator_cpu * (1 + aleatorio.uniform(-0.03, 0.03)), 2
        )
        ram_gb = max(0.05, container["ram_gib"] * fator_ram + aleatorio.uniform(-0.06, 0.06))
        ram_bytes = int(ram_gb * (1024 ** 3))
        ram_percent = round(ram_bytes / limite_ram * 100, 2)

        cpu_total += int(max(0.0, cpu_percent) / 100 / nucleos * delta_sistema)
        cpu_sistema += delta_sistema
        rede_recebida += int(container["rede_mb_s"][0] * fator_cpu * 1024 * 1024 * INTERVALO_SEGUNDOS)
        rede_enviada += int(container["rede_mb_s"][1] * fator_cpu * 1024 * 1024 * INTERVALO_SEGUNDOS)
        disco_leitura += int(container["disco_mb_s"][0] * fator_cpu * 1024 * 1024 * INTERVALO_SEGUNDOS)
        disco_escrita += int(container["disco_mb_s"][1] * fator_cpu * 1024 * 1024 * INTERVALO_SEGUNDOS)

        # Throttling: só nas janelas calibradas (seção 7 do cenário Docker
        # V2 - nunca derivado da CPU instantânea). Fora delas, zero
        # legítimo. Quota simulada: ~7 dos 8 núcleos lógicos por período de
        # 100ms: quando o container está numa janela de throttling, alguns
        # períodos daquele minuto estouram a quota e ficam limitados.
        if janelas_throttling and throttling_ativo(janelas_throttling, horario):
            periodos_novos = aleatorio.randint(1, 4)
            cpu_throttled_periods += periodos_novos
            cpu_throttled_time += periodos_novos * aleatorio.uniform(0.02, 0.08)

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
            "CPU_THROTTLED_TIME": round(cpu_throttled_time, 6),
            "CPU_THROTTLED_PERIODS": cpu_throttled_periods,
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
            "PIDS_CURRENT": max(1, round(container["pids"] * fator_cpu)) + aleatorio.randint(-2, 2),
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
# 12.1. Eventos e logs do Docker (seção C do cenário Docker V2)
# ===========================================================================
#
# Eventos e logs compartilham a MESMA cronologia dos containers: os únicos
# die/start que existem são os das duas reinicializações + o encerramento
# final do coupon-sync em 27/11 (seção 7 do cenário mestre, já em
# CONTAINERS). Nenhum outro container reinicia ou cai. Um "restart" visto de
# fora é sempre um par die -> start - nunca geramos as duas ações pro mesmo
# evento nem contamos a mesma tentativa duas vezes (seção C do prompt).
#
# (container, timestamp, acao, codigo_saida) - codigo_saida é None pra start.
EVENTOS_NARRADOS = [
    ("coupon-sync", datetime(2026, 11, 27, 20, 11, 1), "die", 1),
    ("coupon-sync", datetime(2026, 11, 27, 20, 11, 4), "start", None),
    ("coupon-sync", datetime(2026, 11, 27, 20, 16, 10), "die", 1),
    ("coupon-sync", datetime(2026, 11, 27, 20, 16, 14), "start", None),
    ("coupon-sync", datetime(2026, 11, 27, 20, 18, 0), "die", 1),
]

# (container, timestamp, nivel, mensagem) - mensagens da seção 8 do cenário
# Docker V2, adaptadas ao formato do coletor real (capturas/containers/
# logs.py: LOG_LEVEL + LOG_MENSAGEM). Os horários respeitam a ordem causal
# exigida pela seção C: o ERROR de falha vem antes do die, o INFO de retomada
# vem depois do start.
LOGS_NARRADOS = [
    ("cart-api-java", datetime(2026, 11, 27, 19, 46, 18), "WARN",
     "Tempo de processamento acima do esperado"),
    ("cart-api-java", datetime(2026, 11, 27, 20, 5, 12), "WARN",
     "Fila interna de tarefas em crescimento"),
    ("cart-api-java", datetime(2026, 11, 27, 20, 9, 31), "ERROR",
     "Timeout ao consultar serviço de cupons"),
    ("cart-api-java", datetime(2026, 11, 27, 20, 22, 8), "WARN",
     "Fila interna de tarefas acima do padrão"),
    ("coupon-sync", datetime(2026, 11, 27, 20, 11, 0), "ERROR",
     "Falha na sincronização de cupons"),
    ("coupon-sync", datetime(2026, 11, 27, 20, 11, 5), "INFO",
     "Serviço iniciado novamente"),
    ("coupon-sync", datetime(2026, 11, 27, 20, 16, 9), "ERROR",
     "Nova falha na sincronização"),
    ("coupon-sync", datetime(2026, 11, 27, 20, 16, 15), "INFO",
     "Serviço iniciado novamente"),
    ("coupon-sync", datetime(2026, 11, 27, 20, 17, 58), "ERROR",
     "Encerramento da aplicação"),
    ("pay-api-java", datetime(2026, 11, 27, 19, 28, 32), "WARN",
     "Fila de processamento acima do padrão"),
    ("pay-api-java", datetime(2026, 11, 27, 19, 46, 15), "WARN",
     "Pressão de memória durante processamento"),
    ("pay-retry-worker", datetime(2026, 11, 27, 20, 4, 27), "WARN",
     "Tempo de processamento acima do esperado"),
    # 24/11: episódio recuperável do coupon-sync, SEM reinício de container
    # (seção 2 do prompt - "alternativa conservadora: evento histórico de
    # instabilidade recuperável sem reinício" - preserva o acumulado oficial
    # de 2 reinicializações na Black Friday). Por isso não há entrada
    # correspondente em EVENTOS_NARRADOS: nada de "die"/"start" aconteceu.
    ("coupon-sync", datetime(2026, 11, 24, 19, 12, 0), "WARN",
     "Latência elevada na sincronização de cupons"),
    ("coupon-sync", datetime(2026, 11, 24, 19, 15, 30), "ERROR",
     "Falha transitória na sincronização de cupons"),
    ("coupon-sync", datetime(2026, 11, 24, 19, 17, 45), "INFO",
     "Sincronização recuperada automaticamente"),
]

# Um WARN perto do pico de cada episódio histórico calibrado (seção 11/12) -
# mesma regra da seção 8 ("WARN em pressão/filas"), só que fora da noite da
# Black Friday. O pico de 12/11 (recuperação espontânea, sem fila) não entra
# aqui de propósito - é o contraste "sem instabilidade persistente" do
# cenário.
LOGS_EPISODIOS = [
    # O índice 0 (pico de 12/11) fica de fora: "recuperação espontânea", sem
    # fila - é o contraste proposital com os episódios seguintes.
    ("cart-api-java", EPISODIOS_CART_API_JAVA[1:], "Fila interna de tarefas em crescimento"),
    ("pay-api-java", EPISODIOS_PAY_API_JAVA_RAM, "Pressão de memória durante processamento"),
    ("pay-retry-worker", EPISODIOS_PAY_RETRY_WORKER_RAM, "Fila de processamento acima do padrão"),
]

# INFO de rotina - esparso (um punhado de linhas por dia, não uma por
# minuto - seção 8) para todos os sete containers, enquanto estiverem ativos.
MENSAGENS_ROTINA = {
    "pay-api-java": "Processamento de pagamentos concluído",
    "pay-gateway-node": "Sincronização com gateway externo concluída",
    "pay-retry-worker": "Fila de tentativas processada",
    "cart-api-java": "Requisições de carrinho processadas",
    "cart-worker-node": "Tarefa auxiliar concluída",
    "redis-cache": "Verificação de integridade do cache concluída",
    "coupon-sync": "Sincronização periódica de cupons concluída",
}


def _info_do_container(nome_container):
    """(mac, container_id, servidor, container) a partir só do nome - os
    sete containers do cenário têm nomes únicos entre si."""
    for nome_servidor, containers in CONTAINERS.items():
        for container in containers:
            if container["nome"] == nome_container:
                servidor = SERVIDORES_POR_NOME[nome_servidor]
                return servidor["mac"], id_do_container(servidor["mac"], nome_container), servidor, container
    raise KeyError(nome_container)


def gerar_linhas_eventos():
    """Eventos de ciclo de vida dos containers, no formato real do coletor
    (capturas/containers/eventos.py: COLUNAS_EVENTOS + `descrever_evento`).
    """
    linhas = []
    for nome_container, timestamp, acao, codigo_saida in EVENTOS_NARRADOS:
        if not (INICIO_HISTORICO <= timestamp <= FIM):
            continue
        mac, container_id, _servidor, _container = _info_do_container(nome_container)
        atributos = {"exitCode": codigo_saida} if codigo_saida is not None else {}
        linhas.append({
            "TIMESTAMP": para_timestamp_iso(timestamp),
            "ENDERECO_MAC": mac,
            "EVENT_ID": hashlib.sha256(
                f"{container_id}-{acao}-{timestamp.isoformat()}".encode("utf-8")
            ).hexdigest(),
            "CONTAINER_ID": container_id,
            "CONTAINER": nome_container,
            "EVENT_TYPE": "container",
            "EVENT_ACTION": acao,
            "EVENT_DETAILS": descrever_evento(acao, atributos),
            "EXIT_CODE": codigo_saida,
        })
    linhas.sort(key=lambda linha: linha["TIMESTAMP"])
    return linhas


def gerar_linhas_logs():
    """Logs dos containers: as mensagens narradas (seção 8) + WARN perto dos
    episódios históricos calibrados + INFO esparso de rotina. Nenhuma linha
    nova depois do encerramento definitivo de um container (seção C)."""
    linhas = []

    def adicionar(nome_container, timestamp, nivel, mensagem):
        if not (INICIO_HISTORICO <= timestamp <= FIM):
            return
        mac, container_id, _servidor, container_info = _info_do_container(nome_container)
        encerramento = container_info.get("encerramento")
        if encerramento is not None and timestamp > encerramento:
            return
        linhas.append({
            "TIMESTAMP": para_timestamp_iso(timestamp),
            "ENDERECO_MAC": mac,
            "CONTAINER_ID": container_id,
            "CONTAINER": nome_container,
            "LOG_LEVEL": nivel,
            "LOG_MENSAGEM": mensagem,
        })

    for nome_container, timestamp, nivel, mensagem in LOGS_NARRADOS:
        adicionar(nome_container, timestamp, nivel, mensagem)

    for nome_container, episodios, mensagem in LOGS_EPISODIOS:
        for inicio_ep, fim_ep, _pico in episodios:
            adicionar(nome_container, inicio_ep + (fim_ep - inicio_ep) / 2, "WARN", mensagem)

    for nome_container, mensagem in MENSAGENS_ROTINA.items():
        _mac, _container_id, _servidor, container_info = _info_do_container(nome_container)
        aleatorio = gerador_aleatorio(f"logs-rotina-{nome_container}")
        encerramento = container_info.get("encerramento")
        fim_janela = min(FIM, encerramento) if encerramento else FIM

        dia = INICIO_HISTORICO.replace(hour=0, minute=0, second=0, microsecond=0)
        while dia <= fim_janela:
            # Um horário comercial aleatório por dia, determinístico por
            # container (mesma semente -> mesmo resultado em duas execuções).
            minuto_do_dia = aleatorio.randint(6 * 60, 22 * 60)
            timestamp = dia + timedelta(minutes=minuto_do_dia)
            if INICIO_HISTORICO <= timestamp <= fim_janela:
                adicionar(nome_container, timestamp, "INFO", mensagem)
            dia += timedelta(days=1)

    linhas.sort(key=lambda linha: linha["TIMESTAMP"])
    return linhas


# ===========================================================================
# 13. CSV e upload
# ===========================================================================


def montar_csv(linhas, colunas, delimitador=","):
    """Monta o conteúdo do CSV na memória (nada é escrito em disco).

    Métricas/processos/containers usam ',' (como já era). Logs e eventos
    usam ';', igual ao agente real (capturas/captura_docker.py) - a Silver
    detecta o delimitador de cada arquivo automaticamente (`sep=None` no
    pandas), então os dois formatos convivem sem conflito.
    """
    buffer = io.StringIO()
    escritor = csv.DictWriter(
        buffer, fieldnames=colunas, extrasaction="raise", delimiter=delimitador
    )
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


def enviar_para_s3(conteudo, nome_arquivo, prefixo, bucket, cliente=None):
    """Sobe o CSV para o S3 de verdade, no prefixo raw/.

    As credenciais vêm do próprio ambiente (a sessão do AWS CLI / AWS Academy
    Lab). O nome do bucket NUNCA é escrito no código: ele vem da variável de
    ambiente BUCKET_DESTINO.
    """
    import boto3

    cliente = cliente or boto3.client("s3")
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


def validar_cobertura(horarios):
    """Checagens da seção 4.1 do relatório: ordenação, ausência de
    duplicidade, amostragem de INTERVALO_SEGUNDOS e contagem de dias civis
    tocados pela janela. Não corrige nada - só relata."""
    problemas = []
    if horarios != sorted(horarios):
        problemas.append("instantes fora de ordem")
    if len(set(horarios)) != len(horarios):
        problemas.append("instantes duplicados")
    passos = {
        (b - a).total_seconds() for a, b in zip(horarios, horarios[1:])
    }
    if passos and passos != {float(INTERVALO_SEGUNDOS)}:
        problemas.append(f"amostragem irregular: passos distintos de {INTERVALO_SEGUNDOS}s -> {sorted(passos)}")
    dias_civis = {horario.date() for horario in horarios}
    print(
        f"  cobertura: {len(horarios)} instantes, {horarios[0]} a {horarios[-1]}, "
        f"{len(dias_civis)} dias civis distintos"
    )
    return problemas


def contar_reinicios_coupon_sync(linhas_eventos):
    """Reconciliação pedida na seção 4/6 do relatório: reinicializações
    (pares die+start) no dia 27/11 vs nos últimos 7 dias vs no total gerado.
    `die` sem `start` seguinte (o encerramento final) não conta como
    reinicialização."""
    dies = sorted(
        datetime.fromisoformat(linha["TIMESTAMP"].replace("Z", "+00:00"))
        for linha in linhas_eventos
        if linha["CONTAINER"] == "coupon-sync" and linha["EVENT_ACTION"] == "die"
    )
    starts = sorted(
        datetime.fromisoformat(linha["TIMESTAMP"].replace("Z", "+00:00"))
        for linha in linhas_eventos
        if linha["CONTAINER"] == "coupon-sync" and linha["EVENT_ACTION"] == "start"
    )
    reinicios = [die for die in dies if any(die < start for start in starts)]

    fim_utc = para_utc(FIM)
    janela_7d = fim_utc - timedelta(days=7)
    inicio_27_11_utc = para_utc(datetime(2026, 11, 27, 0, 0))

    no_dia_27 = [r for r in reinicios if r >= inicio_27_11_utc]
    em_7d = [r for r in reinicios if r >= janela_7d]
    print(
        f"  coupon-sync: {len(reinicios)} reinicializações no total gerado, "
        f"{len(em_7d)} nos últimos 7 dias (a partir de {janela_7d.isoformat()}), "
        f"{len(no_dia_27)} em 27/11 (esperado: 2)"
    )
    return reinicios


def dividir_em_lotes(categorias, dias_por_lote):
    """Particiona por dias civis, sem regenerar, copiar ou alterar registros."""
    fuso = ZoneInfo("America/Sao_Paulo")
    primeiro_dia = INICIO_HISTORICO.date()
    lotes = {}
    for nome, (linhas, _, _, _) in categorias.items():
        for linha in linhas:
            instante = datetime.fromisoformat(linha["TIMESTAMP"].replace("Z", "+00:00"))
            if instante.tzinfo is None:
                instante = instante.replace(tzinfo=fuso)
            dia = instante.astimezone(fuso).date()
            inicio = primeiro_dia + timedelta(
                days=((dia - primeiro_dia).days // dias_por_lote) * dias_por_lote
            )
            lote = lotes.setdefault(inicio, {categoria: [] for categoria in categorias})
            lote[nome].append(linha)
    return dict(sorted(lotes.items()))


def nome_arquivo_lote(nome, inicio, dias_por_lote, conteudo):
    # A identidade inclui o conteúdo: repetir a mesma execução sobrescreve a
    # mesma chave, inclusive numa retomada após upload parcial.
    digest = hashlib.sha256(conteudo.encode("utf-8")).hexdigest()
    return f"{nome}_{inicio:%Y%m%d}T000000_{dias_por_lote}d_{digest}.csv"


def verificar_gatilhos(sessao, s3, cliente_lambda, bucket, nome_lambda):
    """Preflight somente leitura. Falta de permissão também impede o envio.

    Notificações indiretas (SNS/SQS/EventBridge) são bloqueadas de forma
    conservadora: este script não consegue provar que não disparam a esteira.
    A verificação é um retrato da configuração, não um lock distribuído.
    """
    configuracao = cliente_lambda.get_function_configuration(FunctionName=nome_lambda)
    arn = configuracao["FunctionArn"].split(":")
    arn = ":".join(arn[:7])  # inclui function:nome, sem versão/alias
    notificacoes = s3.get_bucket_notification_configuration(Bucket=bucket)
    conflitos = []
    for campo in ("LambdaFunctionConfigurations", "QueueConfigurations", "TopicConfigurations"):
        if notificacoes.get(campo):
            conflitos.append(f"S3: {campo}")
    if "EventBridgeConfiguration" in notificacoes:
        conflitos.append("S3: notificações EventBridge habilitadas")

    def mesma_funcao(alvo):
        return alvo == arn or alvo.startswith(arn + ":")

    for pagina in cliente_lambda.get_paginator("list_event_source_mappings").paginate(FunctionName=arn):
        for origem in pagina.get("EventSourceMappings", []):
            if origem.get("State") != "Disabled":
                conflitos.append(f"event source mapping: {origem['UUID']}")

    eventos = sessao.client("events")

    def paginas_barramentos():
        parametros = {}
        while True:
            pagina = eventos.list_event_buses(**parametros)
            yield pagina
            if not pagina.get("NextToken"):
                return
            parametros["NextToken"] = pagina["NextToken"]

    for pagina in paginas_barramentos():
        for barramento in pagina.get("EventBuses", []):
            for regras in eventos.get_paginator("list_rules").paginate(EventBusName=barramento["Name"]):
                for regra in regras.get("Rules", []):
                    if regra.get("State") == "DISABLED":
                        continue
                    for alvos in eventos.get_paginator("list_targets_by_rule").paginate(
                        Rule=regra["Name"], EventBusName=barramento["Name"]
                    ):
                        if any(mesma_funcao(alvo["Arn"]) for alvo in alvos.get("Targets", [])):
                            conflitos.append(f"EventBridge: {regra['Name']}")

    scheduler = sessao.client("scheduler")
    for pagina in scheduler.get_paginator("list_schedules").paginate():
        for agenda in pagina.get("Schedules", []):
            alvo = agenda.get("Target", {}).get("Arn", "")
            if agenda.get("State") == "ENABLED" and (
                mesma_funcao(alvo) or ":aws-sdk:lambda:invoke" in alvo
            ):
                conflitos.append(f"Scheduler: {agenda['GroupName']}/{agenda['Name']}")

    # Impede execução simultânea mesmo se outro chamador surgir depois da
    # verificação. Não altera a configuração da conta automaticamente.
    concorrencia = cliente_lambda.get_function_concurrency(FunctionName=arn)
    if concorrencia.get("ReservedConcurrentExecutions") != 1:
        conflitos.append("configure a concorrência reservada da Lambda como 1 para esta carga")
    if conflitos:
        raise RuntimeError("Envio bloqueado por risco de concorrência: " + "; ".join(conflitos))
    print("[AWS] Verificação concluída. Mantenha gatilhos suspensos e não execute outro produtor durante a carga.", flush=True)
    return configuracao["Timeout"]


def invocar_lambda(cliente, nome_lambda):
    resposta = cliente.invoke(
        FunctionName=nome_lambda, InvocationType="RequestResponse", Payload=b"{}"
    )
    corpo = resposta["Payload"]
    try:
        texto = corpo.read().decode("utf-8")
    finally:
        corpo.close()
    if resposta.get("FunctionError") or resposta.get("StatusCode") != 200:
        raise RuntimeError(f"Lambda falhou: {resposta.get('FunctionError')}: {texto[:2000]}")
    resultado = json.loads(texto)
    # Contrato do lambda_handler atual: statusCode=200 e body JSON.
    if not isinstance(resultado, dict) or resultado.get("statusCode") != 200:
        raise RuntimeError(f"Lambda não confirmou sucesso: {texto[:2000]}")
    corpo_resultado = resultado.get("body")
    if isinstance(corpo_resultado, str):
        try:
            corpo_resultado = json.loads(corpo_resultado)
        except ValueError:
            raise RuntimeError(f"Resposta da Lambda fora do contrato: {texto[:2000]}")
    for parte in (resultado, corpo_resultado):
        if isinstance(parte, dict) and (
            any(parte.get(chave) for chave in ("error", "errorMessage", "erro", "errors"))
            or parte.get("success") is False
            or ("statusCode" in parte and parte["statusCode"] != 200)
        ):
            raise RuntimeError(f"Erro no conteúdo da Lambda: {texto[:2000]}")
    if corpo_resultado != "Pipeline Silver + Gold finalizado com sucesso!":
        raise RuntimeError(f"Confirmação inesperada da Lambda: {texto[:2000]}")


def executar_lotes(categorias, argumentos, inicio_execucao):
    lotes = dividir_em_lotes(categorias, argumentos.dias_por_lote)
    if argumentos.retomar_de and argumentos.retomar_de not in lotes:
        raise ValueError("--retomar-de deve ser a data inicial de um lote, mantendo --dias-historico e --dias-por-lote originais")
    selecionados = [(dia, lote) for dia, lote in lotes.items()
                    if argumentos.retomar_de is None or dia >= argumentos.retomar_de]
    bucket = os.environ.get("BUCKET_DESTINO")
    cliente_s3 = cliente_lambda = None
    if not argumentos.somente_local:
        print("ATENÇÃO: a Gold atual ainda relê todo trusted/; os lotes não limitam o histórico reprocessado.", flush=True)
        if not bucket:
            raise ValueError("Defina BUCKET_DESTINO ou use --somente-local")
        import boto3
        from botocore.config import Config
        # Uma tentativa total evita reexecuções automáticas após timeout de rede.
        config = Config(connect_timeout=10, read_timeout=60,
                        retries={"total_max_attempts": 1}, tcp_keepalive=True)
        sessao = boto3.Session()
        cliente_s3 = sessao.client("s3", config=config)
        if argumentos.lambda_nome:
            cliente_lambda = sessao.client("lambda", config=config)
            timeout = verificar_gatilhos(sessao, cliente_s3, cliente_lambda, bucket, argumentos.lambda_nome)
            cliente_lambda = sessao.client("lambda", config=config.merge(Config(read_timeout=timeout + 60)))
        else:
            print("ATENÇÃO: sem --lambda-nome, modo somente upload; nenhum processamento será confirmado.", flush=True)
    else:
        Path(argumentos.pasta).mkdir(parents=True, exist_ok=True)

    concluidos = registros = arquivos = falhas = 0
    try:
        for indice, (dia, lote) in enumerate(selecionados, 1):
            inicio_lote = time.monotonic()
            print(f"\n[LOTE {indice:02}/{len(selecionados):02}] Data: {dia} (até {min(dia + timedelta(days=argumentos.dias_por_lote - 1), FIM.date())})", flush=True)
            for nome, linhas in lote.items():
                print(f"  {nome}: {len(linhas)} registros", flush=True)
            try:
                for nome, linhas in lote.items():
                    if not linhas:
                        continue
                    _, colunas, delimitador, prefixo = categorias[nome]
                    conteudo = montar_csv(linhas, colunas, delimitador)
                    arquivo = nome_arquivo_lote(nome, dia, argumentos.dias_por_lote, conteudo)
                    if argumentos.amostras > 0:
                        print(f"--- amostra de {nome} ({arquivo}) ---")
                        print("\n".join(conteudo.splitlines()[:argumentos.amostras + 1]))
                    if argumentos.somente_local:
                        destino = Path(argumentos.pasta) / arquivo
                        destino.write_text(conteudo, encoding="utf-8")
                        print(f"[local] Salvo: {destino}", flush=True)
                    else:
                        enviar_para_s3(conteudo, arquivo, prefixo, bucket, cliente=cliente_s3)
                    arquivos += 1
                    registros += len(linhas)
                if cliente_lambda is not None:
                    print("Upload concluído. Executando Lambda...", flush=True)
                    invocar_lambda(cliente_lambda, argumentos.lambda_nome)
                    print("Processamento concluído com sucesso.", flush=True)
                concluidos += 1
                print(f"Tempo total do lote: {time.monotonic() - inicio_lote:.1f}s", flush=True)
                if not argumentos.somente_local and indice < len(selecionados):
                    time.sleep(argumentos.intervalo_entre_lotes)
            except (Exception, KeyboardInterrupt) as erro:
                falhas += 1
                print(f"FALHA no lote {dia}: {erro}\nRetome com --retomar-de {dia}, mantendo os demais parâmetros. "
                      "Se a conexão caiu, confira a execução da Lambda antes de retomar; ela pode ainda estar em andamento.", flush=True)
                return 1
    finally:
        modo = "salvos localmente" if argumentos.somente_local else "enviados"
        print(f"\nResumo: {concluidos}/{len(selecionados)} lotes concluídos; "
              f"{registros} registros e {arquivos} arquivos {modo}; {falhas} falhas; "
              f"tempo total: {time.monotonic() - inicio_execucao:.1f}s. "
              f"Processamento {'confirmado para os lotes concluídos' if cliente_lambda is not None else 'não confirmado (modo local/upload)'}.", flush=True)
    return 0


def main():
    inicio_execucao = time.monotonic()
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
    analisador.add_argument(
        "--dias-historico",
        type=int,
        default=DIAS_HISTORICO_PADRAO,
        help=(
            "quantos dias ANTES de 27/11 20h30 o gerador cobre (padrão: "
            f"{DIAS_HISTORICO_PADRAO}). Use 0 pra gerar só a janela narrada "
            "original (18h45-20h30 de 27/11), igual ao gerador antigo - útil "
            "pra iterar rápido."
        ),
    )
    analisador.add_argument("--dias-por-lote", type=int, default=1, help="dias civis por lote (padrão: 1)")
    analisador.add_argument("--lambda-nome", help="Lambda Silver + Gold a invocar sincronamente")
    analisador.add_argument("--intervalo-entre-lotes", type=float, default=0, help="pausa adicional em segundos; não confirma processamento")
    analisador.add_argument("--retomar-de", type=date.fromisoformat, help="data inicial do lote a retomar (AAAA-MM-DD); preserve os outros parâmetros")
    argumentos = analisador.parse_args()
    if argumentos.dias_por_lote < 1:
        analisador.error("--dias-por-lote deve ser maior que zero")
    if not math.isfinite(argumentos.intervalo_entre_lotes) or argumentos.intervalo_entre_lotes < 0:
        analisador.error("--intervalo-entre-lotes deve ser finito e não negativo")

    if argumentos.dias_historico < 0:
        print("ERRO: --dias-historico não pode ser negativo.")
        return 1

    global INICIO_HISTORICO
    # min(...) garante que --dias-historico 0 ainda cobre a janela narrada
    # inteira (18h45-20h30): "0 dias de histórico ALÉM da janela narrada",
    # não "FIM menos zero dias" (que seria só o instante de referência).
    INICIO_HISTORICO = min(INICIO, FIM - timedelta(days=argumentos.dias_historico))

    print(
        f"Gerando o cenário Black Friday: histórico de {argumentos.dias_historico} dia(s) "
        f"({INICIO_HISTORICO} a {FIM}), incidente narrado em 27/11 18h45-20h30..."
    )

    cabecalho = cabecalho_metricas()
    linhas_metricas = gerar_linhas_metricas()
    linhas_processos = gerar_linhas_processos()
    linhas_containers = gerar_linhas_containers()
    linhas_eventos = gerar_linhas_eventos()
    linhas_logs = gerar_linhas_logs()

    print(f"  métricas: {len(linhas_metricas)} linhas x {len(cabecalho)} colunas")
    print(f"  processos: {len(linhas_processos)} linhas x {len(COLUNAS_PROCESSOS)} colunas")
    print(f"  containers: {len(linhas_containers)} linhas x {len(COLUNAS_CONTAINERS)} colunas")
    print(f"  eventos: {len(linhas_eventos)} linhas x {len(COLUNAS_EVENTOS)} colunas")
    print(f"  logs: {len(linhas_logs)} linhas x {len(COLUNAS_LOGS)} colunas")

    problemas = conferir_cenario(linhas_metricas)

    print("\n" + "=" * 78)
    print("VALIDAÇÃO DE COBERTURA E RECONCILIAÇÃO (seção 4 do relatório)")
    print("=" * 78)
    problemas.extend(validar_cobertura(horarios_da_janela()))
    contar_reinicios_coupon_sync(linhas_eventos)

    categorias = {
        "metricas": (linhas_metricas, cabecalho, ",", PREFIXO_S3),
        "processos": (linhas_processos, COLUNAS_PROCESSOS, ",", PREFIXO_S3),
        "containers": (linhas_containers, COLUNAS_CONTAINERS, ",", PREFIXO_S3_CONTAINERS),
        "eventos": (linhas_eventos, COLUNAS_EVENTOS, ";", PREFIXO_S3_EVENTOS),
        "logs": (linhas_logs, COLUNAS_LOGS, ";", PREFIXO_S3_LOGS),
    }
    try:
        resultado = executar_lotes(categorias, argumentos, inicio_execucao)
    except Exception as erro:
        print(f"ERRO antes do envio dos lotes: {erro}", flush=True)
        print(f"Resumo: 0 lotes concluídos; 0 registros e 0 arquivos enviados; "
              f"1 falha; tempo total: {time.monotonic() - inicio_execucao:.1f}s.", flush=True)
        return 1
    if resultado:
        return resultado

    if problemas:
        print("\nATENÇÃO: confira os problemas listados acima antes de apresentar.")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
