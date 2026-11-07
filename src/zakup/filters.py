"""Фильтр объявлений: одна логика для страницы поиска и для подписок."""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from zakup.goszakup import PORTAL_TZ

# Статусы, при которых объявление ещё принимает заявки или предложения.
STATUSES = {
    240: "Опубликовано (прием ценовых предложений)",
    220: "Опубликовано (прием заявок)",
    210: "Опубликовано",
    230: "Опубликовано (дополнение заявок)",
    280: "Опубликовано (ожидание проведения аукциона)",
}
# Первые две цифры КАТО.
REGIONS = {
    "75": "г. Алматы",
    "19": "Алматинская область",
    "71": "г. Астана",
    "79": "г. Шымкент",
    "33": "область Жетісу",
}
DEADLINE_RULE = "rule"


def deadline_by_rule(today: date) -> date:
    """«Окончание приема по»: через 2 дня, а в субботу и воскресенье — ближайший вторник."""
    if today.weekday() >= 5:
        return today + timedelta(days=(1 - today.weekday()) % 7)
    return today + timedelta(days=2)


@dataclass
class Filters:
    q: str = ""
    statuses: list[int] = field(default_factory=lambda: [240])
    disable_person: bool = True
    regions: list[str] = field(default_factory=lambda: ["75", "19"])
    min_sum: Decimal | None = Decimal(300_000)
    max_sum: Decimal | None = None
    # DEADLINE_RULE — по правилу выше, ГГГГ-ММ-ДД — конкретная дата, "" — без ограничения.
    deadline: str = DEADLINE_RULE
    advance_only: bool = False

    def deadline_date(self, today: date) -> date | None:
        if self.deadline == DEADLINE_RULE:
            return deadline_by_rule(today)
        try:
            return date.fromisoformat(self.deadline) if self.deadline else None
        except ValueError:
            return None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("min_sum", "max_sum"):
            data[key] = str(data[key]) if data[key] is not None else None
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Filters":
        data = dict(data or {})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("min_sum", "max_sum"):
            if key in known:
                known[key] = _decimal(known[key])
        return cls(**known)

    @classmethod
    def from_form(cls, form: dict[str, list[str]]) -> "Filters":
        """Из параметров запроса. Пустая форма (первый заход) — значения по умолчанию."""
        if "applied" not in form:
            return cls()

        def one(name: str) -> str:
            return (form.get(name) or [""])[0].strip()

        return cls(
            q=one("q"),
            statuses=[int(v) for v in form.get("status", []) if v.isdigit() and int(v) in STATUSES],
            disable_person=one("disable_person") == "1",
            regions=[v for v in form.get("region", []) if v in REGIONS],
            min_sum=_decimal(one("min_sum")),
            max_sum=_decimal(one("max_sum")),
            deadline={
                "rule": DEADLINE_RULE,
                "date": one("deadline_date"),
            }.get(one("deadline_mode"), ""),
            advance_only=one("advance_only") == "1",
        )

    def where(self, now: datetime) -> tuple[str, dict[str, Any]]:
        """SQL-условие по таблице announcements (алиас a) и параметры к нему."""
        clauses = ["a.end_date > %(now)s"]
        params: dict[str, Any] = {"now": now}
        if self.statuses:
            clauses.append("a.status_id = ANY(%(statuses)s)")
            params["statuses"] = self.statuses
        if self.disable_person:
            clauses.append("a.disable_person")
        if self.regions:
            clauses.append(
                "EXISTS (SELECT 1 FROM unnest(a.kato) k WHERE left(k, 2) = ANY(%(regions)s))"
            )
            params["regions"] = self.regions
        if self.min_sum is not None:
            clauses.append("a.total_sum >= %(min_sum)s")
            params["min_sum"] = self.min_sum
        if self.max_sum is not None:
            clauses.append("a.total_sum <= %(max_sum)s")
            params["max_sum"] = self.max_sum
        if (deadline := self.deadline_date(now.astimezone(PORTAL_TZ).date())) is not None:
            # «По» включительно: до конца этого дня по времени Астаны.
            clauses.append("a.end_date < %(deadline_end)s")
            params["deadline_end"] = datetime.combine(
                deadline + timedelta(days=1), time.min, PORTAL_TZ
            )
        if self.advance_only:
            clauses.append("a.prepayment_max > 0")
        if self.q:
            clauses.append(
                """EXISTS (SELECT 1 FROM lots l WHERE l.trd_buy_id = a.id
                           AND l.tsv @@ websearch_to_tsquery('russian', %(q)s))"""
            )
            params["q"] = self.q
        return " AND ".join(clauses), params

    def describe(self) -> str:
        parts = []
        if self.q:
            parts.append(f"«{self.q}»")
        if self.statuses:
            parts.append(", ".join(STATUSES[s] for s in self.statuses if s in STATUSES))
        if self.disable_person:
            parts.append("среди организаций инвалидов")
        if self.regions:
            parts.append(", ".join(REGIONS[r] for r in self.regions if r in REGIONS))
        if self.min_sum is not None:
            parts.append(f"от {self.min_sum:,.0f} ₸".replace(",", " "))
        if self.max_sum is not None:
            parts.append(f"до {self.max_sum:,.0f} ₸".replace(",", " "))
        if self.deadline == DEADLINE_RULE:
            parts.append("окончание через 2 дня (в выходные — по вторник)")
        elif self.deadline:
            parts.append(f"окончание по {self.deadline}")
        if self.advance_only:
            parts.append("с авансом")
        return "; ".join(parts)


# Аванс в приоритете: сначала с наибольшим авансом, потом кто раньше заканчивается.
ORDER_BY = "a.prepayment_max DESC NULLS LAST, a.end_date"


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
