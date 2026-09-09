from os import path
import csv
from datetime import datetime
import time

inicio_str = "2026-08-20 11:47:20"
fim_str = "2026-08-20 11:47:30"

formato_data = "%Y-%m-%d %H:%M:%S"
data_inicio = datetime.strptime(inicio_str, formato_data)
data_fim = datetime.strptime(fim_str, formato_data)

# funcao de media pra economizar calculo
def calcular_media(lista):
    return sum(lista) / len(lista) if lista else 0.0

def calcular_desvio_padrao(lista):
    if not lista:
        return 0.0
    media = calcular_media(lista)
    soma_quadrados = 0.0
    for valor in lista:
        soma_quadrados += (valor - media) ** 2
    return (soma_quadrados / len(lista)) ** 0.5

while True:
    pico_cpu = -1.0
    registro_pico = None

    cpu_percents = []
    ram_percents = []
    disk_percents = []
    cpu_freqs = []
    disk_frees = []
    disk_totals = []
    ram_useds = []
    ram_totals = []
    disk_reads = []
    disk_writes = []
    cpu_threads = None
    usuario = None

    try:
        with open('dados.csv', mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            
            for row in reader:
                
                if not usuario and 'usuario' in row and row['usuario']:
                    usuario = row['usuario'].strip()
                    
                if row.get('Date'):
                    try:
                        data_registro = datetime.strptime(row['Date'].strip(), formato_data)
                    except ValueError:
                        continue

                    if data_inicio <= data_registro <= data_fim and row.get('CPU (percent)'):
                        cpu_atual = float(row['CPU (percent)'])
                        if cpu_atual > pico_cpu:
                            pico_cpu = cpu_atual
                            registro_pico = row
                
                if row.get('CPU (percent)'):
                    cpu_percents.append(float(row['CPU (percent)']))
                if row.get('RAM (percent)'):
                    ram_percents.append(float(row['RAM (percent)']))
                if row.get('Disk (percent)'):
                    disk_percents.append(float(row['Disk (percent)']))
                if row.get('CPU (frequency)'):
                    cpu_freqs.append(float(row['CPU (frequency)']))
                if row.get('Disk (free)'):
                    disk_frees.append(float(row['Disk (free)']))
                if row.get('Disk (total)'):
                    disk_totals.append(float(row['Disk (total)']))
                if row.get('CPU (threads)'):
                    cpu_threads = row['CPU (threads)']
                if row.get('RAM (used)'):
                    ram_useds.append(float(row['RAM (used)']))
                if row.get('RAM (total)'):
                    ram_totals.append(float(row['RAM (total)']))
                if row.get('Disk (read)'):
                    disk_reads.append(float(row['Disk (read)']))
                if row.get('Disk (write)'):
                    disk_writes.append(float(row['Disk (write)']))

        media_cpu = calcular_media(cpu_percents)
        media_ram = calcular_media(ram_percents)
        media_disco = calcular_media(disk_percents)
        media_freq_cpu = calcular_media(cpu_freqs)

        # bytes pra GB
        gb = 1024 ** 3
        media_disco_livre_gb = calcular_media(disk_frees) / gb
        media_disco_total_gb = calcular_media(disk_totals) / gb

        media_ram_usada_gb = calcular_media(ram_useds) / gb
        media_ram_total_gb = calcular_media(ram_totals) / gb

        media_disco_leitura_gb = calcular_media(disk_reads) / gb
        media_disco_escrita_gb = calcular_media(disk_writes) / gb

        desvio_cpu = calcular_desvio_padrao(cpu_percents)
        desvio_ram = calcular_desvio_padrao(ram_percents)
        desvio_disco = calcular_desvio_padrao(disk_percents)

        # exibir
        print("=" * 45)
        print("      MÉTRICAS DO SISTEMA       ")
        print("=" * 45)

        print(f"User: {usuario}")

        print("-" * 45)

        # .1f e .2f que poe limite nas casas decimas (q nem java)
        print(f"RAM Média          : {media_ram:.1f}%")
        print(f"RAM Desvio Padrão  : {desvio_ram:.1f}")
        print(f"Espaço RAM         : {media_ram_usada_gb:.2f} GB / {media_ram_total_gb:.2f} GB")
        print(f"CPU Média          : {media_cpu:.1f}%")
        print(f"CPU Desvio Padrão  : {desvio_cpu:.1f}")
        print(f"Frequência CPU     : {media_freq_cpu:.0f} MHz")
        print(f"Threads CPU        : {cpu_threads}")
        print(f"Uso do Disco       : {media_disco:.1f}%")
        print(f"Disco Desvio Padrão: {desvio_disco:.1f}")
        print(f"Espaço Livre Disco : {media_disco_livre_gb:.2f} GB / {media_disco_total_gb:.2f} GB")
        print(f"Leitura Disco      : {media_disco_leitura_gb:.2f} GB")
        print(f"Escrita Disco      : {media_disco_escrita_gb:.2f} GB")
        if registro_pico:
            print(f"Pico CPU ({inicio_str} até {fim_str}): {pico_cpu:.1f}% às {registro_pico['Date']}")
        else:
            print("Nenhum pico encontrado no período informado.")

        print("=" * 45)
    
    except FileNotFoundError:
        print("Arquivo dados.csv não encontrado. Aguardando...")
        
    print("Atualizando dados em 5 segundos... (Pressione Ctrl+C para encerrar)")
    time.sleep(5)