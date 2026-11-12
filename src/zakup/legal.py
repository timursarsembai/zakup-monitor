"""Реквизиты исполнителя для документов (оферта, политика, контакты).

Администратор заполняет их в «Настройки» → «Реквизиты»; хранятся в app_settings
(ключ 'requisites') одной JSON-строкой. Документы подставляют их на лету.
"""

import json
import time
from typing import Any

import psycopg

FIELDS = {
    "name": "Исполнитель (ИП или ТОО полностью)",
    "bin": "БИН / ИИН",
    "address": "Юридический адрес",
    "bank": "Банк",
    "iik": "ИИК (счёт)",
    "bik": "БИК",
    "phone": "Телефон",
    "email": "E-mail",
}
# Версия документов: меняется при правке текста — видна вверху документа.
DOCS_DATE = "08.11.2026"  # политика конфиденциальности
OFFER_DATE = "12.11.2026"  # оферта: пробный месяц
TTL = 60
_cache: tuple[float, dict[str, str]] | None = None


def get(conn: psycopg.Connection) -> dict[str, str]:
    global _cache
    if _cache and _cache[0] > time.monotonic():
        return _cache[1]
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'requisites'").fetchone()
    data = {key: "" for key in FIELDS}
    if row:
        try:
            data.update({k: str(v) for k, v in json.loads(row[0]).items() if k in FIELDS})
        except (ValueError, AttributeError):
            pass
    _cache = (time.monotonic() + TTL, data)
    return data


def save(conn: psycopg.Connection, data: dict[str, Any]) -> None:
    global _cache
    clean = {key: " ".join(str(data.get(key) or "").split())[:300] for key in FIELDS}
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES ('requisites', %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (json.dumps(clean, ensure_ascii=False),),
    )
    _cache = None
