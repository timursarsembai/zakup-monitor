"""Фото своих товаров продавцов (docs/CATALOG_PLAN.md, этап 4).

Файлы — на диске (том media; ZAKUP_MEDIA_DIR, по умолчанию /data/media), раздаёт шлюз по
/media/ — не сайт и не база. При загрузке: проверка по содержимому (только JPEG, PNG, WebP;
не больше MAX_BYTES и MAX_PIXELS), поворот по EXIF, пересохранение в WebP — без EXIF
(геометки телефона), до LARGE px и превью THUMB px. Имя файла — случайное: угадать нельзя,
кэшировать можно навсегда.
"""

import io
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000  # 8000×5000: больше — не фото товара, а риск съесть память
MAX_PER_PRODUCT = 8
DAILY = 300  # фото магазина за сутки (защита от спама)
LARGE = 1600
THUMB = 400
FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}  # MPO — JPEG с телефонов (несколько кадров)
URL = "/media/p/"


def media_dir() -> Path:
    return Path(os.environ.get("ZAKUP_MEDIA_DIR", "/data/media"))


def _path(key: str, thumb: bool = False) -> Path:
    return media_dir() / "p" / key[:2] / f"{key}{'_t' if thumb else ''}.webp"


def url(key: str, thumb: bool = False) -> str:
    return f"{URL}{key[:2]}/{key}{'_t' if thumb else ''}.webp"


# Одно сжатие за раз в процессе сайта: большое фото в памяти — десятки мегабайт, а у копии
# сайта предел памяти (docker-compose.yml).
_converting = threading.Lock()
BAD = "Это не фото JPEG, PNG или WebP."


def convert(content: bytes) -> tuple[bytes, bytes, int, int]:
    """(большое, превью, ширина, высота) в WebP; ValueError — файл не подходит (текст для
    продавца). Без открытого соединения с базой: сжатие — секунды."""
    if not content:
        raise ValueError("Выберите файл.")
    if len(content) > MAX_BYTES:
        raise ValueError("Файл больше 5 МБ.")
    with _converting:
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in FORMATS:
                    raise ValueError("Подходят фото JPEG, PNG или WebP.")
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("Слишком большое фото (больше 40 мегапикселей).")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                # JPEG декодируется сразу уменьшенным (в 2–8 раз) — меньше памяти и быстрее.
                image.draft("RGB", (LARGE, LARGE))
                image.load()
                ImageOps.exif_transpose(image, in_place=True)
                mode = "RGBA" if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info else "RGB"
                large = image if image.mode == mode else image.convert(mode)
                if min(large.size) < 100:
                    raise ValueError("Слишком маленькое фото: нужно хотя бы 100 px по меньшей стороне.")
                large.thumbnail((LARGE, LARGE), Image.Resampling.LANCZOS)
                thumb = large.copy()  # уже не больше LARGE — копия небольшая
                thumb.thumbnail((THUMB, THUMB), Image.Resampling.LANCZOS)
                return _webp(large), _webp(thumb), large.width, large.height
        except ValueError as error:
            # Свои сообщения — как есть; сообщения Pillow (по-английски) — общим текстом.
            if error.args and error.args[0] in _MESSAGES:
                raise
            raise ValueError(BAD) from error
        # Битый PNG (контрольная сумма) — SyntaxError, обрезанный файл — EOFError; сбой
        # кодировщика — OSError.
        except (UnidentifiedImageError, OSError, SyntaxError, EOFError, Image.DecompressionBombError,
                MemoryError) as error:
            raise ValueError(BAD) from error


def _webp(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "WEBP", quality=82, method=4)  # без exif=… — метаданные не переносятся
    return out.getvalue()


_MESSAGES = {"Подходят фото JPEG, PNG или WebP.", "Слишком большое фото (больше 40 мегапикселей).",
             "Слишком маленькое фото: нужно хотя бы 100 px по меньшей стороне."}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def remove(keys: list[str]) -> None:
    for key in keys:
        for thumb in (False, True):
            _path(key, thumb).unlink(missing_ok=True)


