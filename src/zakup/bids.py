"""Рекомендуемая цена заявки (вкладка «Цена заявки» на странице лота; платно, сотрудникам —
без подписки).

Цифры считает код: итоги похожих лотов за год (lot_results) — от самого точного сравнения к
общему, пока не наберётся MIN_LOTS лотов с победителем: тот же раздел, регион и способ
закупки → раздел и способ → раздел → по названию. Из них — на сколько снижают цену
(медиана, «осторожно» — 25-й процентиль, «агрессивно» — 75-й), сколько участников, как часто
одна заявка и как часто закупка не состоялась. Цены — от плановой суммы лота; маржа — если в
подборе нашёлся товар с ценой. ИИ (по кнопке) объясняет это простыми словами на двух языках.
"""

import logging
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup import research
from zakup.outcomes import DISCOUNT

logger = logging.getLogger(__name__)

MIN_LOTS = 15
MIN_SHOWN = 5  # меньше — по нескольким случайным лотам цену не советуем
PERIOD = "365 days"

LEVELS = (
    ("division_region_method", "та же категория, регион и способ закупки"),
    ("division_method", "та же категория и способ закупки"),
    ("division", "та же категория"),
    ("name", "лоты с теми же словами в названии"),
)


def _lot(conn: psycopg.Connection, lot_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT l.amount, l.count, l.divisions, l.regions, a.trade_method_id, l.name_ru
           FROM lots l JOIN announcements a ON a.id = l.trd_buy_id WHERE l.id = %s""", (lot_id,)).fetchone()
    if not row or not row[0]:
        return None
    return dict(zip(("amount", "count", "divisions", "regions", "method", "name"), row, strict=True))


def _stats(conn: psycopg.Connection, where: str, params: dict[str, Any]) -> dict[str, Any]:
    """Доли «одна заявка» и «ни одной» — от всех похожих итогов: в данных портала лот с одной
    заявкой часто «не состоялся», и среди лотов с победителем одиночных почти нет."""
    row = conn.execute(
        f"""SELECT count(*) FILTER (WHERE r.outcome = 'winner' AND {DISCOUNT} IS NOT NULL),
                   count(*) FILTER (WHERE r.outcome = 'failed'), count(*),
                   percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY {DISCOUNT}),
                   avg(jsonb_array_length(r.offers)) FILTER (WHERE r.offers <> '[]'),
                   count(*) FILTER (WHERE jsonb_array_length(r.offers) = 1),
                   count(*) FILTER (WHERE r.outcome = 'failed' AND r.offers = '[]')
            FROM lot_results r JOIN lots l ON l.id = r.lot_id JOIN announcements a ON a.id = l.trd_buy_id
            WHERE r.result_date > now() - interval '{PERIOD}' AND l.id <> %(lot)s AND {where}""",
        params).fetchone()
    won, failed, total, quartiles, offers, single, empty = row
    low, median, high = (float(q) if q is not None else None for q in quartiles or (None, None, None))
    share = lambda n: n / total if total else None  # noqa: E731
    return {"won": won, "total": total, "failed_share": share(failed), "low": low, "median": median,
            "high": high, "offers": float(offers) if offers is not None else None,
            "single": share(single), "empty": share(empty)}


def compute(conn: psycopg.Connection, lot_id: int, items: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Статистика и цены; None — нет данных лота или похожих итогов. items — найденные товары
    (из подбора) для расчёта маржи."""
    lot = _lot(conn, lot_id)
    if lot is None:
        return None
    params = {"lot": lot_id, "divisions": lot["divisions"] or [], "regions": lot["regions"] or [],
              "method": lot["method"], "name": lot["name"] or ""}
    conditions = {
        "division_region_method": "l.divisions && %(divisions)s::text[] AND l.regions && %(regions)s::text[]"
                                  " AND a.trade_method_id = %(method)s",
        "division_method": "l.divisions && %(divisions)s::text[] AND a.trade_method_id = %(method)s",
        "division": "l.divisions && %(divisions)s::text[]",
        "name": "l.tsv @@ plainto_tsquery('russian', %(name)s)",
    }
    # Самое точное сравнение, где набралось MIN_LOTS; если нигде — самое точное, где хотя бы
    # MIN_SHOWN (общее сравнение по названию не должно затирать более точное).
    chosen = None
    for level, label in LEVELS:
        if level != "name" and not params["divisions"]:
            continue
        if level == "division_region_method" and not params["regions"]:
            continue
        stats = _stats(conn, conditions[level], params)
        if stats["won"] < MIN_SHOWN or stats["median"] is None:
            continue
        if chosen is None or stats["won"] >= MIN_LOTS:
            chosen = {**stats, "level": level, "level_label": label}
        if stats["won"] >= MIN_LOTS:
            break
    if chosen is None:
        return None
    amount = float(lot["amount"])
    prices = {key: round(amount * (1 - float(chosen[q])), 2)
              for key, q in (("careful", "low"), ("usual", "median"), ("bold", "high"))}
    cost = None
    # Как маржа в подборе: по лучшему уровню соответствия (полностью → частично → аналог);
    # цены, пересчитанные из упаковки (unit_estimated), — не в счёт.
    priced = []
    for fit in ("полностью", "частично", "аналог"):
        priced = [float(i["unit_price"]) for i in items or []
                  if i.get("fit") == fit and not i.get("unit_estimated")
                  and isinstance(i.get("unit_price"), (int, float)) and i["unit_price"] > 0]
        if priced:
            break
    if priced and lot["count"]:
        cost = round(min(priced) * float(lot["count"]), 2)
    margins = {key: (round(price - cost, 2) if cost is not None else None) for key, price in prices.items()}
    return {**chosen, "amount": amount, "count": float(lot["count"] or 0), "prices": prices,
            "cost": cost, "margins": margins, "reliable": chosen["won"] >= MIN_LOTS}


