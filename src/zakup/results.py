"""Итоги закупки: победители из протокола, договоры и апелляции.

Заявки поставщиков публичный API не отдаёт (TrdApp пуст), поэтому участников,
их цены, победителя и второе место берём из PDF «Протокол итогов» запроса
ценовых предложений. Договоры и апелляции — из GraphQL.
"""

import io
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from zakup import details, spec
from zakup.goszakup import Client, GoszakupError

logger = logging.getLogger(__name__)

PROTOCOL_NAME = "протокол итогов"
CONTRACTS_QUERY = """{ Contract(limit: 100, filter: {trdBuyId: %(id)d}) {
  id contractNumberSys contractSum signDate supplierBiin
  RefContractStatus { nameRu } Supplier { nameRu }
  ContractUnits { lotId quantity itemPrice totalSum }
  File { nameRu filePath }
} ComplaintAppeal(limit: 100, filter: {trdBuyId: %(id)d}) {
  id numberAppeal dateApply decisionDate Files { filePath }
} }"""
# Договоры подписывают через несколько дней после итогов: столько ждём перед новым запросом.
RESULTS_TTL = timedelta(hours=3)
# Итоги отмеченных лотов проверяем столько дней после окончания приёма.
FOLLOW_DAYS = 30

_OFFER = re.compile(
    r"^(\d+)\n([\s\S]+?)\n(\d{12})\n((?:[\d.]+\n){2,3})(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})$",
    re.MULTILINE,
)
_WINNER = re.compile(
    r"Определить победителем по лоту №\s*(\S+)\s*:\s*(\d{12})\s+(.+?)\s*"
    r"(?:,\s*потенциальным поставщиком, занявшим второе место\s+(\d{12})\s+(.+?)\s*)?"
    r"(?=\s2\.\s|$)"
)
_FAILED = re.compile(r"по лоту №\s*(\S+)\s+несостоявшейся в связи с\s+(.+?)\s*(?=Примечание|$)")


def protocol_file(detail: dict) -> str | None:
    for file in detail.get("Files") or []:
        if (file.get("nameRu") or "").strip().lower() == PROTOCOL_NAME and file.get("filePath"):
            return file["filePath"]
    return None


