"""Тариф: название, цена и пункты описания — администратор меняет их в настройках.

Хранится в app_settings (ключ 'tariff') одной JSON-строкой; без записи — значения
по умолчанию. Читается на каждой странице (окно «платная функция»), поэтому
кэшируется на минуту.
"""

import json
import time
from typing import Any

import psycopg

DEFAULT: dict[str, Any] = {
    "name_ru": "Zakup",
    "name_kk": "Zakup",
    "price": 9900,
    "features_ru": [
        "уведомления в Telegram о новых лотах и объявлениях по вашим условиям поиска",
        "избранное с напоминаниями о сроках и итогами",
        "краткая выжимка техспецификации — без ограничений",
        "чек-лист вопросов поставщику — без ограничений",
        "совпадения по подпискам",
        "выгрузка в Excel",
    ],
    "features_kk": [
        "іздеу шарттарыңыз бойынша жаңа лоттар мен хабарландырулар туралы Telegram-да хабарламалар",
        "мерзімдер мен қорытындылар туралы еске салатын таңдаулылар",
        "техникалық ерекшеліктің қысқаша мазмұны — шектеусіз",
        "жеткізушіге сұрақтар тізімі — шектеусіз",
        "жазылымдар бойынша сәйкестіктер",
        "Excel-ге жүктеу",
    ],
}
TTL = 60
_cache: tuple[float, dict[str, Any]] | None = None


def get(conn: psycopg.Connection) -> dict[str, Any]:
    global _cache
    if _cache and _cache[0] > time.monotonic():
        return _cache[1]
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'tariff'").fetchone()
    data = dict(DEFAULT)
    if row:
        try:
            data.update({k: v for k, v in json.loads(row[0]).items() if k in DEFAULT})
        except (ValueError, AttributeError):
            pass
    _cache = (time.monotonic() + TTL, data)
    return data


def save(conn: psycopg.Connection, data: dict[str, Any]) -> None:
    global _cache
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES ('tariff', %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (json.dumps(data, ensure_ascii=False),),
    )
    _cache = None


def lines(text: str) -> list[str]:
    """Пункты описания: по одному в строке, пустые и маркеры «•», «-» убираем."""
    return [line.strip().lstrip("•-–— ").rstrip(";").strip()[:200]
            for line in (text or "").splitlines() if line.strip()][:15]
