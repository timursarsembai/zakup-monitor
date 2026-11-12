#!/usr/bin/env bash
# Сжатие таблиц lots и announcements (VACUUM FULL): после массовых пересчётов в них остаётся
# пустое место — поиск без индекса читает его вместе с данными. Таблица на время сжатия
# заблокирована (~минута на lots), поэтому — только ночью, разово (systemd-run, см. README).
# Сборщики на это время останавливаются. Сайт в эти минуты отвечает медленно или с ошибкой
# на страницах лотов и поиска (запросы ждут блокировку до 15 с) — поэтому только ночью.
# Итог — сообщением владельцу в Telegram.
set -uo pipefail
cd "$(dirname "$0")/.."

# Не одновременно с выкладкой (deploy.sh берёт ту же блокировку): иначе сборщики поднялись
# бы посреди миграции.
exec 8>/tmp/zakup-deploy.lock
if ! flock -w 600 8; then
  echo "Идёт выкладка — сжатие пропущено"; exit 1
fi

psql() { docker compose exec -T postgres psql -U zakup -Atq "$@"; }
size() { psql -c "SELECT pg_size_pretty(pg_total_relation_size('$1'))"; }
db_size() { psql -c "SELECT pg_size_pretty(pg_database_size(current_database()))"; }

# Останавливаем и потом поднимаем только те сборщики, что сейчас работают.
collectors=$(docker compose ps --status running --services 2>/dev/null | grep -xE 'worker|archive|history|specs|securities|scores|skk' | tr '\n' ' ')
before_db=$(db_size); before_lots=$(size lots); before_ann=$(size announcements)
started=$(date +%s)

# VACUUM FULL пишет новую копию таблицы рядом со старой: нужно свободное место не меньше
# двух размеров lots с индексами.
need=$(psql -c "SELECT pg_total_relation_size('lots') * 2 / 1024")
free=$(df -Pk /var/lib/docker | awk 'NR==2 {print $4}')
if [ "${free:-0}" -lt "${need:-0}" ]; then
  echo "Мало места на диске для сжатия: свободно ${free} КБ, нужно ${need} КБ"; exit 1
fi

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
# Вывод — в журнал службы (journalctl --user -u zakup-compact): видно, если сообщение не ушло.
docker compose run --rm --no-deps -e COMPACT_TEXT="$text" --entrypoint python worker \
  -c "import os; from zakup import notify; print('telegram:', notify.send(os.environ['COMPACT_TEXT']))" 2>&1 \
  | grep -v "^ Container"
