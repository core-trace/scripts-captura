import csv
import psutil
import datetime

usuario = "Nicole"
    
def criar_csv():
    with open('./dados.csv', 'w') as csvfile:
        csv.writer(csvfile).writerow(['usuario', 'Date', 'CPU (percent)', 'CPU (frequency)','RAM (percent)', 'Disk (percent)', 'Disk (total)', 'Disk (free)', 'Disk (read)', 'Disk (write)'])
    getStats()

def getStats():
    while True:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        
        cpu_frequency = psutil.cpu_freq().current
        
        agora = datetime.datetime.now()
        
        timestamp = agora.strftime("%Y-%m-%d %H:%M:%S")

        ram = psutil.virtual_memory()

        disk_usage = psutil.disk_usage('/')
        
        disk_rw = psutil.disk_io_counters()
        
        with open('./dados.csv', 'a') as csvfile:
            csv.writer(csvfile,delimiter=',').writerow([usuario, timestamp, cpu_percent, cpu_frequency, ram.percent, disk_usage.percent, disk_usage.total, disk_usage.free, disk_rw.read_bytes, disk_rw.write_bytes])

    
    #print(timestamp,",",cpu,",",ram,",",disco.percent)

try:
    with open('./dados.csv', 'r') as csvfile:
        primeira_linha = csvfile.readline().strip()

    if primeira_linha:
        getStats()
    else:
        criar_csv()
        
except FileNotFoundError:
    criar_csv()
