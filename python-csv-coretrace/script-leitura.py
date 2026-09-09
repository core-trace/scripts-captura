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

while True:
    pico_cpu = -1.0
    registro_pico = None

    cpu_percents = []
    ram_percents = []
    disk_percents = []
    cpu_freqs = []
    disk_frees = []
    disk_totals = []
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

        media_cpu = calcular_media(cpu_percents)
        media_ram = calcular_media(ram_percents)
        media_disco = calcular_media(disk_percents)
        media_freq_cpu = calcular_media(cpu_freqs)

        # bytes pra GB
        gb = 1024 ** 3
        media_disco_livre_gb = calcular_media(disk_frees) / gb
        media_disco_total_gb = calcular_media(disk_totals) / gb

        # exibir
        print("=" * 45)
        print("      MÉTRICAS DO SISTEMA       ")
        print("=" * 45)

        print(f"User: {usuario}")

        print("-" * 45)

        # .1f e .2f que poe limite nas casas decimas (q nem java)
        print(f"RAM Média          : {media_ram:.1f}%")
        print(f"CPU Média          : {media_cpu:.1f}%")
        print(f"Frequência CPU     : {media_freq_cpu:.0f} MHz")
        print(f"Uso do Disco       : {media_disco:.1f}%")
        print(f"Espaço Livre Disco : {media_disco_livre_gb:.2f} GB / {media_disco_total_gb:.2f} GB")
        if registro_pico:
            print(f"Pico CPU ({inicio_str} até {fim_str}): {pico_cpu:.1f}% às {registro_pico['Date']}")
        else:
            print("Nenhum pico encontrado no período informado.")

        print("=" * 45)
    
    except FileNotFoundError:
        print("Arquivo dados.csv não encontrado. Aguardando...")
        
    print("Atualizando dados em 5 segundos... (Pressione Ctrl+C para encerrar)")
    time.sleep(5)