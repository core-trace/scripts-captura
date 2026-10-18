#!/usr/bin/env bash
set -e

# Usa o ambiente que foi criado dentro da própria pasta do agente.
cd -- "$(dirname -- "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Instale o agente primeiro: bash instalar.sh"
    exit 1
fi

if ! .venv/bin/python -c "import psutil, requests"; then
    echo "As bibliotecas não estão prontas. Execute: bash instalar.sh"
    exit 1
fi

if [ ! -f config.json ]; then
    echo "O config.json não foi encontrado. Extraia novamente o pacote baixado da plataforma."
    exit 1
fi

echo "Iniciando o CoreTrace. Para encerrar, pressione Ctrl+C."
exec .venv/bin/python main.py
