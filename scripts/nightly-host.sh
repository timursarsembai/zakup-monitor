#!/usr/bin/env bash
# Запускает `zakup nightly` на хосте: ночной ревьюер, отчёт владельцу за сутки.
# База — через опубликованный на 127.0.0.1 порт Postgres; токен бота — для отчётов.
set -euo pipefail
cd "$(dirname "$0")/.."
value() { grep -E "^$1=" .env | cut -d= -f2- | tr -d '"'; }
export DATABASE_URL="postgresql://zakup_app:$(value APP_DB_PASSWORD)@127.0.0.1:5437/zakup"
export PGTZ=Asia/Almaty
ZAKUP_TELEGRAM_BOT_TOKEN=$(value ZAKUP_TELEGRAM_BOT_TOKEN)
ZAKUP_TELEGRAM_CHAT_ID=$(value ZAKUP_TELEGRAM_CHAT_ID)
export ZAKUP_TELEGRAM_BOT_TOKEN ZAKUP_TELEGRAM_CHAT_ID
export PATH="$HOME/.local/bin:$PATH"
exec .venv-host/bin/zakup nightly
