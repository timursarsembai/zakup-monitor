#!/usr/bin/env bash
# Тесты в контейнере на отдельной базе zakup_test. Вывод — в файл (по умолчанию
# /tmp/zakup-pytest.txt); код выхода — ненулевой, если хоть один тест упал.
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:-/tmp/zakup-pytest.txt}
password=$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | tr -d '"')
docker compose build -q worker
set +e
docker compose run --rm --no-deps \
  -e TEST_DATABASE_URL="postgresql://zakup:${password}@postgres:5432/zakup_test" \
  -e DATABASE_URL="postgresql://zakup:${password}@postgres:5432/zakup_test" \
  -e ZAKUP_TELEGRAM_BOT_TOKEN= -e ZAKUP_CLIENT_BOT_TOKEN= -e ZAKUP_TELEGRAM_CHAT_ID= \
  -e KAZPAYMENT_API_KEY= -e KAZPAYMENT_WEBHOOK_SECRET= -e GOSZAKUP_TOKEN= \
  --entrypoint pytest worker -q -p no:cacheprovider > "$out" 2>&1
code=$?
set -e
tail -3 "$out"
if [ $code -ne 0 ] || grep -qiE '[0-9]+ (failed|errors?)\b' "$out"; then
  echo "ТЕСТЫ НЕ ПРОШЛИ (подробности в $out)"
  exit 1
fi
