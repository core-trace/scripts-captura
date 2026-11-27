"""Junta os resumos aos poucos, sem guardar todas as capturas do dia na memória."""
import math
import pandas as pd

from copy import deepcopy
from formato_gold import identidade_recurso, para_cada_medida

def instante_utc(instante):

    # Converte o texto em um Timestamp do Pandas.
    # Isso também preserva a precisão de nanossegundos.
    horario = pd.Timestamp(instante)

    if pd.isna(horario):
        raise ValueError("Timestamp invalido")

    # Capturas tradicionais usam o horário local de São Paulo.
    # Capturas Docker já possuem o fuso UTC informado.
    if horario.tzinfo is None:
        horario = horario.tz_localize("America/Sao_Paulo")

    return horario.tz_convert("UTC")

# puxei isso pra fora do instante_comparavel pq vou precisar do Timestamp em si
# (nao só do numero em nanossegundos) pra arredondar pro balde de 5 min na evolucao -
# e nao queria copiar a parte do fuso horario de novo, essa conta ja tem uma pegadinha
# (tradicional vs docker) que só existe escrita uma vez
def instante_comparavel(instante):
    # Valor em nanossegundos, pra poder comparar/ordenar horarios igual
    return instante_utc(instante).value

# Mesmo instante, em UTC e no formato ISO com Z, igual ao que a evolução já entrega.
# Os campos novos do resumo (dashboard de servidores) usam isso pra quem lê não precisar saber o fuso da captura.
def instante_iso_utc(instante):
    return instante_utc(instante).isoformat().replace('+00:00', 'Z')

# Aqui juntamos o que já foi calculado com as novas amostras que chegaram
# A cópia evita mudar o resumo anterior enquanto ainda estamos usando ele na conta
def combinar_medidas(anterior, nova, mais_recente):
    if anterior is None:
        return deepcopy(nova)

    for campo in ('metrica', 'tipo', 'unidade'):
        if anterior.get(campo) != nova.get(campo):
            raise ValueError('Tipo, unidade ou metrica mudou durante a agregacao')

    # O último valor é escolhido pelo horário da coleta, não pela ordem de chegada
    if mais_recente:
        resultado = deepcopy(nova)
    else:
        resultado = deepcopy(anterior)

    quantidade_anterior = anterior['amostras_validas']
    quantidade_nova = nova['amostras_validas']
    total_validas = quantidade_anterior + quantidade_nova
    resultado['amostras_validas'] = total_validas
    resultado['amostras_invalidas'] = anterior['amostras_invalidas'] + nova['amostras_invalidas']
    tipo = resultado.get('tipo', 'medicao')

    if tipo == 'medicao':
        # Não dá pra fazer (média antiga + média nova) / 2, pq os lotes podem ter tamanhos diferentes
        # Multiplicar cada média pela quantidade recupera a soma usada naquele resumo
        soma = 0
        if quantidade_anterior > 0:
            soma += anterior['media'] * quantidade_anterior
        if quantidade_nova > 0:
            soma += nova['media'] * quantidade_nova

        if total_validas > 0:
            resultado['media'] = soma / total_validas
        else:
            resultado['media'] = None

        for campo, calcular in [('minimo', min), ('maximo', max)]:
            valores = []
            for resumo in (anterior, nova):
                if resumo[campo] is not None:
                    valores.append(resumo[campo])
            if valores:
                resultado[campo] = calcular(valores)
            else:
                resultado[campo] = None
        # Último valor válido da medição e o instante dele. Se a amostra mais recente veio
        # sem valor válido, vale o da outra: o valor atual nunca vira null por causa de uma
        # leitura ruim.
        candidatos = []
        for resumo in (anterior, nova):
            if resumo.get('ultimo_valor') is not None and resumo.get('ultimo_instante'):
                candidatos.append(resumo)
        if candidatos:
            melhor = max(candidatos, key=lambda resumo: instante_comparavel(resumo['ultimo_instante']))
            resultado['ultimo_valor'] = melhor['ultimo_valor']
            resultado['ultimo_instante'] = melhor['ultimo_instante']
        else:
            resultado['ultimo_valor'] = None
            resultado['ultimo_instante'] = None

    elif tipo in ('eventos_intervalo', 'delta_intervalo'):
        # Aqui são valores novos daquele intervalo, então eles podem ser somados
        # Essa regra não vale pros contadores acumulados do Docker
        valores = []
        for resumo in (anterior, nova):
            if resumo['total_observado'] is not None:
                valores.append(resumo['total_observado'])
        if valores:
            resultado['total_observado'] = sum(valores)
        else:
            resultado['total_observado'] = None

    elif tipo == 'estado':
        # É a porcentagem de amostras em que a interface estava ativa, não tempo de disponibilidade
        soma = 0
        if quantidade_anterior > 0:
            soma += anterior['percentual_amostras_ativas'] * quantidade_anterior
        if quantidade_nova > 0:
            soma += nova['percentual_amostras_ativas'] * quantidade_nova
        if total_validas > 0:
            resultado['percentual_amostras_ativas'] = soma / total_validas
        else:
            resultado['percentual_amostras_ativas'] = None

    # Inventário e contadores ficam com o último valor que escolhemos, sem média ou soma
    return resultado


