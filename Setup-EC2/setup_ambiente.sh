#!/bin/bash

# Script de Inicialização de Ambiente - APM iFood (Simplificado)
set -e

export DEBIAN_FRONTEND=noninteractive
UBUNTU_DIR="/home/ubuntu"

echo "=================================================="
echo " 1. Atualizando sistema e instalando runtimes"
echo "=================================================="
sudo -E apt update && sudo -E apt upgrade -y
sudo -E apt install -y python3 python3-pip python3-venv default-jdk default-jre

echo "=================================================="
echo " 2. Criando Contas de Serviço Isoladas (Sem Login)"
echo "=================================================="
if ! id "userlogsjava" &>/dev/null; then
    sudo useradd -r -s /bin/false userlogsjava
fi

if ! id "usercapturapython" &>/dev/null; then
    sudo useradd -r -s /bin/false usercapturapython
fi

echo "=================================================="
echo " 3. Ajustando Permissões em /home/ubuntu/"
echo "=================================================="
# Garante que os usuários de serviço possam ler, executar e criar os arquivos .csv e .log na home
sudo chmod 777 $UBUNTU_DIR

echo "=================================================="
echo " Ambiente configurado com sucesso na /home/ubuntu!"
echo "=================================================="