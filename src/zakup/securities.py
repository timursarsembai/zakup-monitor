"""Сколько денег заморозить: обеспечение заявки и обеспечение исполнения договора.

Точных сумм в документации нет — только проценты: в проекте договора («внести обеспечение
исполнения Договора в размере 3 (трех) процентов…» — 3% в конкурсе и аукционе, 5% в запросе
ценовых предложений) и в конкурсной документации («обеспечение заявки … в размере одного
процента от суммы, выделенной…»; 3% — строительство «под ключ»). Служба specs разбирает их
из документов открытых объявлений (prefetch) — обычным поиском по тексту, без ИИ. Нет
документа или не прочитался — процент по способу закупки (FALLBACK).

Обеспечение договора — процент от суммы договора (цены победителя) плюс сумма, равная
авансу; при сильном снижении цены — ещё сумма по статье 13 Закона (её не считаем).
"""

import logging
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup import spec

logger = logging.getLogger(__name__)

# Способ закупки → (обеспечение заявки, обеспечение договора), %; проверено по документам
# объявлений ноября 2026. None — не требуется или не знаем.
FALLBACK: dict[int, tuple[float | None, float | None]] = {
    2: (1, 3),      # открытый конкурс
    188: (1, 3),    # конкурс с рейтингово-балльной системой
    120: (1, 3), 121: (1, 3),  # конкурсы по питанию
    3: (None, 5), 126: (None, 5),  # запрос ценовых предложений
    78: (None, 3),  # аукцион
}

_WORDS = {"одного": 1, "одна": 1, "двух": 2, "трех": 3, "трёх": 3, "четырех": 4, "четырёх": 4,
          "пяти": 5, "шести": 6, "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10}
_NUMBER = r"(\d+(?:[.,]\d+)?|" + "|".join(_WORDS) + r")"
_CONTRACT = re.compile(r"обеспечени[яе]\s+исполнения\s+договора\s+в\s+размере\s+" + _NUMBER
                       + r"\s*(?:\([^)]{1,20}\)\s*)?(?:%|процент)", re.IGNORECASE)
_BID = re.compile(r"обеспечени[ея]\s+заявки\s+на\s+участие\s+в\s+\w+\s+в\s+размере\s+" + _NUMBER
                  + r"\s*(?:\([^)]{1,20}\)\s*)?(?:%|процент)", re.IGNORECASE)
CONTRACT_FILE = re.compile(r"проект договора", re.IGNORECASE)
BID_FILE = re.compile(r"конкурсная документация|аукционная документация", re.IGNORECASE)


def _value(raw: str) -> float:
    raw = raw.lower()
    return float(_WORDS[raw]) if raw in _WORDS else float(raw.replace(",", "."))


def parse(text: str, pattern: re.Pattern) -> float | None:
    """Первый процент по шаблону; явно неправдоподобный (больше 30) — не верим."""
    text = " ".join((text or "").split())
    for match in pattern.finditer(text):
        value = _value(match.group(1))
        if 0 < value <= 30:
            return value
    return None


def _files(detail: dict[str, Any], pattern: re.Pattern) -> list[str]:
    files = (detail or {}).get("Files")
    if not isinstance(files, list):
        return []
    return [f["filePath"] for f in files if isinstance(f, dict) and f.get("filePath")
            and pattern.search(str(f.get("nameRu") or ""))]


def from_documents(detail: dict[str, Any]) -> dict[str, Any]:
    """Проценты из документов объявления (скачивает с портала)."""
    result: dict[str, Any] = {"contract": None, "bid": None, "source": "documents"}
    for key, files, pattern in (("contract", _files(detail, CONTRACT_FILE), _CONTRACT),
                                ("bid", _files(detail, BID_FILE), _BID)):
        for url in files[:1]:
            try:
                result[key] = parse(spec.extract(spec.download(url)), pattern)
            except Exception as exc:  # noqa: BLE001 — битый файл не должен останавливать службу
                logger.info("securities: %s: %s", url, exc)
                result["error"] = str(exc)[:200]
    return result


PREFETCH_RETRY = "1 day"
BATCH = 20  # за круг службы specs: файлы с портала качаются по 5–40 с


def prefetch(conn: psycopg.Connection, limit: int) -> int:
    """Разбирает документы открытых объявлений, у которых процентов ещё нет; сначала —
    заканчивающиеся позже (по ним ещё успеют подать)."""
    rows = conn.execute(
        """SELECT id, detail FROM announcements
           WHERE end_date > now() + interval '1 hour' AND detail IS NOT NULL AND lots_ready
             AND (securities_at IS NULL
                  OR (securities ? 'error' AND securities_at < now() - %s::interval))
           ORDER BY securities_at NULLS FIRST, end_date DESC LIMIT %s""",
        (PREFETCH_RETRY, limit)).fetchall()
    conn.commit()  # файлы качаем без открытой транзакции
    for announcement_id, detail in rows:
        found = from_documents(detail)
        conn.execute("UPDATE announcements SET securities = %s, securities_at = now() WHERE id = %s",
                     (Jsonb(found), announcement_id))
        conn.commit()
    return len(rows)


def for_lot(securities: dict[str, Any] | None, method: int | None, amount: Any, prepayment: Any,
            price: Any = None) -> dict[str, Any] | None:
    """Суммы для карточки лота. amount — плановая сумма лота (обеспечение заявки и договор
    «не больше»); price — ожидаемая цена заявки (договор при такой цене), если известна."""
    if not amount:
        return None
    found = securities or {}
    bid_pct, contract_pct = found.get("bid"), found.get("contract")
    fallback = FALLBACK.get(method or 0, (None, None))
    source = "documents"
    if contract_pct is None and fallback[1] is not None:
        contract_pct, source = fallback[1], "method"
    if bid_pct is None:  # в документах не нашли (или их нет) — как у способа закупки
        bid_pct = fallback[0]
    if bid_pct is None and contract_pct is None:
        return None
    amount = float(amount)
    advance = float(prepayment or 0)

    def contract(total: float) -> float:
        return round(total * (contract_pct or 0) / 100 + total * advance / 100, 2)

    return {"bid_pct": bid_pct, "bid": round(amount * bid_pct / 100, 2) if bid_pct else None,
            "contract_pct": contract_pct, "advance_pct": advance or None,
            "contract_max": contract(amount) if contract_pct is not None else None,
            "contract_at_price": contract(float(price)) if price and contract_pct is not None else None,
            "source": source}
