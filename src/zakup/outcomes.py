"""«Итоги тендеров» (/results): поиск по завершённым лотам — кто, у кого и за сколько выиграл.

Источник — разобранные протоколы итогов (lot_results). Список, победитель и цена открыты
всем; все участники с ценами (страница лота) и выгрузка в Excel — по подписке.
Сводка над списком считается по тому же фильтру: сколько, на какую сумму, снижение цены,
доля несостоявшихся и кто чаще выигрывает.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from zakup.filters import REGIONS

PERIODS = {"7": "7 дней", "30": "30 дней", "90": "90 дней", "365": "Год", "all": "Всё время"}
OUTCOMES = {"winner": "С победителем", "all": "Все", "failed": "Не состоялись"}
DISCOUNTS = {"": "Любое", "5": "от 5%", "10": "от 10%", "20": "от 20%", "30": "от 30%"}
SORTS = {"date": "Сначала новые", "amount": "Сумма лота — больше", "discount": "Снижение — больше",
         "offers": "Участников — больше"}
PAGE_SIZE = 50
MAX_PAGE = 200

SOURCE = "lot_results r JOIN lots l ON l.id = r.lot_id JOIN announcements a ON a.id = l.trd_buy_id"
# Дата итогов: из подробностей объявления, иначе — когда разобрали протокол.
DATE = "coalesce(a.itogi_date, r.parsed_at)"
# Снижение от плановой суммы лота — только там, где оно осмысленно. Цена победы меньше 5%
# плановой — почти всегда цена за единицу в протоколе (топливо — за литр), а не за лот.
DISCOUNT = """(CASE WHEN r.outcome = 'winner' AND l.amount > 0 AND r.winner_total BETWEEN l.amount * 0.05 AND l.amount
                    THEN 1 - r.winner_total / l.amount END)"""
ORDER = {"date": f"{DATE} DESC, l.id DESC", "amount": "l.amount DESC NULLS LAST, l.id DESC",
         "discount": f"{DISCOUNT} DESC NULLS LAST, l.id DESC",
         "offers": "jsonb_array_length(r.offers) DESC, l.id DESC"}


@dataclass
class ResultFilters:
    q: str = ""
    customer: str = ""
    winner: str = ""
    regions: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    min_sum: int | None = None
    max_sum: int | None = None
    period: str = "30"
    outcome: str = "winner"
    discount: str = ""
    sort: str = "date"

    @classmethod
    def from_query(cls, params: dict[str, list[str]], divisions: dict[str, str]) -> "ResultFilters":
        def one(key: str) -> str:
            return " ".join((params.get(key) or [""])[0].split())[:100]

        def money(key: str) -> int | None:
            digits = "".join(c for c in one(key) if c.isdigit())[:15]
            return int(digits) if digits else None

        f = cls(q=one("q"), customer=one("customer"), winner=one("winner"),
                regions=[r for r in dict.fromkeys(params.get("region") or []) if r in REGIONS],
                categories=[c for c in dict.fromkeys(params.get("category") or []) if c in divisions],
                min_sum=money("min_sum"), max_sum=money("max_sum"))
        f.period = one("period") if one("period") in PERIODS else "30"
        f.outcome = one("outcome") if one("outcome") in OUTCOMES else "winner"
        f.discount = one("discount") if one("discount") in DISCOUNTS else ""
        f.sort = one("sort") if one("sort") in SORTS else "date"
        return f

    def query(self, **changes: Any) -> list[tuple[str, Any]]:
        """Пары для адреса (без пустых): ссылки страниц, Excel, «показать все»."""
        values = {**self.__dict__, **changes}
        pairs: list[tuple[str, Any]] = []
        for key in ("q", "customer", "winner"):
            if values[key]:
                pairs.append((key, values[key]))
        pairs += [("region", r) for r in values["regions"]] + [("category", c) for c in values["categories"]]
        for key in ("min_sum", "max_sum"):
            if values[key] is not None:
                pairs.append((key, values[key]))
        for key, default in (("period", "30"), ("outcome", "winner"), ("discount", ""), ("sort", "date")):
            if values[key] != default:
                pairs.append((key, values[key]))
        return pairs

    def active_count(self) -> int:
        return sum(1 for flag in (self.q, self.customer, self.winner, self.regions, self.categories,
                                  self.min_sum is not None or self.max_sum is not None,
                                  self.period != "30", self.outcome != "winner", self.discount) if flag)

    def where(self, now: datetime, with_outcome: bool = True) -> tuple[str, dict[str, Any]]:
        clauses, params = ["true"], {}
        if with_outcome and self.outcome != "all":
            clauses.append("r.outcome = %(outcome)s")
            params["outcome"] = self.outcome
        if self.period != "all":
            clauses.append(f"{DATE} >= %(since)s")
            params["since"] = now - timedelta(days=int(self.period))
        if self.q:
            clauses.append("(l.tsv @@ websearch_to_tsquery('russian', %(q)s)"
                           " OR l.tsv_kk @@ websearch_to_tsquery('simple', %(q)s))")
            params["q"] = self.q
        for key, column in (("customer", "l.customer"), ("winner", "r.winner")):
            value = getattr(self, key)
            if value.isdigit():
                clauses.append(f"{column}_bin = %({key})s")
                params[key] = value
            elif value:
                clauses.append(f"{column}_name{'_ru' if key == 'customer' else ''} ILIKE %({key})s")
                params[key] = "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if self.regions:
            clauses.append("l.regions && %(regions)s::text[]")
            params["regions"] = self.regions
        if self.categories:
            clauses.append("l.divisions && %(categories)s::text[]")
            params["categories"] = self.categories
        if self.min_sum is not None:
            clauses.append("l.amount >= %(min_sum)s")
            params["min_sum"] = self.min_sum
        if self.max_sum is not None:
            clauses.append("l.amount <= %(max_sum)s")
            params["max_sum"] = self.max_sum
        if self.discount:
            clauses.append(f"{DISCOUNT} >= %(discount)s")
            params["discount"] = int(self.discount) / 100
        return " AND ".join(clauses), params


COLUMNS = f"""l.id, l.lot_number, l.name_ru, l.name_kz, l.customer_name_ru, l.customer_bin,
              l.regions, l.divisions, l.amount, r.outcome, r.reason, r.winner_bin, r.winner_name,
              r.winner_total, {DISCOUNT}, jsonb_array_length(r.offers), {DATE}, a.id, a.number_anno"""
KEYS = ("id", "lot_number", "name_ru", "name_kz", "customer", "customer_bin", "regions", "divisions",
        "amount", "outcome", "reason", "winner_bin", "winner_name", "winner_total", "discount", "offers",
        "date", "announcement_id", "number_anno")


def search(conn: psycopg.Connection, f: ResultFilters, page: int, now: datetime,
           limit: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
    where, params = f.where(now)
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM {SOURCE} WHERE {where} ORDER BY {ORDER[f.sort]} "
        f"LIMIT {limit} OFFSET {(page - 1) * limit}", params).fetchall()
    total = conn.execute(f"SELECT count(*) FROM {SOURCE} WHERE {where}", params).fetchone()[0]
    return [dict(zip(KEYS, row, strict=True)) for row in rows], total


def summary(conn: psycopg.Connection, f: ResultFilters, now: datetime) -> dict[str, Any]:
    """Сводка по фильтру. Доля несостоявшихся — без условия «исход», иначе она всегда 0 или 100%."""
    where, params = f.where(now, with_outcome=False)
    won, failed, plan, paid, median, offers, single = conn.execute(
        f"""SELECT count(*) FILTER (WHERE r.outcome = 'winner'), count(*) FILTER (WHERE r.outcome = 'failed'),
                   coalesce(sum(l.amount) FILTER (WHERE {DISCOUNT} IS NOT NULL), 0),
                   coalesce(sum(r.winner_total) FILTER (WHERE {DISCOUNT} IS NOT NULL), 0),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY {DISCOUNT}),
                   avg(jsonb_array_length(r.offers)) FILTER (WHERE r.outcome = 'winner'),
                   count(*) FILTER (WHERE r.outcome = 'winner' AND jsonb_array_length(r.offers) = 1)
            FROM {SOURCE} WHERE {where}""", params).fetchone()
    winners = conn.execute(
        f"""SELECT r.winner_bin, max(r.winner_name), count(*), coalesce(sum(r.winner_total), 0)
            FROM {SOURCE} WHERE {where} AND r.outcome = 'winner' AND r.winner_bin IS NOT NULL
            GROUP BY 1 ORDER BY 3 DESC, 4 DESC LIMIT 5""", params).fetchall()
    return {"won": won, "failed": failed, "total": won + failed, "plan": plan, "paid": paid,
            "saved": plan - paid, "median": median, "offers": offers,
            "single": single / won if won else None, "failed_share": failed / (won + failed) if won + failed else None,
            "winners": winners}


def by_group(conn: psycopg.Connection, column: str, days: int, now: datetime, limit: int = 12) -> list:
    """Для «Анализа рынка»: по разделам или областям — лотов с победителем, медианное
    снижение, среднее число заявок, доля лотов с одной заявкой."""
    return conn.execute(
        f"""SELECT g, count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY {DISCOUNT}),
                   avg(jsonb_array_length(r.offers)),
                   avg((jsonb_array_length(r.offers) = 1)::int)
            FROM {SOURCE}, unnest(l.{column}) g
            WHERE r.outcome = 'winner' AND {DATE} >= %(since)s
            GROUP BY g HAVING count(*) >= 10 ORDER BY 2 DESC LIMIT %(limit)s""",
        {"since": now - timedelta(days=days), "limit": limit}).fetchall()
