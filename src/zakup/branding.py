"""Логотип и текст под ним — меняет администратор в «Настройки» → «Оформление».

Хранится в app_settings: 'brand' — JSON (текст на двух языках, версии логотипов и их
размеры), 'brand_logo_light' / 'brand_logo_dark' — сами картинки в base64. Пусто —
стандартные файлы из static. Две копии сайта видят изменения через минуту (кэш).
"""

import base64
import hashlib
import io
import json
import time
from typing import Any

import psycopg
from PIL import Image, UnidentifiedImageError

# light — логотип для светлой темы (тёмные буквы), dark — для тёмной (светлые буквы).
KINDS = {"light": "static/logo-tenderim.png", "dark": "static/logo-tenderim-light.png"}
DEFAULT_SIZE = (173, 30)
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
MAX_BYTES = 900_000  # с запасом под предел запроса nginx (1 МБ)
TAGLINE_MAX = 120
HEIGHT = 30  # высота логотипа в шапке, px
TTL = 60
_cache: tuple[float, dict[str, Any]] | None = None


def cached() -> dict[str, Any] | None:
    """Из кэша, без обращения к базе (шапка — на каждой странице)."""
    return _cache[1] if _cache and _cache[0] > time.monotonic() else None


def get(conn: psycopg.Connection) -> dict[str, Any]:
    global _cache
    if (hit := cached()) is not None:
        return hit
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'brand'").fetchone()
    data: dict[str, Any] = {}
    if row:
        try:
            data = dict(json.loads(row[0]))
        except (ValueError, TypeError):
            data = {}
    _cache = (time.monotonic() + TTL, data)
    return data


def _save(conn: psycopg.Connection, data: dict[str, Any]) -> None:
    global _cache
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES ('brand', %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (json.dumps(data, ensure_ascii=False),))
    conn.commit()
    _cache = None


def save_tagline(conn: psycopg.Connection, ru: str, kk: str) -> None:
    data = dict(get(conn))
    data["tagline_ru"] = " ".join(ru.split())[:TAGLINE_MAX]
    data["tagline_kk"] = " ".join(kk.split())[:TAGLINE_MAX]
    _save(conn, data)


def check_image(content: bytes) -> tuple[str, int, int]:
    """Формат и размер картинки; ValueError — не подходит. SVG не принимаем: в нём
    может быть скрипт."""
    if not content:
        raise ValueError("Выберите файл.")
    if len(content) > MAX_BYTES:
        raise ValueError("Файл больше 900 КБ.")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            kind, width, height = image.format, image.width, image.height
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("Это не картинка PNG, JPEG или WebP.") from error
    if kind not in FORMATS:
        raise ValueError("Подходят PNG, JPEG или WebP.")
    if not (16 <= width <= 4000 and 8 <= height <= 2000):
        raise ValueError("Слишком маленькая или слишком большая картинка.")
    return FORMATS[kind], width, height


def save_logo(conn: psycopg.Connection, kind: str, content: bytes) -> None:
    mime, width, height = check_image(content)
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (f"brand_logo_{kind}", json.dumps({"mime": mime, "data": base64.b64encode(content).decode()})))
    data = dict(get(conn))
    data[f"logo_{kind}"] = {"v": hashlib.sha256(content).hexdigest()[:12],
                            "w": round(width * HEIGHT / height), "h": HEIGHT}
    _save(conn, data)


def reset_logo(conn: psycopg.Connection, kind: str) -> None:
    conn.execute("DELETE FROM app_settings WHERE key = %s", (f"brand_logo_{kind}",))
    data = dict(get(conn))
    data.pop(f"logo_{kind}", None)
    _save(conn, data)


def logo(conn: psycopg.Connection, kind: str) -> tuple[str, bytes] | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (f"brand_logo_{kind}",)).fetchone()
    if not row:
        return None
    stored = json.loads(row[0])
    return stored["mime"], base64.b64decode(stored["data"])


def view(data: dict[str, Any], lang: str, default_tagline: str) -> dict[str, Any]:
    """Для шаблонов: адреса и размеры логотипов, текст под логотипом."""
    logos = {}
    for kind, path in KINDS.items():
        own = data.get(f"logo_{kind}")
        logos[kind] = ({"src": f"/brand/logo-{kind}?v={own['v']}", "w": own["w"], "h": own["h"]} if own
                       else {"src": "/" + path, "w": DEFAULT_SIZE[0], "h": DEFAULT_SIZE[1]})
    tagline = data.get("tagline_kk" if lang == "kk" else "tagline_ru") or data.get("tagline_ru") or default_tagline
    return {"logo": logos, "tagline": tagline,
            "tagline_ru": data.get("tagline_ru", ""), "tagline_kk": data.get("tagline_kk", "")}
