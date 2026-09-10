==================================================
 PASSO A PASSO: PREP & DEMONSTRAÇÃO DO AMBIENTE
==================================================

1. Envie os arquivos (.jar, .py e script .sh) diretamente para a home da EC2:
scp -i "caminho_chave.pem" seu_arquivo.jar seu_script.py setup_ambiente.sh ubuntu@"ippublico":/home/ubuntu

2. Acesse a instância via SSH:
ssh -i "caminho_chave.pem" ubuntu@"ippublico"

3. Dê permissão e execute o script de setup:
chmod +x setup_ambiente.sh
./setup_ambiente.sh

--------------------------------------------------
 COMANDOS PARA A APRESENTAÇÃO (DEMO AO VIVO)
--------------------------------------------------

--- DEMO 1: PYTHON (Captura de Hardware) ---
# Executa o Python na /home/ubuntu com a conta de serviço do Python:
sudo -u usercapturapython python3 /home/ubuntu/seu_script.py

# Pressione Ctrl + C após alguns segundos

# Mostra o CSV gerado na própria home:
cat /home/ubuntu/metricas_hardware.csv


--- DEMO 2: JAVA (Alertas e Logs) ---
# Executa o Java na /home/ubuntu com a conta de serviço do Java:
sudo -u userlogsjava java -jar /home/ubuntu/seu_arquivo.jar

# Pressione Ctrl + C após alguns segundos

# Mostra o log de alertas gerado na própria home:
cat /home/ubuntu/apm_alertas.log