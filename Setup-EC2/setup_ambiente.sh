#!/bin/bash

# Script de Inicialização de Ambiente (Python & Java)
set -e

# Configura instalação não-interativa para evitar travamentos no APT
export DEBIAN_FRONTEND=noninteractive

PASSWORD="123coretrace"
UBUNTU_DIR="/home/ubuntu"

echo "=================================================="
echo " 1. Atualizando sistema e instalando runtime Python e Java"
echo "=================================================="
sudo -E apt update && sudo -E apt upgrade -y
sudo -E apt install -y python3 python3-pip python3-venv default-jdk default-jre sudo

# Mapeamento do caminho real dos binários
JAVA_BIN=$(readlink -f /usr/bin/java)
PYTHON_BIN=$(readlink -f /usr/bin/python3)
JAVA_HOME_DIR=$(readlink -f /usr/bin/java | sed "s:/bin/java::")

echo "=================================================="
echo " 2. Configurando o usuário 'userlogsjava'"
echo "=================================================="
if ! id "userlogsjava" &>/dev/null; then
    sudo useradd -m -s /bin/bash userlogsjava
fi
echo "userlogsjava:$PASSWORD" | sudo chpasswd

# Configura JAVA_HOME e PATH para o userlogsjava
echo "export JAVA_HOME=$JAVA_HOME_DIR" | sudo tee -a /home/userlogsjava/.bashrc > /dev/null
echo "export PATH=\$PATH:\$JAVA_HOME/bin" | sudo tee -a /home/userlogsjava/.bashrc > /dev/null

echo "=================================================="
echo " 3. Configurando o usuário 'usercapturapython'"
echo "=================================================="
if ! id "usercapturapython" &>/dev/null; then
    sudo useradd -m -s /bin/bash usercapturapython
fi
echo "usercapturapython:$PASSWORD" | sudo chpasswd

echo "=================================================="
echo " 4. Configurando regras restritas de Sudoers"
echo "=================================================="

# Regra para userlogsjava (apenas Java)
SUDO_JAVA="/etc/sudoers.d/userlogsjava"
echo "userlogsjava ALL=(ALL) NOPASSWD: /usr/bin/java, $JAVA_BIN" | sudo tee $SUDO_JAVA > /dev/null
sudo chmod 0440 $SUDO_JAVA

# Regra para usercapturapython (apenas Python)
SUDO_PYTHON="/etc/sudoers.d/usercapturapython"
echo "usercapturapython ALL=(ALL) NOPASSWD: /usr/bin/python3, $PYTHON_BIN" | sudo tee $SUDO_PYTHON > /dev/null
sudo chmod 0440 $SUDO_PYTHON

echo "=================================================="
echo " 5. Configurando permissões de acesso em /home/ubuntu/"
echo "=================================================="

# Libera permissão de leitura e execução em /home/ubuntu para que
# os novos usuários consigam ler/executar os arquivos lá dentro
sudo chmod 755 $UBUNTU_DIR

echo "=================================================="
echo " Ambiente configurado e pronto para uso!"
echo "=================================================="