# A estrutura guarda só os resultados em andamento e o último horário de cada parte
# Essa função se chama de novo quando encontra um dicionário ou lista dentro de outro
# Assim CPU, RAM, discos e interfaces seguem o mesmo cálculo, sem repetir uma função por categoria
def atualizar(estrutura, valor, instante, nome=''):
    if estrutura is None:
        estrutura = {'instante': instante}

    # Compara os instantes reais, independentemente do fuso.
    mais_recente = (
        instante_comparavel(instante)
        >= instante_comparavel(estrutura["instante"])
    )

    if isinstance(valor, dict):
        if 'amostras_validas' in valor:
            # Chegamos numa medida que já tem suas contagens e resultados calculados
            # Entrada e saída da rede são tratadas separadas do total do grupo
            medida = {}
            for campo, conteudo in valor.items():
                if campo not in ('recebidos', 'enviados'):
                    medida[campo] = conteudo

            # O resumo só tinha média, mínimo e máximo das medições, sem o valor ATUAL. Cada observação
            # que chega aqui vem de uma única captura, então quando existe 1 amostra válida a média dela
            # é o próprio valor lido. Tipos que já têm ultimo_valor (inventário, contador, estado) ficam como estão.
            if medida.get('tipo') == 'medicao' and 'ultimo_valor' not in medida:
                if medida.get('amostras_validas') == 1 and medida.get('media') is not None:
                    medida['ultimo_valor'] = medida['media']
                    medida['ultimo_instante'] = instante_iso_utc(instante)
                else:
                    medida['ultimo_valor'] = None
                    medida['ultimo_instante'] = None

            estrutura['tipo'] = 'medida'
            estrutura['valor'] = combinar_medidas(estrutura.get('valor'), medida, mais_recente)
            if 'direcoes' not in estrutura:
                estrutura['direcoes'] = {}
            direcoes = estrutura['direcoes']
            for direcao in ('recebidos', 'enviados'):
                if direcao in valor:
                    anterior = direcoes.get(direcao)
                    direcoes[direcao] = atualizar(anterior, valor[direcao], instante)
        else:
            # Ainda estamos numa categoria/grupo, como cpu, ram ou frequencia
            estrutura['tipo'] = 'objeto'
            if 'campos' not in estrutura:
                estrutura['campos'] = {}
            campos = estrutura['campos']
            for campo, conteudo in valor.items():
                campos[campo] = atualizar(campos.get(campo), conteudo, instante, campo)

    elif isinstance(valor, list):
        # Não podemos misturar todos os discos ou todas as CPUs numa média só
        # A identidade permite juntar apenas observações do mesmo recurso
        estrutura['tipo'] = 'lista'
        if 'recursos' not in estrutura:
            estrutura['recursos'] = {}
        recursos = estrutura['recursos']
        for recurso in valor:
            identidade = identidade_recurso(recurso)
            recursos[identidade] = atualizar(recursos.get(identidade), recurso, instante)

    else:
        # Chegamos num valor simples: horário, nome, índice ou quantidade de observações
        estrutura['tipo'] = 'valor'
        if nome in ('inicio', 'primeira_observacao'):
            horarios = []
            for horario in (estrutura.get('valor'), valor):
                if horario is not None:
                    horarios.append(horario)
            if horarios:
                # Seleciona o instante mais antigo, não a menor string.
                estrutura['valor'] = min(
                    horarios,
                    key=instante_comparavel
                )
            else:
                estrutura['valor'] = None
        elif nome in ('fim', 'ultima_observacao'):
            horarios = []
            for horario in (estrutura.get('valor'), valor):
                if horario is not None:
                    horarios.append(horario)
            if horarios:
                # Seleciona o instante mais recente.
                estrutura['valor'] = max(
                    horarios,
                    key=instante_comparavel
                )
            else:
                estrutura['valor'] = None
        elif nome == 'amostras_observadas':
            estrutura['valor'] = estrutura.get('valor', 0) + valor
        elif mais_recente or 'valor' not in estrutura:
            estrutura['valor'] = valor

    # Mantém o instante mais recente para as próximas agregações.
    estrutura["instante"] = max(
        estrutura["instante"],
        instante,
        key=instante_comparavel
    )
    return estrutura


