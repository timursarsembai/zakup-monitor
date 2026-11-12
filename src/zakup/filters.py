"""Фильтр поиска: одна логика для страниц «Лоты», «Объявления» и для подписок.

Поля повторяют поиск портала. На странице «Лоты» (scope="lots") сумма, аванс,
место поставки и статус проверяются у лота, а признак «среди организаций
инвалидов», способ и даты — у его объявления. На странице «Объявления»
(scope="announcements") всё проверяется у объявления.
"""

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from zakup.goszakup import PORTAL_TZ
from zakup.i18n import gettext, language

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
    # 10 000+ — способы Самрук-Казыны (zakup.skk.METHOD_OFFSET): ЗЦП, ЗЦП на понижение,
    # открытый тендер, открытый тендер на понижение.
    "zcp": ("Запрос ценовых предложений", [3, 126, 10012, 10062, 10063]),
    "single": ("Из одного источника", [6, 125]),
    "contest": ("Конкурс", [2, 120, 121, 188, 201]),
    "tender": ("Тендер", [124, 10065, 10066]),
    "auction": ("Аукцион", [78]),
    "special": ("Особый порядок", [130]),
    "other": ("Прочие (жилище, соцзаказ)", [50, 52]),
}
METHOD_NAMES = {i: name for name, ids in METHODS.values() for i in ids}
SUBJECTS = {1: "Товары", 2: "Работы", 3: "Услуги"}
# Система закупок (announcements.system, миграция 0033): госзакупки (OWS) и Самрук-Казына.
SYSTEMS = {"": "Все системы", "goszakup": "Госзакупки", "skk": "Самрук-Казына"}
SYSTEM_IDS = {"goszakup": 1, "skk": 3}
REPEATS = {"": "Все", "any": "Повторная закупка", "failed": "Повторная: прошлая не состоялась"}
OWN_SUPPLIER = {"": "Все", "hide": "Скрыть заказчиков со «своим» поставщиком",
                "only": "Только заказчики со «своим» поставщиком"}
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
# Выражение индекса lot_specs_text_tsv (миграция 0024) — в запросах ровно так же, иначе индекс
# не используется.
SPEC_TSV = "to_tsvector('russian', coalesce(s.text, ''))"
# «Окончание с» = сегодня: сдвигается каждый день, как и правило срока.
TODAY = "today"


def deadline_by_rule(today: date) -> date:
    """Последний день срока «в течение 2 рабочих дней»: пн → ср, ср → пт, чт → пн,
    пт, сб и вс → вт. Праздники не учитываются."""
    day, left = today, 2
    while left:
        day += timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day


def fin_years(today: date) -> list[int]:
    return [today.year + 1, today.year, today.year - 1]


