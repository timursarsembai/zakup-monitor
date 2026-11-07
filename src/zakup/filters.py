"""Фильтр поиска: одна логика для страниц «Лоты», «Объявления» и для подписок.

Поля повторяют поиск портала. На странице «Лоты» (scope="lots") сумма, аванс,
место поставки и статус проверяются у лота, а признак «среди организаций
инвалидов», способ и даты — у его объявления. На странице «Объявления»
(scope="announcements") всё проверяется у объявления.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from zakup.goszakup import PORTAL_TZ

# Справочник статусов портала (ref_buy_status / ref_lots_status), только те, что
# встречаются на практике. Коды у объявлений и лотов общие, кроме итогов.
STATUS_NAMES = {
    240: "Опубликовано (прием ценовых предложений)",
    220: "Опубликовано (прием заявок)",
    210: "Опубликовано",
    230: "Опубликовано (дополнение заявок)",
    245: "Опубликовано (проверка заявок)",
    280: "Опубликовано (ожидание проведения аукциона)",
    290: "Опубликовано (проведение аукциона)",
    190: "Изменена документация",
    250: "Рассмотрение заявок",
    260: "Рассмотрение дополнений заявок",
    270: "Заполнение условных скидок",
    320: "Формирование протокола допуска",
    325: "Формирование протокола промежуточных итогов",
    330: "Формирование протокола итогов",
    420: "Приостановлено",
    440: "На обжаловании",
    444: "Ожидание камерального контроля",
    445: "Ожидание проведения контроля качества",
    450: "Отправлено на контроль качества",
    460: "Пройдено контроль качества",
    510: "Пересмотр итогов",
    350: "Завершено",
    360: "Закупка состоялась",
    370: "Закупка не состоялась",
    410: "Отказ от закупки",
    430: "Отменено",
}
# Итоговые статусы есть только у объявлений (350) или только у лотов (360, 370).
ANNOUNCEMENT_ONLY = {350}
LOT_ONLY = {360, 370}
OPEN_STATUSES = [190, 210, 220, 230, 240, 245, 280, 290]
# Этапы — понятные группы статусов. Для объявлений «есть победитель» и «не
# состоялась» различаются по лотам: у завершённого объявления бывают и те и другие.
STAGES = {
    "open": "Идёт приём заявок",
    "review": "Рассмотрение заявок",
    "done": "Итоги подведены, есть победитель",
    "failed": "Не состоялась",
    "cancelled": "Отменено, отказ",
}
_STAGE_STATUSES = {
    "open": OPEN_STATUSES,
    "review": [250, 260, 270, 295, 300, 310, 320, 325, 330, 420, 440, 444, 445, 450, 460,
               510, 520, 530, 540, 550],
    "cancelled": [410, 430],
}
_HAS_WINNER = "EXISTS (SELECT 1 FROM lots w WHERE w.trd_buy_id = a.id AND w.status_id = 360)"
# Первые две цифры КАТО. Сначала города и области владельца, дальше по алфавиту.
REGIONS = {
    "75": "г. Алматы",
    "19": "Алматинская область",
    "71": "г. Астана",
    "79": "г. Шымкент",
    "10": "область Абай",
    "11": "Акмолинская область",
    "15": "Актюбинская область",
    "23": "Атырауская область",
    "63": "Восточно-Казахстанская область",
    "31": "Жамбылская область",
    "33": "область Жетісу",
    "27": "Западно-Казахстанская область",
    "35": "Карагандинская область",
    "39": "Костанайская область",
    "43": "Кызылординская область",
    "47": "Мангистауская область",
    "55": "Павлодарская область",
    "59": "Северо-Казахстанская область",
    "61": "Туркестанская область",
    "62": "область Ұлытау",
}
# Способы закупки, сгруппированные как на портале: ключ — id справочника.
METHODS = {
    "zcp": ("Запрос ценовых предложений", [3, 126]),
    "single": ("Из одного источника", [6, 125]),
    "contest": ("Конкурс", [2, 120, 121, 188, 201]),
    "tender": ("Тендер", [124]),
    "auction": ("Аукцион", [78]),
    "special": ("Особый порядок", [130]),
    "other": ("Прочие (жилище, соцзаказ)", [50, 52]),
}
METHOD_NAMES = {i: name for name, ids in METHODS.values() for i in ids}
SUBJECTS = {1: "Товары", 2: "Работы", 3: "Услуги"}
DISABLE_PERSON = {"only": "Только среди организаций инвалидов", "exclude": "Без них", "": "Все"}
SORTS = {
    "advance": "Аванс, потом срок",
    "end": "Окончание приёма — раньше",
    "amount_desc": "Сумма — больше",
    "amount_asc": "Сумма — меньше",
    "published": "Публикация — новее",
}
_LOT_ORDER = {
    "advance": "l.prepayment DESC NULLS LAST, a.end_date, a.id, l.lot_number",
    "end": "a.end_date, a.id, l.lot_number",
    "amount_desc": "l.amount DESC NULLS LAST, a.end_date, l.id",
    "amount_asc": "l.amount NULLS LAST, a.end_date, l.id",
    "published": "a.publish_date DESC NULLS LAST, a.id DESC, l.lot_number",
}
_ANNOUNCEMENT_ORDER = {
    "advance": "a.prepayment_max DESC NULLS LAST, a.end_date, a.id",
    "end": "a.end_date, a.id",
    "amount_desc": "a.total_sum DESC NULLS LAST, a.end_date, a.id",
    "amount_asc": "a.total_sum NULLS LAST, a.end_date, a.id",
    "published": "a.publish_date DESC NULLS LAST, a.id DESC",
}
DEADLINE_RULE = "rule"


def deadline_by_rule(today: date) -> date:
    """«Окончание приема по»: через 2 дня, а в субботу и воскресенье — ближайший вторник."""
    if today.weekday() >= 5:
        return today + timedelta(days=(1 - today.weekday()) % 7)
    return today + timedelta(days=2)


def fin_years(today: date) -> list[int]:
    return [today.year + 1, today.year, today.year - 1]


@dataclass
class Filters:
    q: str = ""
    exclude: str = ""
    number: str = ""
    lot_number: str = ""
    enstru: str = ""
    customer: str = ""
    stages: list[str] = field(default_factory=list)
    statuses: list[int] = field(default_factory=lambda: [240])
    # "only" — только среди организаций инвалидов, "exclude" — без них, "" — все.
    disable_person: str = "only"
    regions: list[str] = field(default_factory=lambda: ["75", "19"])
    methods: list[str] = field(default_factory=list)
    subjects: list[int] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    min_sum: Decimal | None = Decimal(300_000)
    max_sum: Decimal | None = None
    # «Окончание по». DEADLINE_RULE — по правилу выше, ГГГГ-ММ-ДД — дата, "" — без ограничения.
    deadline: str = DEADLINE_RULE
    end_from: str = ""
    start_from: str = ""
    start_to: str = ""
    published_from: str = ""
    published_to: str = ""
    itogi_from: str = ""
    itogi_to: str = ""
    advance_only: bool = False
    sort: str = "advance"

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
        # До 2026-11-08 признак был галочкой.
        if isinstance(known.get("disable_person"), bool):
            known["disable_person"] = "only" if known["disable_person"] else ""
        return cls(**known)

    @classmethod
    def from_form(cls, form: dict[str, list[str]]) -> "Filters":
        """Из параметров запроса. Пустая форма (первый заход) — значения по умолчанию."""
        if "applied" not in form:
            return cls()

        def one(name: str) -> str:
            return (form.get(name) or [""])[0].strip()

        def ints(name: str, allowed) -> list[int]:
            return [int(v) for v in form.get(name, []) if v.isdigit() and int(v) in allowed]

        disable = one("disable_person")
        return cls(
            q=one("q"),
            exclude=one("exclude"),
            number=one("number"),
            lot_number=one("lot_number"),
            enstru=one("enstru"),
            customer=one("customer"),
            stages=[v for v in form.get("stage", []) if v in STAGES],
            statuses=ints("status", STATUS_NAMES),
            # Старые ссылки и сохранённые формы: галочка присылала "1".
            disable_person="only" if disable == "1" else disable if disable in DISABLE_PERSON else "",
            regions=[v for v in form.get("region", []) if v in REGIONS],
            methods=[v for v in form.get("method", []) if v in METHODS],
            subjects=ints("subject", SUBJECTS),
            years=ints("year", range(2000, 2100)),
            min_sum=_decimal(one("min_sum")),
            max_sum=_decimal(one("max_sum")),
            # Заполненная дата «по» важнее выбранного режима.
            deadline=_date(one("deadline_date"))
            or (DEADLINE_RULE if one("deadline_mode") == "rule" else ""),
            end_from=_date(one("end_from")),
            start_from=_date(one("start_from")),
            start_to=_date(one("start_to")),
            published_from=_date(one("published_from")),
            published_to=_date(one("published_to")),
            itogi_from=_date(one("itogi_from")),
            itogi_to=_date(one("itogi_to")),
            advance_only=one("advance_only") == "1",
            sort=one("sort") if one("sort") in SORTS else "advance",
        )

    def only_open(self) -> bool:
        """Выбраны только статусы приёма заявок: тогда объявления с прошедшим сроком
        отсекаем — статус на портале меняется с опозданием."""
        chosen, stages = set(self.statuses), set(self.stages)
        return bool(chosen or stages) and chosen <= set(OPEN_STATUSES) and stages <= {"open"}

    def _status_clause(self, scope: str, params: dict[str, Any]) -> str | None:
        lots = scope == "lots"
        status = "l.status_id" if lots else "a.status_id"
        parts = []
        statuses = [s for s in self.statuses if s not in (ANNOUNCEMENT_ONLY if lots else LOT_ONLY)]
        if statuses:
            parts.append(f"{status} = ANY(%(statuses)s)")
            params["statuses"] = statuses
        for stage in self.stages:
            if stage in _STAGE_STATUSES:
                parts.append(f"{status} = ANY(%(stage_{stage})s)")
                params[f"stage_{stage}"] = _STAGE_STATUSES[stage]
            elif lots:
                parts.append(f"l.status_id = {360 if stage == 'done' else 370}")
            else:
                negate = "" if stage == "done" else "NOT "
                parts.append(f"(a.status_id = 350 AND {negate}{_HAS_WINNER})")
        return "(" + " OR ".join(parts) + ")" if parts else None

    def where(self, now: datetime, scope: str = "lots") -> tuple[str, dict[str, Any]]:
        """SQL-условие и параметры. scope="lots": алиасы l и a; "announcements": только a."""
        lots = scope == "lots"
        clauses = ["l.trd_buy_id = a.id"] if lots else []
        params: dict[str, Any] = {"now": now}
        if self.only_open():
            clauses.append("a.end_date > %(now)s")
        if status := self._status_clause(scope, params):
            clauses.append(status)
        if self.disable_person == "only":
            clauses.append("a.disable_person")
        elif self.disable_person == "exclude":
            clauses.append("a.disable_person IS NOT TRUE")
        if self.methods:
            clauses.append("a.trade_method_id = ANY(%(methods)s)")
            params["methods"] = [i for key in self.methods for i in METHODS[key][1]]
        if self.subjects:
            clauses.append("a.subject_type_id = ANY(%(subjects)s)")
            params["subjects"] = self.subjects
        if self.years:
            clauses.append("a.fin_years && %(years)s::integer[]")
            params["years"] = self.years
        if self.number:
            # «17914810-1» или просто «17914810».
            clauses.append("(a.number_anno = %(number)s OR a.id::text = %(number_id)s)")
            params["number"] = self.number
            params["number_id"] = self.number.split("-")[0]
        if self.lot_number and lots:
            clauses.append("l.lot_number ILIKE %(lot_number)s")
            params["lot_number"] = _like(self.lot_number) + "%"
        if self.enstru and lots:
            clauses.append("EXISTS (SELECT 1 FROM unnest(l.enstru) e WHERE e LIKE %(enstru)s)")
            params["enstru"] = _like(self.enstru) + "%"
        if self.customer:
            lot_part = " OR l.customer_{} {} %(customer)s" if lots else ""
            if self.customer.isdigit():
                clauses.append("(a.customer_bin = %(customer)s OR a.org_bin = %(customer)s"
                               + lot_part.format("bin", "=") + ")")
                params["customer"] = self.customer
            else:
                clauses.append("(a.customer_name_ru ILIKE %(customer)s"
                               " OR a.org_name_ru ILIKE %(customer)s"
                               + lot_part.format("name_ru", "ILIKE") + ")")
                params["customer"] = "%" + _like(self.customer) + "%"
        if self.regions:
            # Место поставки лота; если у лота его нет — объявления.
            kato = "coalesce(nullif(l.kato, '{}'), a.kato)" if lots else "a.kato"
            clauses.append(f"""EXISTS (SELECT 1 FROM unnest({kato}) k
                                       WHERE left(k, 2) = ANY(%(regions)s))""")
            params["regions"] = self.regions
        amount = "l.amount" if lots else "a.total_sum"
        if self.min_sum is not None:
            clauses.append(f"{amount} >= %(min_sum)s")
            params["min_sum"] = self.min_sum
        if self.max_sum is not None:
            clauses.append(f"{amount} <= %(max_sum)s")
            params["max_sum"] = self.max_sum
        if (deadline := self.deadline_date(now.astimezone(PORTAL_TZ).date())) is not None:
            # «По» включительно: до конца этого дня по времени Астаны.
            clauses.append("a.end_date < %(deadline_end)s")
            params["deadline_end"] = datetime.combine(
                deadline + timedelta(days=1), time.min, PORTAL_TZ
            )
        for column, start, end in (("end_date", self.end_from, ""),
                                   ("start_date", self.start_from, self.start_to),
                                   ("publish_date", self.published_from, self.published_to),
                                   ("itogi_date", self.itogi_from, self.itogi_to)):
            if start:
                clauses.append(f"a.{column} >= %({column}_from)s")
                params[f"{column}_from"] = _day_start(start)
            if end:
                clauses.append(f"a.{column} < %({column}_to)s")
                params[f"{column}_to"] = _day_start(end, 1)
        if self.advance_only:
            clauses.append("l.prepayment > 0" if lots else "a.prepayment_max > 0")
        tsv = "l.tsv" if lots else "a.tsv"
        if self.q:
            clauses.append(f"{tsv} @@ websearch_to_tsquery('russian', %(q)s)")
            params["q"] = self.q
        if words := self.exclude.replace(",", " ").split():
            # Любое из слов исключает запись.
            clauses.append(f"NOT {tsv} @@ websearch_to_tsquery('russian', %(exclude)s)")
            params["exclude"] = " or ".join(words)
        return " AND ".join(clauses) or "true", params

    def order_by(self, scope: str = "lots") -> str:
        orders = _LOT_ORDER if scope == "lots" else _ANNOUNCEMENT_ORDER
        return orders.get(self.sort, orders["advance"])

    def describe(self) -> str:
        parts = []
        if self.q:
            parts.append(f"«{self.q}»")
        if self.exclude:
            parts.append(f"без слов «{self.exclude}»")
        if self.number:
            parts.append(f"объявление № {self.number}")
        if self.lot_number:
            parts.append(f"лот № {self.lot_number}")
        if self.enstru:
            parts.append(f"ЕНС ТРУ {self.enstru}")
        if self.stages:
            parts.append(", ".join(STAGES[s] for s in self.stages if s in STAGES))
        if self.statuses:
            parts.append(", ".join(STATUS_NAMES[s] for s in self.statuses if s in STATUS_NAMES))
        if self.disable_person == "only":
            parts.append("среди организаций инвалидов")
        elif self.disable_person == "exclude":
            parts.append("без закупок среди организаций инвалидов")
        if self.methods:
            parts.append(", ".join(METHODS[m][0] for m in self.methods if m in METHODS))
        if self.subjects:
            parts.append(", ".join(SUBJECTS[s] for s in self.subjects if s in SUBJECTS).lower())
        if self.years:
            parts.append("финансовый год " + ", ".join(map(str, self.years)))
        if self.customer:
            parts.append(f"заказчик «{self.customer}»")
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
        for label, start, end in (("окончание", self.end_from, ""),
                                  ("начало приёма", self.start_from, self.start_to),
                                  ("опубликовано", self.published_from, self.published_to),
                                  ("протокол итогов", self.itogi_from, self.itogi_to)):
            if start or end:
                parts.append(f"{label} {start or '…'} — {end or '…'}")
        if self.advance_only:
            parts.append("с авансом")
        return "; ".join(parts)


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat() if value else ""
    except ValueError:
        return ""


def _day_start(value: str, plus_days: int = 0) -> datetime:
    day = date.fromisoformat(value) + timedelta(days=plus_days)
    return datetime.combine(day, time.min, PORTAL_TZ)


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