# --- Объяснение ИИ (по кнопке; одно на лот, общее для всех) ----------------------------

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}, "kk": {"type": "string"}},
          "required": ["text", "kk"]}
PROMPT = """Ты помогаешь поставщику на госзакупках Казахстана решить, по какой цене подать заявку.
Лот: {name}. Количество: {count}. Плановая сумма: {amount} тенге.

Итоги похожих закупок за год ({level}; всего итогов: {total}, из них с победителем: {won}):
- обычное снижение от плановой суммы (медиана): {median}; осторожно (25%): {low}; агрессивно (75%): {high};
- заявок в среднем (где они были): {offers};
- из всех похожих: одна заявка — {single}, ни одной заявки — {empty}, закупка не состоялась — {failed}.
Цены заявки: осторожная {careful} ₸, обычная {usual} ₸, агрессивная {bold} ₸.

Напиши 4–6 коротких предложений простыми словами: какую цену разумно ставить и почему, чем
рискует слишком низкая цена, что значат число заявок и доля несостоявшихся. Себестоимость
неизвестна — напомни сравнить цену со своей закупочной ценой с доставкой. Без воды и без общих советов, только по этим цифрам.
Не выдумывай данных и правил закона — только эти цифры. text — по-русски, kk — то же на
казахском (числа не меняй), только кириллицей, без латинских букв внутри слов."""

# Модель иногда сбивается на казахском: слово со смесью алфавитов («бermeңіз») или латиницей
# строчными («artpau»; марки и стандарты обычно с заглавной — ISO, Word).
MIXED = re.compile(r"(?i:\w*(?:[а-яёәғқңөұүһі][a-z]|[a-z][а-яёәғқңөұүһі])\w*)|\b[a-z]{3,}\b")


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.0f}%" if value is not None else "нет данных"


def _money(value: Any) -> str:
    return f"{float(value):,.0f}".replace(",", " ") if value is not None else "неизвестна"


