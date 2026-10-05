#!/bin/bash

# para rodar o configuração.sh
# primeiro, adicione esse shell ao nano configuração.sh
# depois, fora do nano, adicione:
# chmod +x configuracao.sh
# e por fim:
# sudo ./configuracao.sh

# Verificar se está sendo executado como root
if [ "$EUID" -ne 0 ]; then
    echo "Execute este script com sudo."
    exit 1
fi


# ============================================================
# ATUALIZAR O SISTEMA
# ============================================================

echo "Atualizando o sistema..."

apt update
apt upgrade -y


# ============================================================
# CRIAR GRUPO
# ============================================================

echo "Criando grupo devs..."

groupadd devs


# ============================================================
# CRIAR USUÁRIOS
# ============================================================

echo "Criando usuários..."

useradd -m -s /bin/bash livia
useradd -m -s /bin/bash raphael
useradd -m -s /bin/bash marcelly
useradd -m -s /bin/bash vitor
useradd -m -s /bin/bash carlos
useradd -m -s /bin/bash matheus


# ============================================================
# GARANTIR AS HOMES E O SHELL DOS USUÁRIOS
# ============================================================

mkdir -p /home/livia
mkdir -p /home/raphael
mkdir -p /home/marcelly
mkdir -p /home/vitor
mkdir -p /home/carlos
mkdir -p /home/matheus

chown livia:devs /home/livia
chown raphael:devs /home/raphael
chown marcelly:devs /home/marcelly
chown vitor:devs /home/vitor
chown carlos:devs /home/carlos
chown matheus:devs /home/matheus

chmod 755 /home/livia
chmod 755 /home/raphael
chmod 755 /home/marcelly
chmod 755 /home/vitor
chmod 755 /home/carlos
chmod 755 /home/matheus


# ============================================================
# DEFINIR SENHAS
# ============================================================

echo "Definindo senhas..."

echo "livia:coretrace123" | chpasswd
echo "raphael:coretrace123" | chpasswd
echo "marcelly:coretrace123" | chpasswd
echo "vitor:coretrace123" | chpasswd
echo "carlos:coretrace123" | chpasswd
echo "matheus:coretrace123" | chpasswd


# ============================================================
# ADICIONAR USUÁRIOS AO GRUPO DEVS
# ============================================================

echo "Adicionando usuários ao grupo devs..."

usermod -aG devs livia
usermod -aG devs raphael
usermod -aG devs marcelly
usermod -aG devs vitor
usermod -aG devs carlos
usermod -aG devs matheus


# Dar sudo ao Raphael
usermod -aG sudo raphael


# ============================================================
# CONFIGURAR DIRETÓRIO DO PROJETO
# ============================================================

echo "Configurando diretório do projeto..."

mkdir -p /opt/site-institucional

chown -R root:devs /opt/site-institucional
chmod 770 /opt/site-institucional


# ============================================================
# SFTP
# ============================================================

echo "Configurando SFTP..."

# Criar diretórios do SFTP
mkdir -p /srv/urubu
mkdir -p /srv/urubu/data

# Configurar permissões do Chroot
chown root:root /srv/urubu
chmod 755 /srv/urubu

# Criar usuário SFTP
useradd -m -s /bin/false urubu_sftp

# Dar permissão de escrita na pasta data
chown -R urubu_sftp:urubu_sftp /srv/urubu/data


# ============================================================
# CHAVE PÚBLICA DO SFTP
# ============================================================

echo "Configurando chave pública do SFTP..."

# Criar local para a chave pública
mkdir -p /etc/ssh/keys/urubu_sftp

SFTP_PUBLIC_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII6Qtap1lsc1kY/B6RJQFZ8F1U/8WA3Dva0wotKzOF79 urubu-sftp-key'

echo "$SFTP_PUBLIC_KEY" > /etc/ssh/keys/urubu_sftp/authorized_keys

# Configurar permissões da chave
chown -R urubu_sftp:urubu_sftp /etc/ssh/keys/urubu_sftp

chmod 755 /etc/ssh/keys
chmod 700 /etc/ssh/keys/urubu_sftp
chmod 600 /etc/ssh/keys/urubu_sftp/authorized_keys


# ============================================================
# CONFIGURAÇÃO DO SSH / TUNELAMENTO
# ============================================================

echo "Configurando SSH e tunelamento..."

cat >> /etc/ssh/sshd_config <<'EOF'

# Permitir tunelamento SSH para os usuários da equipe
Match User livia,raphael,marcelly,vitor,carlos,matheus
    AllowTcpForwarding yes
    PasswordAuthentication yes

# Configuração do SSH para o usuário SFTP
Match User urubu_sftp
    ForceCommand internal-sftp
    ChrootDirectory /srv/urubu
    PermitTTY no
    X11Forwarding no
    AllowTcpForwarding no
    AuthorizedKeysFile /etc/ssh/keys/urubu_sftp/authorized_keys
    PasswordAuthentication no
    PubkeyAuthentication yes
EOF


# Verificar configuração do SSH
sshd -t

# Reiniciar SSH
systemctl restart ssh


# ============================================================
# INSTALAR PYTHON
# ============================================================

echo "Instalando Python..."

apt install -y python3 python3-pip python3-venv


# ============================================================
# INSTALAR JAVA 21
# ============================================================

echo "Instalando Java 21..."

apt install -y openjdk-21-jdk


# ============================================================
# INSTALAR DOCKER
# ============================================================

echo "Instalando Docker..."

apt install -y docker.io

# Iniciar Docker automaticamente com o sistema
systemctl enable docker
systemctl start docker


# Adicionar usuários da equipe ao grupo docker
echo "Adicionando usuários ao grupo docker..."

usermod -aG docker livia
usermod -aG docker raphael
usermod -aG docker marcelly
usermod -aG docker vitor
usermod -aG docker carlos
usermod -aG docker matheus


# ============================================================
# INSTALAR MYSQL
# ============================================================

echo "Instalando MySQL..."

apt install -y mysql-server

# Iniciar MySQL automaticamente com o sistema
systemctl enable mysql
systemctl start mysql


# ============================================================
# VERIFICAR INSTALAÇÕES
# ============================================================

echo ""
echo "=========================================="
echo "       VERIFICANDO O AMBIENTE"
echo "=========================================="

echo ""
echo "Python:"
python3 --version

echo ""
echo "Java:"
java -version

echo ""
echo "Docker:"
docker --version

echo ""
echo "MySQL:"
mysql --version

echo ""
echo "=========================================="
echo "       CONFIGURAÇÃO CONCLUÍDA"
echo "=========================================="
echo ""
echo "A EC2 foi configurada com:"
echo "- Usuários e grupo devs"
echo "- Permissões do projeto"
echo "- SFTP"
echo "- SSH e tunelamento"
echo "- Python"
echo "- Java 21"
echo "- Docker"
echo "- MySQL"
echo ""
