"""Общее для графиков: ряды по дням/неделям, средние по дням недели, часы.

Даты — по времени Казахстана. Источник данных описывается кортежем
(выражение-значение, FROM, колонка-время, условие WHERE): так одна функция строит
ряд и для объявлений, и для регистраций, и для оплат.
"""

from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

import psycopg

from zakup.goszakup import PORTAL_TZ

PERIODS = (7, 30, 90, 365)
TZ = "Asia/Almaty"


class Source(NamedTuple):
    value: str
    table: str
    column: str
    where: str = "true"


class Period(NamedTuple):
    days: int
    start: date
    end: date  # не включительно
    unit: str  # day | week

    @property
    def bounds(self) -> dict[str, datetime]:
        return {"from": start_ts(self.start), "to": start_ts(self.end)}

    def previous(self) -> "Period":
        return Period(self.days, self.start - timedelta(days=self.days), self.start, self.unit)


def period(days: int, now: datetime) -> Period:
    days = days if days in PERIODS else 30
    end = now.astimezone(PORTAL_TZ).date() + timedelta(days=1)  # сегодня включительно
    return Period(days, end - timedelta(days=days), end, "week" if days > 90 else "day")


def start_ts(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=PORTAL_TZ)


def buckets(p: Period) -> list[date]:
    out, day = [], p.start
    if p.unit == "week":
        day = p.start - timedelta(days=p.start.weekday())
    step = timedelta(days=7 if p.unit == "week" else 1)
    while day < p.end:
        out.append(day)
        day += step
    return out


def labels(p: Period) -> list[str]:
    return [b.isoformat() for b in buckets(p)]


def series(conn: psycopg.Connection, src: Source, p: Period, params: dict | None = None) -> list[float]:
    rows = conn.execute(
        f"""SELECT date_trunc('{p.unit}', {src.column} AT TIME ZONE '{TZ}')::date, {src.value}
            FROM {src.table} WHERE ({src.where}) AND {src.column} >= %(from)s AND {src.column} < %(to)s
            GROUP BY 1""",
        {**p.bounds, **(params or {})},
    ).fetchall()
    got = {b: float(v or 0) for b, v in rows}
    return [round(got.get(b, 0), 2) for b in buckets(p)]


def total(conn: psycopg.Connection, src: Source, p: Period, params: dict | None = None) -> float:
    return float(conn.execute(
        f"""SELECT {src.value} FROM {src.table}
            WHERE ({src.where}) AND {src.column} >= %(from)s AND {src.column} < %(to)s""",
        {**p.bounds, **(params or {})},
    ).fetchone()[0] or 0)


def weekdays(conn: psycopg.Connection, src: Source, start: date, end: date,
             params: dict | None = None) -> list[float]:
    """Среднее за день недели (пн…вс): сумма по дню недели / сколько таких дней было.
    start — не раньше начала истории, иначе пустые дни до неё занижают среднее."""
    rows = dict(conn.execute(
        f"""SELECT extract(isodow FROM {src.column} AT TIME ZONE '{TZ}')::int, {src.value}
            FROM {src.table} WHERE ({src.where}) AND {src.column} >= %(from)s AND {src.column} < %(to)s
            GROUP BY 1""",
        {"from": start_ts(start), "to": start_ts(end), **(params or {})},
    ).fetchall())
    count = [0] * 7
    day = start
    while day < end:
        count[day.weekday()] += 1
        day += timedelta(days=1)
    return [round(float(rows.get(i + 1) or 0) / max(count[i], 1), 1) for i in range(7)]


def hours(conn: psycopg.Connection, src: Source, p: Period, params: dict | None = None) -> list[float]:
    rows = dict(conn.execute(
        f"""SELECT extract(hour FROM {src.column} AT TIME ZONE '{TZ}')::int, {src.value}
            FROM {src.table} WHERE ({src.where}) AND {src.column} >= %(from)s AND {src.column} < %(to)s
            GROUP BY 1""",
        {**p.bounds, **(params or {})},
    ).fetchall())
    return [float(rows.get(h) or 0) for h in range(24)]


def heatmap(conn: psycopg.Connection, src: Source, p: Period, params: dict | None = None) -> list[list[float]]:
    """7×24: день недели × час (сумма за период)."""
    grid = [[0.0] * 24 for _ in range(7)]
    for dow, hour, value in conn.execute(
        f"""SELECT extract(isodow FROM {src.column} AT TIME ZONE '{TZ}')::int,
                   extract(hour FROM {src.column} AT TIME ZONE '{TZ}')::int, {src.value}
            FROM {src.table} WHERE ({src.where}) AND {src.column} >= %(from)s AND {src.column} < %(to)s
            GROUP BY 1, 2""",
        {**p.bounds, **(params or {})},
    ).fetchall():
        grid[dow - 1][hour] = float(value or 0)
    return grid


def change(now: float, before: float) -> int | None:
    """Изменение к прошлому периоду в процентах (None — сравнивать не с чем)."""
    return round((now - before) / before * 100) if before else None


def pack(conn: psycopg.Connection, sources: dict[str, Source], p: Period) -> dict[str, Any]:
    """Ряды, итоги за период и изменение к прошлому — для набора показателей."""
    prev = p.previous()
    out: dict[str, Any] = {"labels": labels(p), "series": {}, "totals": {}, "change": {}}
    for key, src in sources.items():
        out["series"][key] = series(conn, src, p)
        out["totals"][key] = total(conn, src, p)
        out["change"][key] = change(out["totals"][key], total(conn, src, prev))
    return out
