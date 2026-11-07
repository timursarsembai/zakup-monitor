#!/usr/bin/env bash
# Запускает `zakup research --providers claude_code` на хосте: база — через
# опубликованный на 127.0.0.1 порт Postgres, пароль — из .env проекта.
set -euo pipefail
cd "$(dirname "$0")/.."
password=$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | tr -d '"')
export DATABASE_URL="postgresql://zakup:${password}@127.0.0.1:5437/zakup"
export PGTZ=Asia/Almaty
export PATH="$HOME/.local/bin:$PATH"
exec .venv-host/bin/zakup research --providers claude_code