# Tira o controle interno do acumulador e devolve só o que vai pro JSON
# Os campos internos tipo/campos/recursos daqui não são novos campos do contrato
def exportar(estrutura):
    if estrutura['tipo'] == 'objeto':
        resultado = {}
        for campo, conteudo in estrutura['campos'].items():
            resultado[campo] = exportar(conteudo)
        return resultado

    if estrutura['tipo'] == 'lista':
        resultado = []
        for recurso in estrutura['recursos'].values():
            resultado.append(exportar(recurso))
        return resultado

    if estrutura['tipo'] == 'medida':
        resultado = estrutura['valor'].copy()
        for direcao, conteudo in estrutura['direcoes'].items():
            resultado[direcao] = exportar(conteudo)
        return resultado

    return estrutura['valor']


# Cada máquina/dia tem seu próprio acumulador. A Gold vai adicionando uma observação por vez
class AcumuladorResumo:
    def __init__(self):
        self.estrutura = None

    def adicionar(self, resumo, instante):
        self.estrutura = atualizar(self.estrutura, resumo, instante)

    def resultado(self):
        if self.estrutura is None:
            return {}
        return exportar(self.estrutura)


# Replica EXATAMENTE a comparação que o api-java faz em ProcessadorJson.atingiuLimite.
# Os dois lados precisam concordar: se um dia alguém comparar lado a lado o alerta
# gerado pelo Java com o "tempo no estado" calculado aqui, a conta tem que bater.
def atingiu_limite(valor, limite, operador):
    if limite is None:
        return False

    operador = operador.strip() if operador and operador.strip() else '>'

    if operador == '>':
        return valor > limite
    if operador == '>=':
        return valor >= limite
    if operador == '<':
        return valor < limite
    if operador == '<=':
        return valor <= limite
    if operador in ('=', '=='):
        return valor == limite
    if operador in ('!=', '<>'):
        return valor != limite

    print(f"Operador '{operador}' nao suportado; limite ignorado.")
    return False


# componentes_perfil permite uma segunda condição independente (operador_2 +
# seus próprios limiares). O Java avalia as duas condições com OR, não com AND:
# ou a condição 1 bate, ou a condição 2 bate - qualquer uma das duas já é suficiente.
# Crítico tem prioridade sobre atenção, mesmo que a condição de atenção também bata.
def classificar_estado(valor, limites):
    operador = limites.get('operador')
    operador_2 = limites.get('operador_2')
    tem_segunda_condicao = bool(operador_2 and operador_2.strip())

    critico = atingiu_limite(valor, limites.get('limite_critico'), operador)
    if not critico and tem_segunda_condicao:
        critico = atingiu_limite(valor, limites.get('limite_critico_2'), operador_2)

    if critico:
        return 'critico'

    atencao = atingiu_limite(valor, limites.get('limite_atencao'), operador)
    if not atencao and tem_segunda_condicao:
        atencao = atingiu_limite(valor, limites.get('limite_atencao_2'), operador_2)

    if atencao:
        return 'atencao'

    return 'normal'


# "Atencao" e "critico" são níveis: todo valor crítico também conta como
# estar em atenção-ou-pior. É por isso que precisamos achar as DUAS datas
# de início separadamente, andando de trás pra frente a partir da última leitura.
def _desde_contiguo(leituras_ordenadas, limites, niveis_aceitos):
    # leituras_ordenadas: lista de (instante, valor), já ordenada do mais antigo pro mais novo.
    desde = leituras_ordenadas[-1][0]
    for instante, valor in reversed(leituras_ordenadas[:-1]):
        if classificar_estado(valor, limites) in niveis_aceitos:
            desde = instante
        else:
            break
    return desde


def calcular_tempo_no_estado(leituras_ordenadas, limites):
    """Recebe as leituras (instante, valor) de UMA métrica num dia, já ordenadas
    por horário, e devolve desde quando ela está ininterruptamente em atenção
    e/ou em crítico, olhando de trás pra frente a partir da última leitura."""
    if not leituras_ordenadas:
        return None

    ultimo_instante, ultimo_valor = leituras_ordenadas[-1]
    estado_atual = classificar_estado(ultimo_valor, limites)

    if estado_atual == 'normal':
        return {'estado_atual': 'normal'}

    resultado = {
        'estado_atual': estado_atual,
        'em_atencao_desde': _desde_contiguo(leituras_ordenadas, limites, ('atencao', 'critico')),
    }
    if estado_atual == 'critico':
        resultado['em_critico_desde'] = _desde_contiguo(leituras_ordenadas, limites, ('critico',))
    return resultado


