#!/usr/bin/env bash
# Interrompe a instalação se algum comando falhar.
set -e

# Trabalha na pasta deste arquivo, mesmo quando iniciado de outra pasta.
cd -- "$(dirname -- "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 não encontrado. Instale Python 3 e execute novamente."
    exit 1
fi

echo "Criando o ambiente Python do agente..."
if ! python3 -m venv .venv; then
    echo "Não foi possível criar o ambiente. Confira se o suporte a venv está instalado."
    echo "No Ubuntu/Debian, o pacote costuma se chamar python3-venv."
    exit 1
fi

echo "Instalando as bibliotecas. Esta etapa precisa de acesso à internet..."
if ! .venv/bin/python -m pip install -r requirements.txt; then
    echo "A instalação não foi concluída. Confira sua conexão e execute bash instalar.sh novamente."
    exit 1
fi

echo "Instalação concluída. Para começar, execute: bash iniciar.sh"
