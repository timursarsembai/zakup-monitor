#!/usr/bin/env bash
# Проверка живого сайта в браузере (Playwright в контейнере): ошибки скриптов, шторка фильтра.
# Пакет playwright ставится один раз в ~/.cache/zakup-smoke.
set -euo pipefail
cd "$(dirname "$0")/.."
image=mcr.microsoft.com/playwright:v1.64.1-noble
dir="$HOME/.cache/zakup-smoke"
mkdir -p "$dir"
cp scripts/smoke.mjs "$dir/"
run() { docker run --rm -u "$(id -u)" -e HOME=/tmp -v "$dir:/work" -w /work "$image" "$@"; }
[ -d "$dir/node_modules/playwright" ] || run npm install --silent --no-audit --no-fund playwright@1.64.1 >/dev/null
run node smoke.mjs
