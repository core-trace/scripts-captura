import csv
import psutil
import datetime

usuario = "user"
    
def criar_csv():
    with open('dados.csv', 'w', newline='') as csvfile:
        csv.writer(csvfile).writerow(['usuario', 'Date', 'CPU (percent)', 'CPU (frequency)', 'CPU (threads)', 'RAM (percent)', 'RAM (used)', 'RAM (total)', 'Disk (percent)', 'Disk (total)', 'Disk (free)', 'Disk (read)', 'Disk (write)'])
    getStats()

def getStats():
    while True:

        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            
            cpu_frequency = psutil.cpu_freq().current
            
            cpu_threads = psutil.cpu_count()
            
            agora = datetime.datetime.now()
            
            timestamp = agora.strftime("%Y-%m-%d %H:%M:%S")

            ram = psutil.virtual_memory()   

            disk_usage = psutil.disk_usage('/')
            
            disk_rw = psutil.disk_io_counters()
            
            with open('dados.csv', 'a') as csvfile:
                csv.writer(csvfile).writerow([usuario, timestamp, cpu_percent, cpu_frequency, cpu_threads, ram.percent, ram.used, ram.total, disk_usage.percent, disk_usage.total, disk_usage.free, disk_rw.read_bytes, disk_rw.write_bytes])

        except Exception as e:
            print(f"Erro ao coletar dados: {e}")
    
    #print(timestamp,",",cpu,",",ram,",",disco.percent)

try:
    with open('dados.csv', 'r') as csvfile:
        amostra = csvfile.read(1024)
        
        if not amostra:
            criar_csv()
        else:
            tem_cabecalho = csv.Sniffer().has_header(amostra)
 
            if tem_cabecalho:
                getStats()
            else:
                criar_csv()
        
except FileNotFoundError:
    criar_csv()

    
    
    