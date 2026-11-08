#!/usr/bin/env bash
# Пользователь базы для приложения — zakup_app, без прав суперпользователя: владелец
# только таблиц zakup. Суперпользователь zakup (создан образом postgres, отключить
# нельзя) остаётся для обслуживания изнутри контейнера: psql/pg_dump по сокету.
#
# Почему: с правами суперпользователя любая ошибка в SQL или чужой код в процессе
# приложения дали бы выполнение команд в контейнере базы (COPY … TO PROGRAM), чтение
# файлов и других баз. Владельцу таблиц это недоступно.
#
# Повторный запуск безопасен: пароль — из .env (APP_DB_PASSWORD, создаётся при первом
# запуске), владелец — только у объектов, где он ещё не zakup_app. Меняет владельца с
# коротким ожиданием блокировки: сборщики лучше остановить (scripts/deploy.sh делает это
# сам при новых миграциях; при первом запуске — вручную).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! grep -q '^APP_DB_PASSWORD=' .env; then
  # Пароль не печатаем и не передаём аргументом: сразу в .env (права 600).
  printf 'APP_DB_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env
  echo "==> APP_DB_PASSWORD добавлен в .env"
fi
password=$(grep -E '^APP_DB_PASSWORD=' .env | cut -d= -f2- | tr -d '"')

# Пароль — первой строкой stdin (переменная psql), не аргументом: аргументы видны в ps.
{ printf "\\set pw '%s'\n" "$password"; cat <<'SQL'; } | \
  docker compose exec -T postgres psql -U zakup -d zakup -v ON_ERROR_STOP=1 -q
SET lock_timeout = '5s';
SELECT 'CREATE ROLE zakup_app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'zakup_app')
\gexec
ALTER ROLE zakup_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'pw';
ALTER DATABASE zakup OWNER TO zakup_app;
REVOKE ALL ON DATABASE zakup FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE zakup TO zakup_app;
-- Таблицы и представления (их последовательности id переходят вместе с ними).
SELECT format('ALTER %s %s OWNER TO zakup_app',
              CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
              c.oid::regclass)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
  AND pg_get_userbyid(c.relowner) <> 'zakup_app'
\gexec
-- Отдельные последовательности, не привязанные к столбцу.
SELECT format('ALTER SEQUENCE %s OWNER TO zakup_app', c.oid::regclass)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'S' AND pg_get_userbyid(c.relowner) <> 'zakup_app'
  AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a', 'i'))
\gexec
SQL
left=$(docker compose exec -T postgres psql -U zakup -d zakup -Atc \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) <> 'zakup_app'")
echo "==> zakup_app: готово; объектов с другим владельцем: $left"