# Mecanismo PARALELO ao AcumuladorResumo, de propósito: "tempo no estado" depende
# da ordem cronológica das leituras (preciso saber qual veio antes de qual pra achar
# onde o estado começou), enquanto o AcumuladorResumo soma/tira média e por isso é
# comutativo (não importa a ordem de chegada dos lotes). As duas contas não podem
# compartilhar a mesma estrutura sem uma delas perder sua garantia.
#
# Limitação conhecida e aceita aqui: guardamos a lista inteira de leituras do dia em
# memória, sem nenhum teto. Para o cenário de demonstração (um dia, poucos servidores)
# isso é tranquilo; numa escala de produção com muitos servidores/dias precisaria de
# outra estratégia (por exemplo, descartar leituras mais antigas que o maior
# "desde" que ainda importa).
#
# Limitação conhecida nº 2: o agrupamento é por nome de métrica (ex.: "disk_percent"),
# não por recurso físico. Se um servidor tiver mais de um disco ou mais de uma
# interface de rede, as leituras dos dois recursos entram na mesma série. Os
# servidores de demonstração têm só um disco e uma interface, então isso não afeta
# o resultado aqui - mas é uma simplificação, não uma garantia geral.
class AcumuladorTempoNoEstado:
    def __init__(self):
        self.leituras_por_metrica = {}

    def adicionar(self, metrica, instante, valor, status):
        # Só leituras válidas entram, igual ao resumir_leituras do formato_gold.
        if metrica is None or status != 'ok' or valor is None:
            return
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return
        self.leituras_por_metrica.setdefault(metrica, []).append((instante, valor))

    def calcular(self, limites_por_metrica):
        resultado = {}
        for metrica, leituras in self.leituras_por_metrica.items():
            limites = limites_por_metrica.get(metrica)
            if limites is None:
                continue  # metrica sem limite configurado nesse perfil: nada pra comparar

            leituras_ordenadas = sorted(leituras, key=lambda par: instante_comparavel(par[0]))
            tempo = calcular_tempo_no_estado(leituras_ordenadas, limites)
            if tempo is not None:
                resultado[metrica] = tempo
        return resultado


# previsao de quando uma metrica vai cruzar o proximo limite, olhando so 2
# pontos (inicio e fim) - mesma ideia do preverVolume que
# ja existe em website/public/scripts/dashboardDados.js, so que em minutos em
# vez de dias, pq cpu/ram variam rapido demais pra fazer sentido medir em dias
# importante usar só os ultimos 30 min, nao o dia inteiro

JANELA_PREVISAO_MINUTOS = 30
# previsao de mais de 2h nao e mais "risco em acompanhamento", e so ruido longe
# demais pra alguem agir rapido
HORIZONTE_MAXIMO_MINUTOS = 120

# só funciona pra metrica "quanto maior pior" (operador >), que e o unico jeito
# que o projeto configura hoje 
def calcular_previsao(leituras_ordenadas, limites):
    if len(leituras_ordenadas) < 3:
        return None  # pouca amostra, previsao ia ser chute

    ultimo_instante, ultimo_valor = leituras_ordenadas[-1]
    limite_da_janela = instante_utc(ultimo_instante) - pd.Timedelta(minutes=JANELA_PREVISAO_MINUTOS)
    recentes = [par for par in leituras_ordenadas if instante_utc(par[0]) >= limite_da_janela]

    if len(recentes) < 3:
        return None  # pouca amostra na janela recente

    primeiro_instante, primeiro_valor = recentes[0]

    minutos_decorridos = (instante_utc(ultimo_instante) - instante_utc(primeiro_instante)).total_seconds() / 60
    if minutos_decorridos < 10:
        return None  # janela curta demais, previsao ia ser so ruido

    estado_atual = classificar_estado(ultimo_valor, limites)
    if estado_atual == 'critico':
        return None  # ja chegou, nao ha mais o que prever

    alvo = limites.get('limite_atencao') if estado_atual == 'normal' else limites.get('limite_critico')
    if alvo is None:
        return None

    crescimento_por_minuto = (ultimo_valor - primeiro_valor) / minutos_decorridos
    if crescimento_por_minuto <= 0:
        return None  # estavel ou melhorando, nunca vai cruzar nada

    minutos_estimados = math.ceil((alvo - ultimo_valor) / crescimento_por_minuto)
    if minutos_estimados > HORIZONTE_MAXIMO_MINUTOS:
        return None  # longe demais pra ser "risco em acompanhamento"

    proximo_estado = 'atencao' if estado_atual == 'normal' else 'critico'
    return {'proximo_estado': proximo_estado, 'limite': alvo, 'minutos_estimados': minutos_estimados}