def protocol_text(pdf: bytes) -> str:
    """Русская часть протокола: строки без лишних пробелов."""
    try:
        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]
    except Exception as error:  # pypdf бросает много разных исключений на битых файлах
        raise spec.SpecError(f"не удалось прочитать PDF: {type(error).__name__}") from None
    text = "\n".join(pages).replace("\t", " ").replace("\xa0", " ")
    start = text.find("Протокол об итогах")
    text = text[start:] if start >= 0 else text
    lines = [re.sub(r" {2,}", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _decimal(value: str) -> str | None:
    try:
        return str(Decimal(value))
    except InvalidOperation:
        return None


def parse_protocol(text: str) -> dict[str, dict[str, Any]]:
    """{номер лота: итог}. Итог: outcome ('winner' / 'failed'), предложения, победитель."""
    results: dict[str, dict[str, Any]] = {}
    for section in re.split(r"^Лот №\n", text, flags=re.MULTILINE)[1:]:
        number = section.split("\n", 1)[0].strip()
        offers_start = section.find("представлены следующие ценовые")
        offers = []
        if offers_start >= 0:
            for match in _OFFER.finditer(section[offers_start:]):
                numbers = match.group(4).split()
                offers.append({
                    "name": " ".join(match.group(2).split()),
                    "bin": match.group(3),
                    # Без цены за единицу в протоколе две колонки: сумма по ст. 13 и общая.
                    "price": _decimal(numbers[0]) if len(numbers) == 3 else None,
                    "total": _decimal(numbers[-1]),
                    "applied_at": match.group(5),
                })
        flat = " ".join(section.split())
        result: dict[str, Any] = {"offers": offers}
        if match := _WINNER.search(flat):
            result.update(
                outcome="winner",
                winner_bin=match.group(2),
                winner_name=match.group(3).rstrip(" ,."),
                second_bin=match.group(4),
                second_name=(match.group(5) or "").rstrip(" ,.") or None,
            )
        elif match := _FAILED.search(flat):
            result.update(outcome="failed", reason=match.group(2).rstrip(" ."))
        else:
            continue
        results[number] = result
    return results


def _save_protocol(conn: psycopg.Connection, detail: dict, url: str) -> int:
    parsed = parse_protocol(protocol_text(spec.download(url)))
    lot_ids = {
        lot.get("lotNumber"): int(lot["id"]) for lot in detail.get("Lots") or [] if lot.get("id")
    }
    saved = 0
    for number, result in parsed.items():
        if (lot_id := lot_ids.get(number)) is None:
            continue
        winner = next((o for o in result["offers"] if o["bin"] == result.get("winner_bin")), None)
        conn.execute(
            """INSERT INTO lot_results (lot_id, outcome, reason, winner_bin, winner_name,
                                        winner_total, second_bin, second_name, offers, protocol_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (lot_id) DO UPDATE SET outcome = excluded.outcome,
                   reason = excluded.reason, winner_bin = excluded.winner_bin,
                   winner_name = excluded.winner_name, winner_total = excluded.winner_total,
                   second_bin = excluded.second_bin, second_name = excluded.second_name,
                   offers = excluded.offers, protocol_url = excluded.protocol_url,
                   parsed_at = now()""",
            (lot_id, result["outcome"], result.get("reason"), result.get("winner_bin"),
             result.get("winner_name"), winner and winner["total"], result.get("second_bin"),
             result.get("second_name"), Jsonb(result["offers"]), url),
        )
        saved += 1
    return saved


def refresh(conn: psycopg.Connection, client: Client, announcement_id: int) -> None:
    """Дочитывает договоры, апелляции и, если появился, разбирает протокол итогов."""
    data = client.graphql(CONTRACTS_QUERY % {"id": announcement_id})
    conn.execute(
        "UPDATE announcements SET results = %s, results_fetched_at = now() WHERE id = %s",
        (Jsonb({"contracts": data.get("Contract") or [],
                "appeals": data.get("ComplaintAppeal") or []}), announcement_id),
    )
    detail = conn.execute(
        "SELECT detail FROM announcements WHERE id = %s", (announcement_id,)
    ).fetchone()[0] or {}
    parse_announcement(conn, announcement_id, detail)
    conn.commit()


def parse_announcement(conn: psycopg.Connection, announcement_id: int, detail: dict) -> None:
    """Разбирает протокол итогов объявления, если он есть и ещё не разобран."""
    url = protocol_file(detail)
    if not url:
        return
    done = conn.execute(
        "SELECT protocol_url = %s FROM announcements WHERE id = %s", (url, announcement_id)
    ).fetchone()
    if done and done[0]:
        return
    try:
        _save_protocol(conn, detail, url)
    except (spec.SpecError, httpx.HTTPError) as error:
        # Сетевой сбой — попробуем в следующий раз; битый файл — не будем.
        logger.warning("protocol of %s not parsed: %s", announcement_id, error)
        if isinstance(error, httpx.HTTPError):
            return
    conn.execute(
        "UPDATE announcements SET protocol_url = %s WHERE id = %s", (url, announcement_id)
    )


def parse_pending(conn: psycopg.Connection, limit: int) -> int:
    """Протоколы итогов завершённых объявлений, начиная с новых: для поиска по итогам."""
    rows = conn.execute(
        """SELECT id, detail FROM announcements
           WHERE status_id = 350 AND detail IS NOT NULL
             AND protocol_url IS DISTINCT FROM (
                 -- Files бывает null: тогда пустой список, а не ошибка.
                 SELECT f->>'filePath' FROM jsonb_array_elements(
                     CASE WHEN jsonb_typeof(detail->'Files') = 'array'
                          THEN detail->'Files' ELSE '[]' END) f
                 WHERE lower(f->>'nameRu') = %s LIMIT 1)
           ORDER BY end_date DESC LIMIT %s""",
        (PROTOCOL_NAME, limit),
    ).fetchall()
    for announcement_id, detail in rows:
        parse_announcement(conn, announcement_id, detail)
        conn.commit()
    return len(rows)


def stale(fetched_at: datetime | None, now: datetime | None = None) -> bool:
    return fetched_at is None or (now or datetime.now(UTC)) - fetched_at > RESULTS_TTL


def follow(conn: psycopg.Connection, client: Client, limit: int = 50) -> int:
    """Итоги по отмеченным лотам, у которых закончился приём заявок."""
    ids = [
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT a.id, a.results_fetched_at FROM lot_marks m
               JOIN lots l ON l.id = m.lot_id
               JOIN announcements a ON a.id = l.trd_buy_id
               WHERE m.status NOT IN ('', 'rejected')
                 AND a.end_date < now() AND a.end_date > now() - %s
                 AND (NOT EXISTS (SELECT 1 FROM lot_results r WHERE r.lot_id = l.id)
                      OR a.results_fetched_at IS NULL
                      OR a.results_fetched_at < now() - %s)
               ORDER BY a.results_fetched_at NULLS FIRST LIMIT %s""",
            (timedelta(days=FOLLOW_DAYS), RESULTS_TTL, limit),
        )
    ]
    for announcement_id in ids:
        try:
            details.fetch_one(conn, client, announcement_id)
            refresh(conn, client, announcement_id)
        except GoszakupError as error:
            conn.rollback()
            logger.warning("results of %s: %s", announcement_id, error)
    return len(ids)


def by_lot(conn: psycopg.Connection, lot_ids: list[int]) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """SELECT lot_id, outcome, reason, winner_bin, winner_name, winner_total,
                  second_bin, second_name, offers, protocol_url
           FROM lot_results WHERE lot_id = ANY(%s)""",
        (lot_ids,),
    )
    keys = ("outcome", "reason", "winner_bin", "winner_name", "winner_total",
            "second_bin", "second_name", "offers", "protocol_url")
    return {row[0]: dict(zip(keys, row[1:])) for row in rows}


def contracts_by_lot(results: dict | None) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for contract in (results or {}).get("contracts") or []:
        for unit in contract.get("ContractUnits") or []:
            if unit.get("lotId") and contract not in found.setdefault(unit["lotId"], []):
                found[unit["lotId"]].append(contract)
    return found

