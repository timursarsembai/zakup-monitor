#!/usr/bin/env bash
# Подтягивает секреты из Infisical (проект zakup, окружение prod) в .env.
# Ключи из Infisical перезаписывают одноимённые строки .env, остальные строки
# (пароль Postgres и т.п.) остаются как есть. Прежний файл — в .env.bak.
# Учётка машины: ~/.config/infisical/zakup.env (роль Viewer только на zakup).
set -euo pipefail
cd "$(dirname "$0")/.."

bin="${INFISICAL_BIN:-$HOME/.local/bin/infisical}"
conf="${INFISICAL_CONF:-$HOME/.config/infisical/zakup.env}"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

# Учётные данные живут только в подоболочке и не попадают в argv (видно в ps).
(
  set -a
  # shellcheck disable=SC1090
  . "$conf"
  set +a
  export INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$INFISICAL_CLIENT_ID"
  export INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$INFISICAL_CLIENT_SECRET"
  token=$("$bin" login --method=universal-auth --domain="$INFISICAL_API_URL" --silent --plain)
  INFISICAL_TOKEN="$token" "$bin" export --env=prod --projectId="$INFISICAL_PROJECT_ID" \
    --domain="$INFISICAL_API_URL" --format=dotenv --silent > "$tmp"
)

if ! grep -q '^GOSZAKUP_TOKEN=' "$tmp"; then
  echo "!! В ответе Infisical нет GOSZAKUP_TOKEN, .env не трогаю" >&2
  exit 1
fi

[ -f .env ] && cp .env .env.bak && chmod 600 .env.bak
python3 - "$tmp" <<'PY'
import os, sys

def parse(path):
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            key, sep, value = line.rstrip("\n").partition("=")
            if sep and key and not key.startswith("#"):
                # CLI отдаёт значения в одинарных кавычках, compose их не снимает.
                if len(value) >= 2 and value[0] == value[-1] == "'":
                    value = value[1:-1]
                out[key] = value
    return out

fresh = parse(sys.argv[1])
lines, seen = [], set()
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        key = line.partition("=")[0]
        if key in fresh:
            line = f"{key}={fresh[key]}\n"
            seen.add(key)
        lines.append(line)
lines += [f"{k}={v}\n" for k, v in fresh.items() if k not in seen]
fd = os.open(".env.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.writelines(lines)
os.replace(".env.new", ".env")
print(f"==> .env: из Infisical обновлено {len(fresh)} ключ(ей): {', '.join(sorted(fresh))}")
PY