# Mesmo esqueleto do AcumuladorTempoNoEstado, só trocando o calculo do fim
class AcumuladorPrevisao:
    def __init__(self):
        self.leituras_por_metrica = {}

    def adicionar(self, metrica, instante, valor, status):
        # nao mudei nada aqui do temponoestado
        if metrica is None or status != 'ok' or valor is None:
            return
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return
        self.leituras_por_metrica.setdefault(metrica, []).append((instante, valor))

    def calcular(self, limites_por_metrica):
        resultado = {}
        for metrica, leituras in self.leituras_por_metrica.items():
            limites = limites_por_metrica.get(metrica)
            if limites is None:
                continue  # metrica sem limite configurado nesse perfil: nada pra prever

            leituras_ordenadas = sorted(leituras, key=lambda par: instante_comparavel(par[0]))
            previsao = calcular_previsao(leituras_ordenadas, limites)
            if previsao is not None:
                resultado[metrica] = previsao
        return resultado


# Mesma ordem de gravidade do "pesos" usado em website/public/scripts/dashboardDados.js
# (maior(a, b) do front), só que sem o nivel "sem-dados": a Gold nunca lida com ele,
# nem como entrada (AcumuladorTempoNoEstado so compara leituras validas) nem como saida
# (ver o comentario em calcular_severidade sobre por que esse nivel fica pra quem le o resumo).
PESOS_SEVERIDADE = {"normal": 0, "atencao": 1, "critico": 2}


def calcular_severidade(resumos, estado_recursos=None):
    """Resume num unico campo, pro servidor inteiro, qual e o estado ATUAL mais grave
    entre as metricas - nao e o pior que ja aconteceu durante o dia, e sim o pior
    "agora" (a ultima leitura de cada metrica). corrigindo uma imprecisao que eu
    tinha deixado aqui antes: tinha escrito "pior situacao do dia", mas o calculo
    sempre olhou so a ultima leitura de cada metrica, nunca o historico inteiro.
    quem quiser saber se o servidor passou por algo pior e ja normalizou precisa
    olhar o campo evolucao, nao esse aqui.

    Parte 1 - a pior metrica manda: percorremos todas as leituras do resumo que
    ganharam um tempo_no_estado (ou seja, tinham limite configurado no perfil - ver
    AcumuladorTempoNoEstado.calcular) e pegamos o estado_atual (da ultima leitura)
    mais grave entre elas.
    Se NENHUMA leitura tiver tempo_no_estado - a API de limites pode ter caido nesse dia,
    ou o perfil simplesmente nao configurou limite pra nenhuma metrica - nao inventamos
    um valor "normal" por omissao: devolvemos None, e quem chamar essa funcao (gold.py)
    deve OMITIR o campo "severidade" do resumo, do mesmo jeito que tempo_no_estado ja
    fica ausente (e nao None) quando nao ha limite pra comparar.

    Parte 2 - OOM e um criterio por fora da Parte 1: ram_oom_kills nao tem
    limite_atencao/limite_critico em nenhum perfil do catalogo (database/script.sql),
    entao ela nunca ganha tempo_no_estado - a Parte 1, sozinha, nunca saberia de um OOM.
    Por isso, se o dia teve pelo menos um OOM kill (resumos.ram.oom.total_observado > 0),
    forcamos severidade = "critico" por essa regra de negocio (um OOM e grave mesmo que
    a ultima leitura de CPU/RAM esteja normal) - mesmo que a Parte 1 nao tenha achado
    nada pra comparar. As tres partes sao independentes: uma nao precisa da outra ter
    dado certo.

    Parte 3 - recursos que a Parte 1 nao enxerga: metricas com leitura por recurso
    (cpu_percent_percpu, uma por nucleo logico) nao ganham tempo_no_estado - entrariam
    misturadas na mesma serie, de nucleos diferentes, e o estado sairia errado (ja
    vimos isso: um nucleo sozinho a 96% virava "normal" porque os outros nucleos,
    mais calmos, entravam na mesma conta - ver AcumuladorTempoNoEstado). O estado por
    recurso de verdade mora em estado_recursos (AcumuladorEstadoRecursos), que ja
    separa cada nucleo/volume/interface - por isso ele tambem concorre aqui pelo pior
    estado, do mesmo jeito que a Parte 1 faz com as metricas sem recurso.

    Por que nunca devolvemos "sem-dados": esse nivel so faz sentido comparando o
    timestamp de uma leitura contra o "agora" de quem le o resumo depois (a regra dos
    15 minutos de recencia, ja decidida no site) - e a Gold roda num momento diferente
    de quando alguem vai olhar esse resumo. Calcular "sem-dados" aqui arriscaria ficar
    velho antes mesmo de alguem ler. Isso espelha como catalog-api-03 fica "silenciosa"
    sem nenhum campo dizendo "sem dados": quem le percebe pelo "fim" (timestamp da
    ultima leitura) estar antigo.
    """
    pior = None

    def _conferir_estado(leitura):
        nonlocal pior
        tempo = leitura.get('tempo_no_estado')
        if tempo is None:
            return
        estado = tempo.get('estado_atual')
        if pior is None or PESOS_SEVERIDADE.get(estado, -1) > PESOS_SEVERIDADE.get(pior, -1):
            pior = estado

    para_cada_medida(resumos, _conferir_estado)

    for recurso in estado_recursos or []:
        estado = recurso.get('estado')
        if pior is None or PESOS_SEVERIDADE.get(estado, -1) > PESOS_SEVERIDADE.get(pior, -1):
            pior = estado

    oom = resumos.get('ram', {}).get('oom', {}).get('total_observado')
    if isinstance(oom, (int, float)) and not isinstance(oom, bool) and oom > 0:
        pior = 'critico'

    return pior


