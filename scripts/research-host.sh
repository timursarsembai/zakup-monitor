#!/usr/bin/env bash
# Запускает `zakup research --providers claude_code` на хосте: база — через
# опубликованный на 127.0.0.1 порт Postgres, пароль — из .env проекта.
set -euo pipefail
cd "$(dirname "$0")/.."
password=$(grep -E '^APP_DB_PASSWORD=' .env | cut -d= -f2- | tr -d '"')
export DATABASE_URL="postgresql://zakup_app:${password}@127.0.0.1:5437/zakup"
export PGTZ=Asia/Almaty
# Ключ расшифровки настроек (прокси для Kaspi и т.п.).
ZAKUP_SECRET_KEY=$(grep -E '^ZAKUP_SECRET_KEY=' .env | cut -d= -f2- | tr -d '"')
export ZAKUP_SECRET_KEY
export PATH="$HOME/.local/bin:$PATH"
exec .venv-host/bin/zakup research --providers claude_code
