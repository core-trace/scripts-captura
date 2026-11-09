"""Junta os resumos aos poucos, sem guardar todas as capturas do dia na memória."""
from copy import deepcopy
from formato_gold import identidade_recurso


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

    mais_recente = instante >= estrutura['instante']

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
                estrutura['valor'] = min(horarios)
            else:
                estrutura['valor'] = None
        elif nome in ('fim', 'ultima_observacao'):
            horarios = []
            for horario in (estrutura.get('valor'), valor):
                if horario is not None:
                    horarios.append(horario)
            if horarios:
                estrutura['valor'] = max(horarios)
            else:
                estrutura['valor'] = None
        elif nome == 'amostras_observadas':
            estrutura['valor'] = estrutura.get('valor', 0) + valor
        elif mais_recente or 'valor' not in estrutura:
            estrutura['valor'] = valor

    estrutura['instante'] = max(estrutura['instante'], instante)
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