# essa classe é tipo o AcumuladorTempoNoEstado, só que em vez de achar desde
# quando o estado começou eu guardo o pior estado de cada balde de 5 min. usei
# 5 min pq já é o mesmo ciclo que a gold e o java rodam hoje, nem pensei em
# inventar outro número. isso vira a linha do tempo que o gráfico de evolução do
# site vai mostrar, até agora só tinha o estado atual (severidade do dia
# inteiro) e não dava pra saber o que tinha acontecido durante o dia
class AcumuladorEvolucao:
    def __init__(self):
        self.leituras_por_metrica = {}

    def adicionar(self, metrica, instante, valor, status):
        if metrica is None or status != 'ok' or valor is None:
            return
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return
        self.leituras_por_metrica.setdefault(metrica, []).append((instante, valor))

    def calcular(self, limites_por_metrica):
        # um balde só, compartilhado entre todas as métricas, pq preciso do pior
        # estado entre todas elas no mesmo balde, não o pior de cada métrica
        # separada. é basicamente o calcular_severidade, só que por balde de
        # 5 min em vez de uma vez só no fim do dia
        estado_por_balde = {}

        for metrica, leituras in self.leituras_por_metrica.items():
            limites = limites_por_metrica.get(metrica)
            if limites is None:
                continue  # sem limite configurado pra essa metrica nesse perfil, nao da pra classificar

            for instante, valor in leituras:
                balde = instante_utc(instante).floor('5min')
                estado = classificar_estado(valor, limites)

                atual = estado_por_balde.get(balde)
                if atual is None or PESOS_SEVERIDADE[estado] > PESOS_SEVERIDADE[atual]:
                    estado_por_balde[balde] = estado

        return [
            {'instante': balde.isoformat().replace('+00:00', 'Z'), 'estado': estado}
            for balde, estado in sorted(estado_por_balde.items())
        ]


# Estado ATUAL de cada recurso do servidor (núcleo da CPU, volume, dispositivo, interface),
# pra dashboard do Marcelo. É paralelo ao AcumuladorTempoNoEstado, de propósito: aquele agrupa
# por nome de métrica (limitação conhecida nº 2) e aqui a chave é (métrica, recurso), então um
# núcleo em 100% não se perde no meio dos outros. Usa o mesmo calcular_tempo_no_estado e o mesmo
# classificar_estado, então a conta bate com o tempo_no_estado e com a do Java.
#
# Também guarda o horário da última captura de métricas (o "fim" do resumo mistura processos,
# containers e logs) e o horário do último OOM, pra quem lê aplicar a janela que quiser.
#
# Mesma limitação do AcumuladorTempoNoEstado: guarda as leituras do dia em memória, sem teto.
class AcumuladorEstadoRecursos:
    def __init__(self):
        self.series = {}
        self.ultima_captura = None  # (nanossegundos, instante original)
        self.ultimo_oom = None  # (nanossegundos, instante original)
        self.ultimo_texto = None  # guarda o último instante convertido, evita converter igual várias vezes
        self.ultimo_ns = None

    def _nanossegundos(self, instante):
        # Todas as medidas de uma mesma captura têm o mesmo instante, então convertemos uma vez só
        if instante != self.ultimo_texto:
            self.ultimo_ns = instante_comparavel(instante)
            self.ultimo_texto = instante
        return self.ultimo_ns

    def registrar_captura(self, instante):
        ns = self._nanossegundos(instante)
        if self.ultima_captura is None or ns > self.ultima_captura[0]:
            self.ultima_captura = (ns, instante)

    def adicionar(self, categoria, recurso, metrica, instante, valor, status, unidade, tipo):
        # Só leituras válidas entram, igual ao AcumuladorTempoNoEstado.
        if metrica is None or status != 'ok' or valor is None:
            return
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return

        ns = self._nanossegundos(instante)

        # OOM não tem limite no catálogo (ver calcular_severidade): guardamos só o horário do último.
        if metrica == 'ram_oom_kills' and valor > 0:
            if self.ultimo_oom is None or ns > self.ultimo_oom[0]:
                self.ultimo_oom = (ns, instante)

        # Inventário e contadores acumulados não são comparados com limite.
        if tipo not in ('medicao', 'estado'):
            return

        identidade = None
        if recurso is not None:
            identidade = (recurso['tipo'], recurso['identificador'])
        chave = (metrica, identidade)

        serie = self.series.get(chave)
        if serie is None:
            serie = {'categoria': categoria, 'metrica': metrica, 'recurso': recurso,
                     'unidade': unidade, 'leituras': []}
            self.series[chave] = serie
        serie['leituras'].append((ns, instante, valor))

    def ultima_coleta_iso(self):
        if self.ultima_captura is None:
            return None
        return instante_iso_utc(self.ultima_captura[1])

    def ultimo_oom_iso(self):
        if self.ultimo_oom is None:
            return None
        return instante_iso_utc(self.ultimo_oom[1])

    def calcular(self, limites_por_metrica):
        # Sem limites (API fora do ar, por exemplo) não dá pra classificar: devolve lista vazia.
        if limites_por_metrica is None:
            return []

        resultado = []
        for serie in self.series.values():
            limites = limites_por_metrica.get(serie['metrica'])
            if limites is None:
                continue  # métrica sem limite configurado nesse perfil: nada pra comparar

            ordenadas = sorted(serie['leituras'], key=lambda leitura: leitura[0])
            pares = []
            for ns, instante, valor in ordenadas:
                pares.append((instante, valor))

            tempo = calcular_tempo_no_estado(pares, limites)
            if tempo is None:
                continue

            ultimo_instante, ultimo_valor = pares[-1]
            item = {
                'categoria': serie['categoria'],
                'metrica': serie['metrica'],
                'recurso': serie['recurso'],
                'unidade': serie['unidade'],
                'valor_atual': ultimo_valor,
                'instante': instante_iso_utc(ultimo_instante),
                'estado': tempo['estado_atual'],
                'limites': {
                    'operador': limites.get('operador'),
                    'limite_atencao': limites.get('limite_atencao'),
                    'limite_critico': limites.get('limite_critico'),
                    'operador_2': limites.get('operador_2'),
                    'limite_atencao_2': limites.get('limite_atencao_2'),
                    'limite_critico_2': limites.get('limite_critico_2'),
                }
            }
            for campo in ('em_atencao_desde', 'em_critico_desde'):
                if campo in tempo:
                    item[campo] = instante_iso_utc(tempo[campo])
            resultado.append(item)

        resultado.sort(key=_ordem_recurso)
        return resultado


