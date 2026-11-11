"""Сколько денег заморозить: обеспечение заявки и обеспечение исполнения договора.

Точных сумм в документации нет — только проценты: в проекте договора («внести обеспечение
исполнения Договора в размере 3 (трех) процентов…» — 3% в конкурсе и аукционе, 5% в запросе
ценовых предложений) и в конкурсной документации («обеспечение заявки … в размере одного
процента от суммы, выделенной…»; 3% — строительство «под ключ»). Служба securities разбирает их
из документов открытых объявлений (prefetch) — обычным поиском по тексту, без ИИ. Для
способов по закону документы не качаем: в 114 разобранных объявлениях проценты совпали с
Правилами без исключений, — берём процент по способу закупки (FALLBACK).

Обеспечение договора — процент от суммы договора (цены победителя) плюс обеспечение аванса,
равное авансу (п. 542–543 Правил осуществления госзакупок, приказ МФ РК № 687).

Антидемпинг (статья 13 Закона, п. 85–99 Правил): цена ниже порога — демпинговая. В конкурсе
(товары, услуги) и в запросе ценовых предложений порог — на 10% ниже выделенной суммы; такую
цену можно подать, если вместе с обеспечением договора внести антидемпинговую сумму — «равную
сниженной сумме от минимальной допустимой цены» (порог минус ваша цена). В конкурсе на услуги
питания порог тот же, но демпинговые цены не принимаются вовсе (п. 93, 96). У работ порог
зависит от вида (СМР — 2% от сметы, текущий ремонт — 20%…), его не считаем. В аукционе
антидемпинга нет.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup import spec

logger = logging.getLogger(__name__)

# Способ закупки → (обеспечение заявки, обеспечение договора), %: п. 104 и 542 Правил
# (редакция на 31.08.2026). None — не требуется или не знаем.
FALLBACK: dict[int, tuple[float | None, float | None]] = {
    2: (1, 3),      # открытый конкурс
    188: (1, 3),    # конкурс с рейтингово-балльной системой
    120: (1, 3), 121: (1, 3),  # конкурсы по питанию (услуги / товары)
    201: (3, 3),    # конкурс по строительству «под ключ»
    3: (3, 5), 126: (3, 5),  # запрос ценовых предложений
    78: (1, 3),     # аукцион
}
CONTESTS = {2, 188, 121}
DUMPING_PCT = 10
GOODS, WORKS, SERVICES = 1, 2, 3


def dumping(method: int | None, subject: int | None) -> dict[str, Any] | None:
    """Порог демпинга: {"pct", "allowed"} — можно ли подать ниже порога с антидемпинговой
    суммой; {"works": True} — у работ порог свой по виду работ; None — антидемпинга нет."""
    if method in (3, 126):  # п. 97–98
        return {"pct": DUMPING_PCT, "allowed": True}
    if method == 120:  # услуги питания: п. 93, 96
        return {"pct": DUMPING_PCT, "allowed": False}
    if method == 201 or (method in CONTESTS and subject == WORKS):  # п. 85–89
        return {"works": True}
    if method in CONTESTS and subject in (GOODS, SERVICES):  # п. 94–95
        return {"pct": DUMPING_PCT, "allowed": True}
    return None

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
BATCH = 40
WORKERS = 1  # документы качаем только у способов вне закона — их немного; разбор PDF — до ~200 МБ


def prefetch(conn: psycopg.Connection, limit: int) -> int:
    """Разбирает документы открытых объявлений, у которых процентов ещё нет; сначала —
    заканчивающиеся позже (по ним ещё успеют подать). Только способы вне закона о госзакупках
    (тендер квазигоссектора, госсоцзаказ, особый порядок): у конкурса, аукциона и запроса
    ценовых предложений проценты заданы Правилами (FALLBACK) — документы с ними совпадают."""
    rows = conn.execute(
        """SELECT id, detail FROM announcements
           WHERE end_date > now() + interval '1 hour' AND detail IS NOT NULL AND lots_ready
             AND NOT (trade_method_id = ANY(%s))
             AND (securities_at IS NULL
                  OR (securities ? 'error' AND securities_at < now() - %s::interval))
           ORDER BY securities_at NULLS FIRST, end_date DESC LIMIT %s""",
        (list(FALLBACK), PREFETCH_RETRY, limit)).fetchall()
    conn.commit()  # файлы качаем без открытой транзакции
    with ThreadPoolExecutor(WORKERS) as pool:
        for announcement_id, found in zip((r[0] for r in rows), pool.map(lambda r: from_documents(r[1]), rows),
                                          strict=True):
            conn.execute("UPDATE announcements SET securities = %s, securities_at = now() WHERE id = %s",
                         (Jsonb(found), announcement_id))
            conn.commit()
    return len(rows)


def for_lot(securities: dict[str, Any] | None, method: int | None, amount: Any, prepayment: Any,
            subject: int | None = None, prices: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Суммы для страницы лота. amount — выделенная сумма лота (от неё обеспечение заявки и порог
    демпинга; договор — «не больше»); prices — цены заявки (осторожная/обычная/агрессивная), если
    известны: для каждой — сколько заморозить после победы."""
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
    rule = dumping(method, subject)
    floor = round(amount * (1 - rule["pct"] / 100), 2) if rule and "pct" in rule else None

    def at(price: float) -> dict[str, Any]:
        """Заморозить после победы при этой цене: обеспечение договора + аванса + антидемпинг."""
        contract = round(price * (contract_pct or 0) / 100, 2)
        advance_sum = round(price * advance / 100, 2)
        below = floor is not None and price < floor
        anti = round(floor - price, 2) if below and rule["allowed"] else 0.0
        return {"price": price, "contract": contract, "advance": advance_sum, "anti": anti,
                "rejected": below and not rule["allowed"], "total": round(contract + advance_sum + anti, 2)}

    by_price = {key: at(float(value)) for key, value in (prices or {}).items() if value}
    return {"bid_pct": bid_pct, "bid": round(amount * bid_pct / 100, 2) if bid_pct else None,
            "contract_pct": contract_pct, "advance_pct": advance or None,
            "contract_max": at(amount)["total"] if contract_pct is not None else None,
            "contract_at_price": by_price["usual"]["total"] if "usual" in by_price and contract_pct is not None else None,
            "dumping": {**rule, "floor": floor} if rule else None, "by_price": by_price, "source": source}
