#!/usr/bin/env bash
# Тесты в контейнере на своей временной базе, в сети без интернета (служба tests в
# docker-compose.yml): ни боевой базы, ни секретов. Вывод — в файл (по умолчанию
# .test-output.txt в корне проекта); код выхода ненулевой, если хоть один тест упал.
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:-$PWD/.test-output.txt}
# Пароль боевой базы тестам не нужен; compose требует переменную только для разбора файла.
export POSTGRES_PASSWORD=unused
# Временная база общая: два прогона сразу (вы и дежурный) — по очереди.
exec 9>/tmp/zakup-test.lock
flock 9
compose() { docker compose -p zakup --profile test "$@"; }
compose build -q tests
set +e
compose run --rm tests > "$out" 2>&1
code=$?
# Проверка стиля и типичных ошибок (неиспользуемое, порядок импортов, недосмотры).
compose run --rm --no-deps --entrypoint ruff tests check --no-cache -q src tests >> "$out" 2>&1
lint=$?
compose rm -sf postgres-test >/dev/null 2>&1
set -e
tail -3 "$out"
if [ $lint -ne 0 ]; then
  echo "ПРОВЕРКА СТИЛЯ НЕ ПРОШЛА (ruff, подробности в $out)"
  exit 1
fi
if [ $code -ne 0 ] || grep -qiE '[0-9]+ (failed|errors?)\b' "$out"; then
  echo "ТЕСТЫ НЕ ПРОШЛИ (подробности в $out)"
  exit 1
fi