# Ordem estável: categoria, métrica e recurso (núcleo 2 antes do 10, não por texto).
def _ordem_recurso(item):
    recurso = item['recurso']
    tipo = ''
    numero = 0
    texto = ''
    if recurso is not None:
        tipo = recurso['tipo']
        texto = str(recurso['identificador'])
        if isinstance(recurso['identificador'], int):
            numero = recurso['identificador']
    return (item['categoria'], item['metrica'], tipo, numero, texto)


# Só o que precisa de atenção, do mais grave pro menos grave. Entre dois do mesmo nível vem
# primeiro quem está há mais tempo nesse estado. O "desde" é o do nível atual do recurso.
def piores_recursos(recursos_estado):
    piores = []
    for item in recursos_estado:
        if item['estado'] == 'critico':
            desde = item.get('em_critico_desde')
        elif item['estado'] == 'atencao':
            desde = item.get('em_atencao_desde')
        else:
            continue
        entrada = dict(item)
        entrada['desde'] = desde
        piores.append(entrada)

    piores.sort(key=lambda entrada: (-PESOS_SEVERIDADE[entrada['estado']], instante_comparavel(entrada['desde'])))
    return piores


# Estado mais grave de cada categoria (cpu, ram, disco, rede...), incluindo "normal".
# Serve pros cards do servidor: cada recurso mostra a própria cor mesmo sem estar crítico.
def estado_por_categoria(recursos_estado):
    resultado = {}
    for item in recursos_estado:
        atual = resultado.get(item['categoria'])
        if atual is None or PESOS_SEVERIDADE[item['estado']] > PESOS_SEVERIDADE[atual]:
            resultado[item['categoria']] = item['estado']
    return resultado


