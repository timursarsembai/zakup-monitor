#!/usr/bin/env bash
# Сжатие таблиц lots и announcements (VACUUM FULL): после массовых пересчётов в них остаётся
# пустое место — поиск без индекса читает его вместе с данными. Таблица на время сжатия
# заблокирована (~минута на lots), поэтому — только ночью, разово (systemd-run, см. README).
# Сборщики на это время останавливаются; сайт отвечает, поиск ждёт окончания.
# Итог — сообщением владельцу в Telegram.
set -uo pipefail
cd "$(dirname "$0")/.."

psql() { docker compose exec -T postgres psql -U zakup -Atq "$@"; }
size() { psql -c "SELECT pg_size_pretty(pg_total_relation_size('$1'))"; }
db_size() { psql -c "SELECT pg_size_pretty(pg_database_size(current_database()))"; }

collectors="worker archive history specs"
before_db=$(db_size); before_lots=$(size lots); before_ann=$(size announcements)
started=$(date +%s)

docker compose stop $collectors >/dev/null 2>&1
# Сборщики поднимаются в любом случае, даже если сжатие упало.
trap 'docker compose start $collectors >/dev/null 2>&1' EXIT

result="готово"
for table in lots announcements; do
  # Блокировку ждём не дольше минуты: если что-то держит таблицу — пропускаем, не вешаем сайт.
  if ! psql -c "SET lock_timeout = '60s'" -c "VACUUM (FULL, ANALYZE) $table" 2>/tmp/zakup-compact.err; then
    result="ошибка на $table: $(head -c 300 /tmp/zakup-compact.err)"
    break
  fi
done

docker compose start $collectors >/dev/null 2>&1
trap - EXIT
seconds=$(( $(date +%s) - started ))
text="Сжатие таблиц базы — ${result} за ${seconds} с.
Лоты: ${before_lots} → $(size lots)
Объявления: ${before_ann} → $(size announcements)
Вся база: ${before_db} → $(db_size)"
echo "$text"
docker compose run --rm --no-deps -e COMPACT_TEXT="$text" --entrypoint python worker \
  -c "import os; from zakup import notify; notify.send(os.environ['COMPACT_TEXT'])" >/dev/null 2>&1
