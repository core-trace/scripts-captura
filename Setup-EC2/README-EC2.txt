primeiro envie os arquivos para home da EC2
scp -i "caminh chave pem" "caminho jar ou python" ubuntu@"ippublico":/home/ubuntu
ippublico = curl icanhazip.com
apo baixar ambos, envie tambem da mesma forma o arquivo shell script
escreva :
chmod +x setup_ambiente.sh -- permissao para rodar
./setup_ambiente.sh -- roda


teste com usuarios pra ver se tem permissao para rodar na home/ubuntu/

sudo su - userlogsjava
sudo java -jar /home/ubuntu/seu_arquivo.jar
-------------------------------------------------
sudo su - usercapturapython
sudo python3 /home/ubuntu/seu_script.py