# Processos têm escopo próprio (persona Amanda) e não entram no CATEGORIAS_SERVIDOR
# do formato_gold.py: misturá-los com a CPU/RAM do host mudaria o contrato do resumo
# usado pela dashboard de servidores. Este acumulador é paralelo ao
# AcumuladorEstadoRecursos, mas agrupa por instância (pid, nome) em vez de recurso
# físico, e guarda só o necessário para os KPIs de pico.
class AcumuladorProcessos:
    def __init__(self):
        self.instancias = {}

    def _leitura_valida(self, leitura):
        # Mesma regra do resumir_leituras: status diferente de "ok" e null não entram.
        # "aquecendo" e "indisponivel" ficam de fora, inclusive com valor preenchido.
        if not isinstance(leitura, dict) or leitura.get('status') != 'ok':
            return None
        valor = leitura.get('valor')
        if valor is None or isinstance(valor, bool):
            return None
        if not isinstance(valor, (int, float)) or not math.isfinite(valor):
            return None
        return valor

    def adicionar(self, registro):
        # O registro já vem organizado por organizar_processo.
        pid = registro.get('pid')
        nome = registro.get('nome')
        if not isinstance(pid, int) or isinstance(pid, bool) or not nome:
            return
        instante = registro.get('timestamp')
        if instante is None:
            return

        chave = (pid, nome)
        instancia = self.instancias.get(chave)
        if instancia is None:
            instancia = {
                'pid': pid, 'nome': nome,
                'leituras_cpu': [], 'leituras_memoria': []
            }
            self.instancias[chave] = instancia

        cpu = registro.get('cpu') or {}
        valor_cpu = self._leitura_valida(cpu.get('uso'))
        if valor_cpu is not None:
            instancia['leituras_cpu'].append(
                (instante_comparavel(instante), instante, valor_cpu)
            )

        memoria = registro.get('memoria') or {}
        valor_memoria = self._leitura_valida(memoria.get('uso_percentual'))
        if valor_memoria is not None:
            instancia['leituras_memoria'].append(
                (instante_comparavel(instante), instante, valor_memoria)
            )

    def _resumir_cpu(self, instancia):
        # Ordenamos pelo instante real, não pela ordem de chegada dos lotes.
        leituras = sorted(instancia['leituras_cpu'], key=lambda leitura: leitura[0])
        if not leituras:
            return None

        pico = leituras[0][2]
        instante_pico = leituras[0][1]
        soma = 0
        acima_100 = 0
        for ns, instante, valor in leituras:
            soma += valor
            # Só troca com valor estritamente maior: em empate fica o primeiro instante.
            if valor > pico:
                pico = valor
                instante_pico = instante
            if valor > 100:
                acima_100 += 1

        return {
            'pid': instancia['pid'],
            'nome': instancia['nome'],
            'pico_cpu_percent': pico,
            'instante_pico': instante_iso_utc(instante_pico),
            'media_cpu_percent': soma / len(leituras),
            'segundos_acima_100': acima_100,
            'amostras_validas': len(leituras),
        }

    def _resumir_memoria(self, instancia):
        # Mesma ordem cronológica da CPU: o "último" não depende da ordem de chegada dos lotes.
        leituras = sorted(instancia['leituras_memoria'], key=lambda leitura: leitura[0])
        if not leituras:
            return None

        pico = leituras[0][2]
        instante_pico = leituras[0][1]
        minimo = leituras[0][2]
        soma = 0
        for ns, instante, valor in leituras:
            soma += valor
            # Em empate, fica o primeiro instante em que o pico foi atingido.
            if valor > pico:
                pico = valor
                instante_pico = instante
            if valor < minimo:
                minimo = valor

        # O coletor já arredonda a leitura em duas casas; a variação segue a mesma precisão.
        ultimo = leituras[-1][2]
        return {
            'pid': instancia['pid'],
            'nome': instancia['nome'],
            'pico_memoria_percent': pico,
            'instante_pico': instante_iso_utc(instante_pico),
            'minimo_memoria_percent': minimo,
            'ultimo_memoria_percent': ultimo,
            # Crescer em relação ao mínimo ajuda a ver retenção sem depender do pico absoluto.
            'variacao_memoria_percent': round(ultimo - minimo, 2),
            'media_memoria_percent': soma / len(leituras),
            'amostras_validas': len(leituras),
        }

    def calcular(self):
        ranking_cpu = []
        ranking_memoria = []
        for instancia in self.instancias.values():
            resumo_cpu = self._resumir_cpu(instancia)
            if resumo_cpu is not None:
                ranking_cpu.append(resumo_cpu)
            resumo_memoria = self._resumir_memoria(instancia)
            if resumo_memoria is not None:
                ranking_memoria.append(resumo_memoria)

        # Maior pico, depois mais tempo saturado, depois menor PID.
        ranking_cpu.sort(key=lambda item: (
            -item['pico_cpu_percent'],
            -item['segundos_acima_100'],
            item['pid'],
        ))

        # Maior pico de RAM, depois maior variação, depois menor PID.
        ranking_memoria.sort(key=lambda item: (
            -item['pico_memoria_percent'],
            -item['variacao_memoria_percent'],
            item['pid'],
        ))

        return {
            'pico_cpu': {
                'destaque': deepcopy(ranking_cpu[0]) if ranking_cpu else None,
                'ranking': [deepcopy(item) for item in ranking_cpu[:5]],
            },
            'pico_memoria': {
                'destaque': deepcopy(ranking_memoria[0]) if ranking_memoria else None,
                'ranking': [deepcopy(item) for item in ranking_memoria[:5]],
            }
        }