def request(conn: psycopg.Connection, lot_id: int) -> str:
    provider, model = research.light_model(conn)
    row = conn.execute(
        """INSERT INTO bid_advice (lot_id, provider, model) VALUES (%s, %s, %s)
           ON CONFLICT (lot_id) DO UPDATE SET status = 'queued', error = NULL, created_at = now(),
               provider = excluded.provider, model = excluded.model
           WHERE bid_advice.status = 'error' OR bid_advice.created_at < now() - interval '7 days'
              OR (bid_advice.status = 'queued' AND bid_advice.created_at < now() - interval '30 minutes')
           RETURNING status""", (lot_id, provider, model)).fetchone()
    conn.commit()
    return row[0] if row else of(conn, lot_id)["status"]


def of(conn: psycopg.Connection, lot_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT status, result, error FROM bid_advice WHERE lot_id = %s", (lot_id,)).fetchone()
    return dict(zip(("status", "result", "error"), row, strict=True)) if row else None


def _explain(conn: psycopg.Connection, lot_id: int, provider: str, model: str) -> dict[str, Any]:
    # Объяснение общее на всех — только по статистике итогов: находки подбора у каждого клиента
    # свои, в общий текст их не берём.
    data = compute(conn, lot_id)
    lot = _lot(conn, lot_id)
    if data is None or lot is None:
        raise research.ResearchError("нет похожих итогов для расчёта")
    prompt = PROMPT.format(
        name=lot["name"] or "—", count=_money(data["count"]), amount=_money(data["amount"]),
        level=data["level_label"], won=data["won"], total=data["total"], empty=_pct(data["empty"]), median=_pct(data["median"]), low=_pct(data["low"]),
        high=_pct(data["high"]), offers=f"{float(data['offers']):.1f}" if data["offers"] else "нет данных",
        single=_pct(data["single"]), failed=_pct(data["failed_share"]),
        careful=_money(data["prices"]["careful"]), usual=_money(data["prices"]["usual"]),
        bold=_money(data["prices"]["bold"]))
    key = research.api_key(conn, provider)
    conn.commit()
    for _ in range(2):  # вторая попытка — если казахский текст вышел со смесью алфавитов
        if provider == "claude_code":
            raw = research.providers._claude_code(prompt, model, SCHEMA, web=False)
        elif provider == "anthropic":
            raw = research.providers._anthropic(prompt, model, key, schema=SCHEMA, web=False)
        else:
            raw = research.providers._openai(prompt, model, key, schema=SCHEMA, web=False)
        text = str(raw.get("text") or "").strip()[:2000]
        if not text:
            raise research.ResearchError("ИИ не вернул объяснения")
        kk = str(raw.get("kk") or "").strip()[:2000]
        if not MIXED.search(kk):
            break
    else:
        kk = ""  # казахский не удался — покажем русский текст
    return {"text": text, "kk": kk}


def work_one(conn: psycopg.Connection, providers: list[str]) -> bool:
    row = conn.execute(
        """UPDATE bid_advice SET status = 'running', started_at = now()
           WHERE lot_id = (SELECT lot_id FROM bid_advice WHERE status = 'queued' AND provider = ANY(%s)
                           ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
           RETURNING lot_id, provider, model""", (providers,)).fetchone()
    conn.commit()
    if not row:
        return False
    try:
        result, error = _explain(conn, *row), None
    except Exception as exc:  # noqa: BLE001 — задача не должна зависнуть в running
        conn.rollback()
        logger.warning("bid advice for lot %s failed: %s", row[0], exc)
        result, error = None, str(exc)[:300]
    conn.execute("UPDATE bid_advice SET status = %s, result = %s, error = %s, finished_at = now() WHERE lot_id = %s",
                 ("done" if result else "error", Jsonb(result) if result else None, error, row[0]))
    conn.commit()
    return True