@dataclass
class Filters:
    q: str = ""
    # Слова искать и в тексте техспецификации лота (lot_specs, индекс миграции 0024).
    in_specs: bool = False
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
    # Категории — разделы КПВЭД, первые две цифры кода ЕНС ТРУ (zakup.catalog.DIVISIONS).
    categories: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    subjects: list[int] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    # "" — все, "goszakup" — госзакупки, "skk" — Самрук-Казына (SYSTEMS).
    system: str = ""
    min_sum: Decimal | None = Decimal(300_000)
    max_sum: Decimal | None = None
    # Окончание приёма. DEADLINE_RULE — от сегодня по день по правилу выше (каждый день
    # сдвигается сам — для подписок), ГГГГ-ММ-ДД — «по» эту дату, "" — без ограничения.
    deadline: str = DEADLINE_RULE
    end_from: str = TODAY
    start_from: str = ""
    start_to: str = ""
    published_from: str = ""
    published_to: str = ""
    itogi_from: str = ""
    itogi_to: str = ""
    advance_only: bool = False
    # Размер аванса, % от суммы: 0–100 — без ограничения.
    advance_min: int = 30
    advance_max: int = 100
    # Оценки лота (таблица lot_scores, zakup.scores):
    # плановая цена за единицу выше цены победителей на N% и больше (0 — без условия);
    market_above: int = 0
    # повторная закупка: "any" — заказчик уже объявлял этот лот, "failed" — и прошлая не состоялась;
    repeat: str = ""
    # «свой поставщик» заказчика по такому товару: "hide" — скрыть, "only" — только такие;
    own_supplier: str = ""
    # доля несостоявшихся закупок заказчика не больше N% (100 — без условия);
    customer_failed_max: int = 100
    # денег на заморозку (обеспечение заявки и договора с авансом) не больше, ₸.
    freeze_max: Decimal | None = None
    sort: str = "advance"

    @classmethod
    def public(cls) -> "Filters":
        """Для гостей и клиентов: все закупки с открытым приёмом заявок, без ограничений."""
        return cls(statuses=[], stages=["open"], disable_person="", regions=[], min_sum=None,
                   deadline="", advance_min=0)

    def deadline_date(self, today: date) -> date | None:
        if self.deadline == DEADLINE_RULE:
            return deadline_by_rule(today)
        try:
            return date.fromisoformat(self.deadline) if self.deadline else None
        except ValueError:
            return None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("min_sum", "max_sum", "freeze_max"):
            data[key] = str(data[key]) if data[key] is not None else None
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Filters":
        data = dict(data or {})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("min_sum", "max_sum", "freeze_max"):
            if key in known:
                known[key] = _decimal(known[key])
        # Сохранённые до 2026-11-08 подписки: аванс не ограничивали.
        known.setdefault("advance_min", 0)
        known.setdefault("end_from", "")
        # До 2026-11-08 признак был галочкой.
        if isinstance(known.get("disable_person"), bool):
            known["disable_person"] = "only" if known["disable_person"] else ""
        return cls(**known)

    @classmethod
    def fresh(cls, staff: bool = False) -> "Filters":
        """Первый заход на поиск — свежая сводка: опубликованные сегодня (в выходные — с
        пятницы), с открытым приёмом заявок, все регионы, без признаков и аванса. Клиентам и
        гостям — сначала новые; сотрудникам — их привычная сортировка."""
        return replace(cls.public(), published_from=TODAY, sort="advance" if staff else "published")

    @classmethod
    def from_form(cls, form: dict[str, list[str]], staff: bool = True) -> "Filters":
        """Из параметров запроса. Пустая форма (первый заход) — свежая сводка (fresh)."""
        if "applied" not in form:
            return cls.fresh(staff)

        def one(name: str) -> str:
            return (form.get(name) or [""])[0].strip()

        def ints(name: str, allowed) -> list[int]:
            return [int(v) for v in form.get(name, []) if v.isdigit() and int(v) in allowed]

        disable = one("disable_person")
        return cls(
            q=one("q"),
            in_specs=one("in_specs") == "1",
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
            categories=[v for v in form.get("category", []) if re.fullmatch(r"\d\d", v)][:100],
            methods=[v for v in form.get("method", []) if v in METHODS],
            subjects=ints("subject", SUBJECTS),
            years=ints("year", range(2000, 2100)),
            system=one("system") if one("system") in SYSTEM_IDS else "",
            min_sum=_decimal(one("min_sum")),
            max_sum=_decimal(one("max_sum")),
            # Заполненная дата «по» важнее выбранного режима.
            deadline=_date(one("deadline_date"))
            or (DEADLINE_RULE if one("deadline_mode") == "rule" else ""),
            # «Только с открытым приёмом заявок» — окончание с сегодня, каждый день заново.
            # Своя дата в поле «с» важнее галочки.
            # Старые ссылки: сегодняшняя дата в «Окончание с» значила «с сегодня».
            end_from=TODAY if one("end_from") in (TODAY, _today().isoformat())
            else (_date(one("end_from")) or (TODAY if one("open_now") == "1" else "")),
            start_from=_date(one("start_from")),
            start_to=_date(one("start_to")),
            # «Только опубликованные сегодня» — каждый день заново.
            published_from=_date(one("published_from"))
            or (TODAY if one("today_only") == "1" or one("published_from") == TODAY else ""),
            published_to=_date(one("published_to")),
            itogi_from=_date(one("itogi_from")),
            itogi_to=_date(one("itogi_to")),
            advance_only=one("advance_only") == "1",
            advance_min=_percent(one("advance_min"), 0),
            advance_max=_percent(one("advance_max"), 100),
            market_above=_percent(one("market_above"), 0),
            repeat=one("repeat") if one("repeat") in REPEATS else "",
            own_supplier=one("own_supplier") if one("own_supplier") in OWN_SUPPLIER else "",
            customer_failed_max=_percent(one("customer_failed_max"), 100),
            freeze_max=_decimal(one("freeze_max")),
            sort=one("sort") if one("sort") in SORTS else "advance",
        )

    def active_count(self) -> int:
        """Сколько условий задано (для кнопки «Фильтры» на телефоне)."""
        flags = [self.q, self.exclude, self.number, self.lot_number, self.enstru, self.customer,
                 self.regions, self.categories, self.methods, self.subjects, self.years, self.disable_person,
                 self.system,
                 self.min_sum is not None or self.max_sum is not None, self.deadline,
                 self.start_from or self.start_to, self.published_from or self.published_to,
                 self.end_from,  # «Только с открытым приёмом» (и «Окончание с») — тоже условие
                 self.itogi_from or self.itogi_to,
                 self.advance_only or self.advance_min > 0 or self.advance_max < 100,
                 self.market_above, self.repeat, self.own_supplier, self.customer_failed_max < 100,
                 self.freeze_max is not None,
                 self.statuses or (self.stages and self.stages != ["open"])]
        return sum(1 for flag in flags if flag)

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
        if self.statuses and not statuses and not self.stages:
            # Выбраны только статусы, которых у этой страницы нет (350 у лотов,
            # 360/370 у объявлений): ничего не найдено, а не «все статусы».
            return "false"
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

    def where(self, now: datetime, scope: str = "lots",
              subscription: bool = False) -> tuple[str, dict[str, Any]]:
        """SQL-условие и параметры. scope="lots": алиасы l и a; "announcements": только a.

        subscription — прежний параметр, срок «в течение 2 рабочих дней» теперь везде
        окном от сегодня до дня по правилу.
        """
        lots = scope == "lots"
        # Открытое объявление — только когда в его подробностях пришли лоты (lots_ready): API
        # портала отдаёт их с задержкой, а пустая страница объявления выглядит как ошибка сайта.
        # Так же и рассылка: подписки-фильтры строятся на этом условии.
        clauses = ["(a.lots_ready OR a.end_date <= %(now)s OR a.end_date IS NULL)"]
        if lots:
            clauses.insert(0, "l.trd_buy_id = a.id")
        params: dict[str, Any] = {"now": now}
        for part in (self._where_kind, self._where_ids, self._where_place_and_sum,
                     self._where_dates, self._where_advance, self._where_words, self._where_scores):
            part(clauses, params, lots, now)
        return " AND ".join(clauses) or "true", params

    def _where_kind(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Открытые, статус, организации инвалидов, способ, вид, год."""
        if self.only_open():
            clauses.append("a.end_date > %(now)s")
        if status := self._status_clause("lots" if lots else "announcements", params):
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
        if self.system in SYSTEM_IDS:
            # Число — из своего справочника, не из запроса: константой в тексте условия
            # планировщик видит частичный индекс announcements_other_systems.
            clauses.append(f"a.system = {SYSTEM_IDS[self.system]}")
        if self.years:
            clauses.append("a.fin_years && %(years)s::integer[]")
            params["years"] = self.years

    def _where_ids(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Номер объявления и лота, код ЕНС ТРУ, заказчик."""
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
        if self.categories:
            # Лот — по любому своему коду ЕНС ТРУ; объявление — если такой есть у его лота.
            # Готовый список разделов с GIN-индексом (миграция 0019, zakup.details.codes).
            clauses.append(f"{'l' if lots else 'a'}.divisions && %(categories)s::text[]")
            params["categories"] = self.categories
        if self.customer:
            by_bin = self.customer.isdigit()
            column, op = ("bin", "=") if by_bin else ("name_ru", "ILIKE")
            params["customer"] = self.customer if by_bin else "%" + _like(self.customer) + "%"
            own = f"(a.customer_{column} {op} %(customer)s OR a.org_{column} {op} %(customer)s)"
            if lots and by_bin:
                # БИН — заказчик лота или заказчик/организатор объявления (централизованные
                # закупки): выборки по индексам маленькие, одним списком id.
                clauses.append("""l.id IN (SELECT x.id FROM lots x WHERE x.customer_bin = %(customer)s
                                           UNION ALL
                                           SELECT x.id FROM lots x JOIN announcements a2 ON a2.id = x.trd_buy_id
                                           WHERE a2.customer_bin = %(customer)s OR a2.org_bin = %(customer)s)""")
            elif lots:
                # Название — заказчик лота (он есть у каждого лота, в совместных закупках —
                # свой): одна таблица и trigram-индекс. Условие «или у объявления» читало всю
                # таблицу лотов (до 5 с на частых словах вроде «акимат»).
                clauses.append(f"l.customer_{column} {op} %(customer)s")
            else:
                clauses.append(own)

    def _where_place_and_sum(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Регион поставки и сумма."""
        if self.regions:
            # Место поставки лота; если у лота его нет — объявления. Готовый список областей
            # с GIN-индексом (миграция 0019, zakup.details.codes).
            clauses.append(f"{'l' if lots else 'a'}.regions && %(regions)s::text[]")
            params["regions"] = self.regions
        amount = "l.amount" if lots else "a.total_sum"
        if self.min_sum is not None:
            clauses.append(f"{amount} >= %(min_sum)s")
            params["min_sum"] = self.min_sum
        if self.max_sum is not None:
            clauses.append(f"{amount} <= %(max_sum)s")
            params["max_sum"] = self.max_sum

    def _where_dates(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Срок по правилу и даты (окончание, начало, публикация, итоги)."""
        if (deadline := self.deadline_date(now.astimezone(PORTAL_TZ).date())) is not None:
            # «По» включительно: до конца этого дня по времени Астаны. По правилу —
            # «в течение 2 рабочих дней»: от сегодня до этого дня включительно.
            clauses.append("a.end_date < %(deadline_end)s")
            params["deadline_end"] = datetime.combine(
                deadline + timedelta(days=1), time.min, PORTAL_TZ
            )
        today = now.astimezone(PORTAL_TZ).date().isoformat()
        end_from = today if self.end_from == TODAY else self.end_from
        # «Опубликованные сегодня»: в выходные — с пятницы (портал в выходные почти не публикует).
        published_from = last_workday(now.astimezone(PORTAL_TZ).date()).isoformat() \
            if self.published_from == TODAY else self.published_from
        for column, start, end in (("end_date", end_from, ""),
                                   ("start_date", self.start_from, self.start_to),
                                   ("publish_date", published_from, self.published_to),
                                   ("itogi_date", self.itogi_from, self.itogi_to)):
            if start:
                clauses.append(f"a.{column} >= %({column}_from)s")
                params[f"{column}_from"] = _day_start(start)
            if end:
                clauses.append(f"a.{column} < %({column}_to)s")
                params[f"{column}_to"] = _day_start(end, 1)

    def _where_advance(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Аванс."""
        prepayment = "l.prepayment" if lots else "a.prepayment_max"
        if self.advance_only:
            clauses.append(f"{prepayment} > 0")
        low, high = sorted((self.advance_min, self.advance_max))
        if low > 0:
            clauses.append(f"{prepayment} >= %(advance_min)s")
            params["advance_min"] = low
        if high < 100:
            clauses.append(f"coalesce({prepayment}, 0) <= %(advance_max)s")
            params["advance_max"] = high

    def _where_scores(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Оценки лота (lot_scores). Лоты без оценок (служба ещё не посчитала) под условия
        «выше рынка», «повторная», «только свой поставщик», «надёжность», «заморозка» не
        попадают; под «скрыть своего поставщика» — попадают."""
        need = []
        if self.market_above:
            need.append("s.market_diff >= %(market_above)s")
            params["market_above"] = self.market_above / 100
        if self.repeat == "any":
            need.append("s.repeats > 0")
        elif self.repeat == "failed":
            need.append("s.repeats_failed > 0")
        if self.own_supplier == "only":
            need.append("s.same_winner_bin IS NOT NULL")
        if self.customer_failed_max < 100:
            need.append("coalesce(s.customer_failed_share, 0) <= %(customer_failed_max)s")
            params["customer_failed_max"] = self.customer_failed_max / 100
        if self.freeze_max is not None:
            need.append("coalesce(s.freeze_total, 0) <= %(freeze_max)s")
            params["freeze_max"] = self.freeze_max
        # Объявление — если условию отвечает хотя бы один его лот.
        source = ("lot_scores s WHERE s.lot_id = l.id" if lots else
                  "lots l2 JOIN lot_scores s ON s.lot_id = l2.id WHERE l2.trd_buy_id = a.id")
        if need:
            clauses.append(f"EXISTS (SELECT 1 FROM {source} AND {' AND '.join(need)})")
        if self.own_supplier == "hide":
            clauses.append(f"NOT EXISTS (SELECT 1 FROM {source} AND s.same_winner_bin IS NOT NULL)")

    def score_labels(self) -> list[tuple[str, dict[str, Any]]]:
        """Условия по оценкам лота словами и чем их сбросить (для selected и describe)."""
        _ = gettext
        items = []
        if self.market_above:
            items.append((_("цена выше рынка на {pct}%+", pct=self.market_above), {"market_above": 0}))
        if self.repeat:
            items.append((_(REPEATS[self.repeat]).lower(), {"repeat": ""}))
        if self.own_supplier:
            items.append((_(OWN_SUPPLIER[self.own_supplier]).lower(), {"own_supplier": ""}))
        if self.customer_failed_max < 100:
            items.append((_("у заказчика не состоялось не больше {pct}%", pct=self.customer_failed_max),
                          {"customer_failed_max": 100}))
        if self.freeze_max is not None:
            items.append((_("заморозить не больше {sum} ₸", sum=f"{self.freeze_max:,.0f}".replace(",", " ")),
                          {"freeze_max": None}))
        return items

    def _where_words(self, clauses: list[str], params: dict[str, Any], lots: bool, now: datetime) -> None:
        """Слова (рус. с морфологией и каз.) и слова-исключения."""
        # Слова ищем и в русских названиях (с морфологией), и в казахских.
        tsv, tsv_kk = ("l.tsv", "l.tsv_kk") if lots else ("a.tsv", "a.tsv_kk")
        if self.q:
            words = f"""({tsv} @@ websearch_to_tsquery('russian', %(q)s)
                         OR {tsv_kk} @@ websearch_to_tsquery('simple', %(q)s))"""
            if self.in_specs:
                # И текст техспецификации (индекс 0024). Одним списком id: «или» между
                # названием и спецификацией в соединении читало всю таблицу (4 с), а так
                # обе выборки идут по своим индексам.
                spec = f"SELECT s.lot_id FROM lot_specs s WHERE {SPEC_TSV} @@ websearch_to_tsquery('russian', %(q)s)"
                if lots:
                    clauses.append(f"""l.id IN (SELECT l.id FROM lots l WHERE {words} UNION {spec})""")
                else:
                    clauses.append(f"""a.id IN (SELECT a.id FROM announcements a WHERE {words}
                                                UNION SELECT l2.trd_buy_id FROM lots l2 WHERE l2.id IN ({spec}))""")
            else:
                clauses.append(words)
            params["q"] = _OR.sub(" or ", _OR_KK.sub(" or ", self.q))
        if words := self.exclude.replace(",", " ").split():
            # Любое из слов исключает запись.
            clauses.append(f"""NOT ({tsv} @@ websearch_to_tsquery('russian', %(exclude)s)
                                    OR {tsv_kk} @@ websearch_to_tsquery('simple', %(exclude)s))""")
            params["exclude"] = " or ".join(words)

    def relative(self) -> bool:
        """Нет зафиксированных дат: фильтр сам сдвигается со временем — на него
        можно подписаться."""
        return not (
            (self.deadline and self.deadline != DEADLINE_RULE)
            or self.end_from not in ("", TODAY)
            or self.start_from or self.start_to or self.published_from not in ("", TODAY) or self.published_to
            or self.itogi_from or self.itogi_to
        )

    def to_query(self, scope: str = "lots") -> str:
        """Ссылка на поиск с этим фильтром."""
        pairs: list[tuple[str, Any]] = [("applied", 1)]
        names = {"stages": "stage", "statuses": "status", "regions": "region", "categories": "category",
                 "methods": "method", "subjects": "subject", "years": "year"}
        for key, value in self.to_json().items():
            if key == "end_from" and value == TODAY:
                pairs.append(("open_now", 1))
            elif key == "published_from" and value == TODAY:
                pairs.append(("today_only", 1))
            elif key == "deadline":
                pairs.append(("deadline_mode", "rule" if value == DEADLINE_RULE else ""))
                if value and value != DEADLINE_RULE:
                    pairs.append(("deadline_date", value))
            elif key in names:
                pairs += [(names[key], item) for item in value]
            elif isinstance(value, bool):
                if value:
                    pairs.append((key, 1))
            elif value not in (None, ""):
                pairs.append((key, value))
        return f"/{scope}?" + urlencode(pairs)

    def order_by(self, scope: str = "lots") -> str:
        orders = _LOT_ORDER if scope == "lots" else _ANNOUNCEMENT_ORDER
        return orders.get(self.sort, orders["advance"])

    def selected(self, scope: str = "lots") -> list[tuple[str, str]]:
        """Заданные условия для строки «Выбранные фильтры»: (подпись, ссылка на тот же поиск
        без этого условия). Без условия = как будто поле не заполнено (applied=1)."""
        _ = gettext
        items: list[tuple[str, str]] = []

        def add(label: str, **reset: Any) -> None:
            items.append((label, replace(self, **reset).to_query(scope)))

        money = lambda v: f"{v:,.0f}".replace(",", " ")  # noqa: E731
        if self.q:
            add(f"«{self.q}»" + (" " + _("и в техспецификации") if self.in_specs else ""), q="", in_specs=False)
        if self.exclude:
            add(_("без слов «{words}»", words=self.exclude), exclude="")
        if self.number:
            add(_("объявление № {number}", number=self.number), number="")
        if self.lot_number:
            add(_("лот № {number}", number=self.lot_number), lot_number="")
        if self.enstru:
            add(f"ЕНС ТРУ {self.enstru}", enstru="")
        if self.customer:
            add(_("заказчик «{name}»", name=self.customer), customer="")
        for region in self.regions:
            add(_(REGIONS.get(region, region)), regions=[r for r in self.regions if r != region])
        if self.categories:
            from zakup.catalog import division_name

            for code in self.categories:
                add(division_name(code, language()) or code, categories=[c for c in self.categories if c != code])
        if self.statuses or self.stages:
            names = [_(STAGES[s]) for s in self.stages if s in STAGES]
            names += [_(STATUS_NAMES[s]) for s in self.statuses if s in STATUS_NAMES]
            add(_("Статус") + ": " + ", ".join(names), statuses=[], stages=[])
        if self.disable_person:
            add(_("среди организаций инвалидов") if self.disable_person == "only"
                else _("без закупок среди организаций инвалидов"), disable_person="")
        if self.system in SYSTEM_IDS:
            add(_(SYSTEMS[self.system]), system="")
        if self.methods:
            add(", ".join(_(METHODS[m][0]) for m in self.methods if m in METHODS), methods=[])
        if self.subjects:
            add(", ".join(_(SUBJECTS[s]) for s in self.subjects if s in SUBJECTS), subjects=[])
        if self.years:
            add(_("финансовый год {years}", years=", ".join(map(str, self.years))), years=[])
        if self.min_sum is not None or self.max_sum is not None:
            label = " ".join(filter(None, (
                _("от {sum} ₸", sum=money(self.min_sum)) if self.min_sum is not None else "",
                _("до {sum} ₸", sum=money(self.max_sum)) if self.max_sum is not None else "")))
            add(label, min_sum=None, max_sum=None)
        if self.deadline:
            add(_("окончание в течение 2 рабочих дней") if self.deadline == DEADLINE_RULE
                else _("окончание по {date}", date=self.deadline), deadline="")
        if self.end_from == TODAY:
            add(_("только с открытым приёмом заявок"), end_from="")
        elif self.end_from:
            add(f"{_('окончание')} {self.end_from} — …", end_from="")
        if self.published_from == TODAY:
            add(_("только опубликованные сегодня"), published_from="")
        if self.start_from or self.start_to or self.itogi_from or self.itogi_to or self.published_to \
                or (self.published_from and self.published_from != TODAY):
            add(_("Даты"), start_from="", start_to="", itogi_from="", itogi_to="", published_to="",
                published_from=TODAY if self.published_from == TODAY else "")
        if self.advance_only or (self.advance_min, self.advance_max) != (0, 100):
            add(_("с авансом") if self.advance_only and (self.advance_min, self.advance_max) == (0, 100)
                else _("аванс {low}–{high}%", low=self.advance_min, high=self.advance_max),
                advance_only=False, advance_min=0, advance_max=100)
        for label, reset in self.score_labels():
            add(label, **reset)
        return items

    def describe(self) -> str:
        """Условия фильтра словами — на языке интерфейса."""
        _ = gettext
        parts = []
        if self.q:
            parts.append(f"«{self.q}»" + (" " + _("и в техспецификации") if self.in_specs else ""))
        if self.exclude:
            parts.append(_("без слов «{words}»", words=self.exclude))
        if self.number:
            parts.append(_("объявление № {number}", number=self.number))
        if self.lot_number:
            parts.append(_("лот № {number}", number=self.lot_number))
        if self.enstru:
            parts.append(f"ЕНС ТРУ {self.enstru}")
        if self.stages:
            parts.append(", ".join(_(STAGES[s]) for s in self.stages if s in STAGES))
        if self.statuses:
            parts.append(", ".join(_(STATUS_NAMES[s]) for s in self.statuses if s in STATUS_NAMES))
        if self.disable_person == "only":
            parts.append(_("среди организаций инвалидов"))
        elif self.disable_person == "exclude":
            parts.append(_("без закупок среди организаций инвалидов"))
        if self.system in SYSTEM_IDS:
            parts.append(_(SYSTEMS[self.system]))
        if self.methods:
            parts.append(", ".join(_(METHODS[m][0]) for m in self.methods if m in METHODS))
        if self.subjects:
            parts.append(", ".join(_(SUBJECTS[s]) for s in self.subjects if s in SUBJECTS).lower())
        if self.years:
            parts.append(_("финансовый год {years}", years=", ".join(map(str, self.years))))
        if self.customer:
            parts.append(_("заказчик «{name}»", name=self.customer))
        if self.regions:
            parts.append(", ".join(_(REGIONS[r]) for r in self.regions if r in REGIONS))
        if self.categories:
            from zakup.catalog import division_name

            parts.append(", ".join(division_name(c, language()) or c for c in self.categories))
        if self.min_sum is not None:
            parts.append(_("от {sum} ₸", sum=f"{self.min_sum:,.0f}".replace(",", " ")))
        if self.max_sum is not None:
            parts.append(_("до {sum} ₸", sum=f"{self.max_sum:,.0f}".replace(",", " ")))
        if self.deadline == DEADLINE_RULE:
            parts.append(_("окончание в течение 2 рабочих дней"))
        elif self.deadline:
            parts.append(_("окончание по {date}", date=self.deadline))
        if self.end_from == TODAY:
            parts.append(_("только с открытым приёмом заявок"))
        if self.published_from == TODAY:
            parts.append(_("только опубликованные сегодня"))
        for label, start, end in (("окончание", "" if self.end_from == TODAY else self.end_from, ""),
                                  ("начало приёма", self.start_from, self.start_to),
                                  ("опубликовано", "" if self.published_from == TODAY else self.published_from,
                                   self.published_to),
                                  ("протокол итогов", self.itogi_from, self.itogi_to)):
            if start or end:
                parts.append(f"{_(label)} {start or '…'} — {end or '…'}")
        if self.advance_only:
            parts.append(_("с авансом"))
        if (self.advance_min, self.advance_max) != (0, 100):
            parts.append(_("аванс {low}–{high}%", low=self.advance_min, high=self.advance_max))
        parts += [label for label, _reset in self.score_labels()]
        return "; ".join(parts)


MAX_SUM_DIGITS = 15


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    # «1e200000000» — допустимое Decimal, но в строку это сотни мегабайт: копия сайта
    # падала от нехватки памяти. Сумм больше триллиона тенге в закупках не бывает.
    # adjusted() — порядок числа без вычислений (abs() такого числа переполняется).
    return number if number.is_finite() and number.adjusted() < MAX_SUM_DIGITS else None


# «стол или стул» — то же, что «стол or стул» в синтаксисе websearch_to_tsquery.
_OR = re.compile(r"\s+или\s+", re.IGNORECASE)
_OR_KK = re.compile(r"\s+немесе\s+", re.IGNORECASE)


def _percent(value: str, default: int) -> int:
    return min(max(int(value), 0), 100) if value.isdigit() else default


def last_workday(today: date) -> date:
    """С какого дня «опубликованные сегодня»: сегодня; в субботу и воскресенье — с пятницы;
    в понедельник — с субботы (публикации выходных тоже новые для того, кто пришёл в пн)."""
    if today.weekday() == 0:
        return today - timedelta(days=2)
    return today - timedelta(days=max(0, today.weekday() - 4))


def _today() -> date:
    return datetime.now(PORTAL_TZ).date()


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
