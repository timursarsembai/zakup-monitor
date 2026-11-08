"""Текст технической спецификации лота из файла портала (PDF, Excel, Word).

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


def _xlsx_pages(data: bytes) -> list[str]:
    """Excel: каждый лист — «страница», строки — ячейки через « | »."""
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pages = []
    for sheet in book.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                rows.append(" | ".join(cells))
        pages.append("\n".join(rows))
    return pages


def _docx_pages(data: bytes) -> list[str]:
    """Word: абзацы и строки таблиц из word/document.xml (без сторонних библиотек)."""
    import xml.etree.ElementTree as ET
    import zipfile

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    lines = []
    for block in root.iter():
        if block.tag == f"{ns}tr":
            cells = ["".join(t.text or "" for t in cell.iter(f"{ns}t")).strip()
                     for cell in block.iter(f"{ns}tc")]
            if any(cells):
                lines.append(" | ".join(c for c in cells if c))
        elif block.tag == f"{ns}p" and not any(True for _ in block.iterfind(f".//{ns}tbl")):
            text = "".join(t.text or "" for t in block.iter(f"{ns}t")).strip()
            if text:
                lines.append(text)
    return ["\n".join(dict.fromkeys(lines))]


def _pages(data: bytes) -> list[str]:
    """Текст по страницам: PDF, а также Excel (xlsx) и Word (docx) — их тоже загружают."""
    if data[:5] == b"%PDF-":
        return [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
    if data[:2] == b"PK":
        import zipfile

        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        if any(n.startswith("xl/") for n in names):
            return _xlsx_pages(data)
        if "word/document.xml" in names:
            return _docx_pages(data)
        raise SpecError("архив или неизвестный формат файла — откройте файл")
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise SpecError("старый формат Excel/Word (xls/doc) — откройте файл")
    # Без подписи формата — пробуем как PDF (бывает мусор перед заголовком).
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]


def extract_both(pdf: bytes) -> tuple[str, str | None]:
    """(русский текст, казахский текст). Русский — раздел с характеристиками,
    если он нашёлся, иначе вся русская часть; казахский — казахская часть целиком."""
    try:
        pages = _pages(pdf)
    except SpecError:
        raise
    except Exception as error:  # битые файлы: pypdf, openpyxl и zip бросают разное
        raise SpecError(f"не удалось прочитать файл: {type(error).__name__}") from None
    if not any(page.strip() for page in pages):
        raise SpecError("в файле нет текста — откройте файл")
    russian = [p for p in pages if any(marker in p for marker in _RUSSIAN_MARKERS)]
    kazakh = [p for p in pages if p not in russian] if russian else []
    text = _clean("\n".join(russian or pages))
    if match := _SECTION.search(text):
        text = text[match.end():].strip() or text
    return text, _clean("\n".join(kazakh)) or None


def extract(pdf: bytes) -> str:
    return extract_both(pdf)[0]


def fetch(conn: psycopg.Connection, lot_id: int, url: str) -> tuple[str | None, str | None]:
    """Скачивает и разбирает PDF, сохраняет текст (сам файл не храним)."""
    try:
        (text, text_kk), error = extract_both(download(url)), None
    except (SpecError, httpx.HTTPError) as exc:
        text, text_kk = None, None
        error = str(exc) if isinstance(exc, SpecError) else "портал недоступен"
    conn.execute(
        """INSERT INTO lot_specs (lot_id, file_url, text, text_kk, error, attempts)
           VALUES (%s, %s, %s, %s, %s, 1)
           ON CONFLICT (lot_id) DO UPDATE SET file_url = excluded.file_url,
               text = excluded.text, text_kk = excluded.text_kk, error = excluded.error,
               fetched_at = now(),
               attempts = CASE WHEN excluded.text IS NULL THEN lot_specs.attempts + 1 ELSE 0 END""",
        (lot_id, url, text, text_kk, error),
    )
    conn.commit()
    return text, error


def get(conn: psycopg.Connection, lot_id: int, url: str) -> tuple[str | None, str | None]:
    """Текст спецификации из базы, а если его ещё нет — с портала."""
    row = conn.execute(
        "SELECT text, error FROM lot_specs WHERE lot_id = %s AND file_url = %s", (lot_id, url)
    ).fetchone()
    if row:
        return row[0], row[1]
    return fetch(conn, lot_id, url)


# Сетевые ошибки повторяем, но не бесконечно: битый PDF не исправится.
MAX_ATTEMPTS = 4


def prefetch(conn: psycopg.Connection, limit: int) -> int:
    """Заранее разбирает спецификации открытых лотов: сначала нужные (подписки,
    отметки, фильтр владельца), потом те, что заканчиваются раньше."""
    rows = conn.execute(
        """SELECT l.id, l.spec_url FROM lots l
           JOIN announcements a ON a.id = l.trd_buy_id
           LEFT JOIN lot_specs s ON s.lot_id = l.id
           WHERE a.end_date > now() AND l.spec_url IS NOT NULL
             AND (s.lot_id IS NULL OR s.file_url <> l.spec_url
                  OR (s.text IS NULL AND s.attempts < %(max)s
                      AND s.fetched_at < now() - interval '1 hour' * s.attempts))
           ORDER BY (EXISTS (SELECT 1 FROM matches m WHERE m.lot_id = l.id)
                     OR EXISTS (SELECT 1 FROM lot_marks k WHERE k.lot_id = l.id)) DESC,
                    a.disable_person DESC NULLS LAST, a.end_date
           LIMIT %(limit)s""",
        {"max": MAX_ATTEMPTS, "limit": limit},
    ).fetchall()
    for lot_id, url in rows:
        fetch(conn, lot_id, url)
    return len(rows)
