import psutil
import csv
import socket
from datetime import datetime

caminho_arquivo = f'./processos_{socket.gethostname()}.csv'
with open(caminho_arquivo, 'w', newline='') as arquivo_csv:
    writer = csv.writer(arquivo_csv)
    writer.writerow(['Data e Hora', 'PID', 'Nome', 'Usuário'])

    for p in psutil.process_iter(['pid', 'name', 'username']):
        writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), p.info['pid'], p.info['name'], p.info['username']])