def _main(conn: psycopg.Connection, product_id: int) -> None:
    """Главное фото (первое) — картинка товара в таблицах и на карточках."""
    row = conn.execute("SELECT key FROM product_photos WHERE product_id = %s ORDER BY position, id LIMIT 1",
                       (product_id,)).fetchone()
    conn.execute("UPDATE product_catalog SET image = %s, updated_at = now() WHERE id = %s",
                 (url(row[0], thumb=True) if row else None, product_id))


LIMIT_MESSAGE = "У товара уже 8 фото — больше нельзя."
DAILY_MESSAGE = "За сутки загружено 300 фото — остальные завтра."


def add(conn: psycopg.Connection, product_id: int, converted: tuple[bytes, bytes, int, int]) -> int:
    """Сохраняет сжатое фото товара (convert): файлы — сразу, строку — в транзакции
    вызывающего. Строка товара блокируется: встречные загрузки не превысят предел."""
    conn.execute("SELECT 1 FROM product_catalog WHERE id = %s FOR UPDATE", (product_id,))
    count = conn.execute("SELECT count(*) FROM product_photos WHERE product_id = %s", (product_id,)).fetchone()[0]
    if count >= MAX_PER_PRODUCT:
        raise ValueError(LIMIT_MESSAGE)
    today = conn.execute(
        """SELECT count(*) FROM product_photos ph JOIN product_catalog p ON p.id = ph.product_id
           WHERE p.seller_id = (SELECT seller_id FROM product_catalog WHERE id = %s)
             AND ph.created_at > now() - interval '1 day'""", (product_id,)).fetchone()[0]
    if today >= DAILY:
        raise ValueError(DAILY_MESSAGE)
    large, thumb, width, height = converted
    key = secrets.token_hex(16)
    _write(_path(key), large)
    _write(_path(key, thumb=True), thumb)
    try:
        photo_id = conn.execute(
            """INSERT INTO product_photos (product_id, key, width, height, position)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""", (product_id, key, width, height, count)).fetchone()[0]
        _main(conn, product_id)
    except BaseException:
        remove([key])
        raise
    return photo_id


def sweep(conn: psycopg.Connection, older_than: float = 3600) -> int:
    """Файлы без строки в базе (транзакция загрузки откатилась после записи файлов) — удаляет,
    если им больше older_than секунд (свежие — может, ещё идёт загрузка). Из `zakup tidy`."""
    root = media_dir() / "p"
    if not root.is_dir():
        return 0
    now = time.time()
    found: dict[str, list[Path]] = {}
    for path in root.glob("*/*.webp"):
        if now - path.stat().st_mtime > older_than:
            found.setdefault(path.stem.removesuffix("_t"), []).append(path)
    if not found:
        return 0
    known = {r[0] for r in conn.execute("SELECT key FROM product_photos WHERE key = ANY(%s)", (list(found),))}
    removed = 0
    for key, paths in found.items():
        if key not in known:
            for path in paths:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def listing(conn: psycopg.Connection, product_id: int) -> list[dict[str, Any]]:
    return [{"id": pid, "key": key, "width": w, "height": h, "url": url(key), "thumb": url(key, thumb=True)}
            for pid, key, w, h in conn.execute(
                "SELECT id, key, width, height FROM product_photos WHERE product_id = %s ORDER BY position, id",
                (product_id,))]


def delete(conn: psycopg.Connection, product_id: int, photo_id: int) -> str | None:
    """Убирает фото из товара; возвращает ключ файлов — удалить после коммита (remove)."""
    row = conn.execute("DELETE FROM product_photos WHERE id = %s AND product_id = %s RETURNING key",
                       (photo_id, product_id)).fetchone()
    if row is None:
        return None
    _main(conn, product_id)
    return row[0]


def make_main(conn: psycopg.Connection, product_id: int, photo_id: int) -> bool:
    """Делает фото главным: оно первое, остальные — за ним по прежнему порядку."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM product_photos WHERE product_id = %s ORDER BY position, id FOR UPDATE", (product_id,))]
    if photo_id not in ids:
        return False
    for position, pid in enumerate([photo_id] + [i for i in ids if i != photo_id]):
        conn.execute("UPDATE product_photos SET position = %s WHERE id = %s", (position, pid))
    _main(conn, product_id)
    return True


def keys(conn: psycopg.Connection, product_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT key FROM product_photos WHERE product_id = %s", (product_id,))]
