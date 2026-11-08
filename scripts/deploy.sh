#!/usr/bin/env bash
# Выкладка: собрать образ, при изменённой схеме базы применить её при остановленных
# сборщиках, поднять все службы, перезапустить ИИ-службу на хосте.
#
# Почему так: изменение таблиц (ALTER) ждёт, пока все закроют транзакции, а сборщики
# держат их открытыми, пока ждут портал. Миграция «на ходу» давала DeadlockDetected.
set -euo pipefail
cd "$(dirname "$0")/.."

# Лишние `docker compose run` от ручных запусков — это второй сборщик в той же базе.
stray=$(docker ps --format '{{.Names}}' | grep -E '^zakup-.*-run-' || true)
if [ -n "$stray" ]; then
  echo "Останавливаю лишние разовые контейнеры: $stray"
  docker rm -f $stray >/dev/null
fi

docker compose build -q worker

file_digest=$(sha256sum src/zakup/schema.sql | cut -d' ' -f1)
db_digest=$(docker compose exec -T postgres psql -U zakup -Atc \
  "SELECT digest FROM schema_state WHERE id = 1" 2>/dev/null || true)
if [ "$file_digest" != "$db_digest" ]; then
  echo "Схема изменилась: применяю при остановленных сборщиках"
  # Если миграция упадёт, службы не должны остаться остановленными.
  trap 'echo "Миграция не прошла — поднимаю службы на старой схеме"; docker compose up -d; systemctl --user start zakup-research' EXIT
  docker compose stop worker archive specs research bot client-bot
  systemctl --user stop zakup-research
  docker compose run --rm --no-deps --entrypoint zakup worker migrate
  trap - EXIT
fi

# Всё, кроме сайта, — как обычно.
docker compose up -d --no-deps postgres worker archive specs research bot client-bot

# Сайт — две копии за шлюзом: перезапускаем по очереди и ждём, пока новая ответит.
# Пока одна перезапускается, шлюз отправляет запросы в другую — перерыва нет.
wait_healthy() {
  for _ in $(seq 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = healthy ] && return 0
    sleep 1
  done
  echo "Копия $1 не поднялась за минуту — проверьте: docker compose logs ${2}"
  return 1
}
docker compose up -d --no-deps web-2
wait_healthy zakup-web-2 web-2
docker compose up -d --no-deps web
wait_healthy zakup-web-1 web
docker compose up -d --no-deps gateway
docker compose exec -T gateway nginx -s reload >/dev/null 2>&1

systemctl --user restart zakup-research
# Дежурного не перезапускаем, если выкладку запустил он сам: иначе прервёт свой же разбор.
# И не перезапускаем, пока он разбирает сбой: перезапуск прервал бы разбор (подхватит позже).
working=$(docker compose exec -T postgres psql -U zakup -Atc \
  "SELECT count(*) FROM incidents WHERE status = 'working'" 2>/dev/null || echo 0)
if [ -z "${ZAKUP_AUTOFIX:-}" ] && [ "$working" = "0" ]; then
  systemctl --user try-restart zakup-autofix || true
fi
echo "Готово: $(git log --oneline -1)"
