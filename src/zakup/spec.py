"""Текст технической спецификации лота из PDF портала.

Спецификация — PDF из двух частей, на казахском и на русском. Берём русскую
и из неё раздел с характеристиками; не вышло — всю русскую часть.
"""

import io
import logging
import re
from urllib.parse import urlsplit

import httpx
import psycopg
from pypdf import PdfReader

logger = logging.getLogger(__name__)

MAX_BYTES = 15 * 1024 * 1024
ALLOWED_HOST = "ows.goszakup.gov.kz"
_SECTION = re.compile(r"эксплуатационные\s+характеристики[^:]*:", re.IGNORECASE)
_RUSSIAN_MARKERS = ("Количество", "Единица измерения", "Наименование лота")


class SpecError(Exception):
    pass


def spec_file(lot_detail: dict) -> str | None:
    """Ссылка на файл «Техническая спецификация» из ответа GraphQL по лоту."""
    for file in lot_detail.get("Files") or []:
        if "специфик" in (file.get("nameRu") or "").lower() and file.get("filePath"):
            return file["filePath"]
    return None


def download(url: str) -> bytes:
    parts = urlsplit(url)
    # Ссылка приходит из данных API: ходим только на сам портал.
    if parts.scheme != "https" or parts.hostname != ALLOWED_HOST:
        raise SpecError("ссылка не на портал госзакупок")
    chunks, size = [], 0
    with httpx.stream("GET", url, timeout=30, follow_redirects=False) as response:
        if response.status_code != 200:
            raise SpecError(f"портал ответил {response.status_code}")
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise SpecError("файл слишком большой")
            chunks.append(chunk)
    return b"".join(chunks)


def _clean(text: str) -> str:
    text = text.replace("\t", " ").replace("\xa0", " ")
    lines = [re.sub(r" {2,}", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract(pdf: bytes) -> str:
    try:
        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]
    except Exception as error:  # pypdf бросает много разных исключений на битых файлах
        raise SpecError(f"не удалось прочитать PDF: {type(error).__name__}") from None
    russian = [p for p in pages if any(marker in p for marker in _RUSSIAN_MARKERS)]
    text = _clean("\n".join(russian or pages))
    if match := _SECTION.search(text):
        section = text[match.end():].strip()
        if section:
            return section
    return text


def get(conn: psycopg.Connection, lot_id: int, url: str) -> tuple[str | None, str | None]:
    """Текст спецификации из кэша или с портала. Возвращает (текст, ошибка)."""
    row = conn.execute(
        "SELECT text, error FROM lot_specs WHERE lot_id = %s AND file_url = %s", (lot_id, url)
    ).fetchone()
    if row:
        return row[0], row[1]
    try:
        text, error = extract(download(url)), None
    except (SpecError, httpx.HTTPError) as exc:
        text, error = None, str(exc) if isinstance(exc, SpecError) else "портал недоступен"
    conn.execute(
        """INSERT INTO lot_specs (lot_id, file_url, text, error) VALUES (%s, %s, %s, %s)
           ON CONFLICT (lot_id) DO UPDATE SET file_url = excluded.file_url,
               text = excluded.text, error = excluded.error, fetched_at = now()""",
        (lot_id, url, text, error),
    )
    conn.commit()
    return text, error
