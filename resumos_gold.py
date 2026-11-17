"""Junta os resumos aos poucos, sem guardar todas as capturas do dia na memória."""
import pandas as pd

from copy import deepcopy
from formato_gold import identidade_recurso, para_cada_medida

def instante_comparavel(instante):

    # Converte o texto em um Timestamp do Pandas.
    # Isso também preserva a precisão de nanossegundos.
    horario = pd.Timestamp(instante)

    if pd.isna(horario):
        raise ValueError("Timestamp invalido")

    # Capturas tradicionais usam o horário local de São Paulo.
    # Capturas Docker já possuem o fuso UTC informado.
    if horario.tzinfo is None:
        horario = horario.tz_localize("America/Sao_Paulo")

    # Converte para UTC e retorna o valor em nanossegundos.
    # Assim todos os horários podem ser comparados igualmente.
    return horario.tz_convert("UTC").value

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


# Mesma ordem de gravidade do "pesos" usado em website/public/scripts/dashboardDados.js
# (maior(a, b) do front), só que sem o nivel "sem-dados": a Gold nunca lida com ele,
# nem como entrada (AcumuladorTempoNoEstado so compara leituras validas) nem como saida
# (ver o comentario em calcular_severidade sobre por que esse nivel fica pra quem le o resumo).
PESOS_SEVERIDADE = {"normal": 0, "atencao": 1, "critico": 2}


def calcular_severidade(resumos):
    """Resume num unico campo, pro servidor inteiro, qual foi a pior situacao do dia.

    Parte 1 - a pior metrica manda: percorremos todas as leituras do resumo que
    ganharam um tempo_no_estado (ou seja, tinham limite configurado no perfil - ver
    AcumuladorTempoNoEstado.calcular) e pegamos o estado_atual mais grave entre elas.
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
    nada pra comparar. As duas partes sao independentes: uma nao precisa da outra ter
    dado certo.

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

    oom = resumos.get('ram', {}).get('oom', {}).get('total_observado')
    if isinstance(oom, (int, float)) and not isinstance(oom, bool) and oom > 0:
        pior = 'critico'

    return pior
