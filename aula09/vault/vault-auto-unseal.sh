#!/usr/bin/env bash
set -e

# ENDEREÇO DO VAULT (mesmo usado na aula)
export VAULT_ADDR="https://devops-management:8200"
export VAULT_SKIP_VERIFY=true

# Tentativa automática de localizar o arquivo vault.init
INIT_FILE=""

# 1) Procura em /home/*/vault.init (qualquer usuário real)
for f in /home/*/vault.init; do
  if [ -f "$f" ]; then
    INIT_FILE="$f"
    break
  fi
done

# 2) Se não encontrou em /home, tenta /root/vault.init (caso tenha sido salvo como root)
if [ -z "$INIT_FILE" ] && [ -f "/root/vault.init" ]; then
  INIT_FILE="/root/vault.init"
fi

if [ -z "$INIT_FILE" ]; then
  echo "Arquivo vault.init não encontrado em /home/* nem em /root."
  echo "Certifique-se de que o comando 'vault operator init' foi executado e o output salvo em ~/vault.init."
  exit 1
fi

echo "Usando arquivo de unseal: $INIT_FILE"

UNSEAL_KEY=$(grep 'Unseal Key 1' "$INIT_FILE" | awk '{print $4}')

if [ -z "$UNSEAL_KEY" ]; then
  echo "Não foi possível extrair a Unseal Key 1 de $INIT_FILE."
  exit 1
fi

echo "Executando vault operator unseal..."
/usr/bin/vault operator unseal "$UNSEAL_KEY"

