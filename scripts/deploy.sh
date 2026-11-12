#!/usr/bin/env bash
# Выкладка: собрать образ, новые миграции базы применить при остановленных
# сборщиках, поднять все службы, перезапустить ИИ-службу на хосте.
#
# Почему так: изменение таблиц (ALTER) ждёт, пока все закроют транзакции, а сборщики
# держат их открытыми, пока ждут портал. Миграция «на ходу» давала DeadlockDetected.
set -euo pipefail
cd "$(dirname "$0")/.."

# Одна выкладка за раз и не во время ночного сжатия таблиц (scripts/compact.sh).
exec 8>/tmp/zakup-deploy.lock
flock -w 900 8 || { echo "Идёт другая выкладка или сжатие таблиц — подождите"; exit 1; }

# Выкладываем только закоммиченное: образ собирается из рабочей папки, и незакоммиченная
# правка (чужая или недоделанная) иначе уехала бы на сервер незаметно.
if [ -n "$(git status --porcelain)" ]; then
  echo "В репозитории незакоммиченные правки — сначала коммит (git status)."
  exit 1
fi

# Лишние `docker compose run` от ручных запусков — это второй сборщик в той же базе.
stray=$(docker ps --format '{{.Names}}' | grep -E '^zakup-.*-run-' || true)
if [ -n "$stray" ]; then
  echo "Останавливаю лишние разовые контейнеры: $stray"
  docker rm -f $stray >/dev/null
fi

docker compose build -q worker

# Новые миграции — файлы src/zakup/migrations/*.sql, которых нет в schema_migrations.
applied=$(docker compose exec -T postgres psql -U zakup -Atc \
  "SELECT name FROM schema_migrations" 2>/dev/null || true)
new_migrations=""
for file in src/zakup/migrations/*.sql; do
  name=$(basename "$file" .sql)
  grep -qxF "$name" <<< "$applied" || new_migrations="$new_migrations $name"
done
if [ -n "$new_migrations" ]; then
  echo "Новые миграции:$new_migrations — применяю при остановленных сборщиках"
  # Если миграция упадёт, службы не должны остаться остановленными.
  # Запускаем те же остановленные контейнеры (прежний код), а не пересоздаём из нового
  # образа: новый код на старой схеме и повтор миграции при живых сборщиках — хуже.
  trap 'echo "Миграция не прошла — запускаю прежние службы на старой схеме"; docker compose start worker archive history specs securities scores research bot client-bot; docker compose start skk reserve 2>/dev/null || true; systemctl --user start zakup-research' EXIT
  docker compose stop worker archive history specs securities scores skk reserve research bot client-bot
  systemctl --user stop zakup-research
  docker compose run --rm --no-deps --entrypoint zakup worker migrate
  trap - EXIT
fi

# Всё, кроме сайта, — как обычно.
docker compose up -d --no-deps postgres worker archive history specs securities scores skk reserve research bot client-bot

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
# Конфигурация шлюза — проверить до перезапуска (ошибка в ней положила бы сайт).
docker run --rm -v "$PWD/deploy/gateway/default.conf:/etc/nginx/conf.d/default.conf:ro" nginx:1.27-alpine nginx -t -q
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

# Сайт уже выложен; проверка в браузере только предупреждает (откат — вручную).
scripts/smoke.sh || echo "ВНИМАНИЕ: проверка в браузере не прошла — смотрите вывод выше"
