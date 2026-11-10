"""Общее для всех разделов сайта: пул соединений с базой, шаблоны и их помощники,
зависимости доступа (гость / клиент / тариф / сотрудник / администратор), журнал и
статистика, клиент портала.
"""

import hashlib
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import psycopg
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from psycopg_pool import ConnectionPool

from zakup import (
    SITE_URL,
    audit,
    auth,
    branding,
    catalog,
    config,
    i18n,
    legal,
    orgs,
    subjects,
    tariff,
    visits,
)
from zakup.filters import (
    DEADLINE_RULE,
    DISABLE_PERSON,
    METHOD_NAMES,
    METHODS,
    OPEN_STATUSES,
    REGIONS,
    SORTS,
    STAGES,
    STATUS_NAMES,
    SUBJECTS,
    Filters,
    deadline_by_rule,
    fin_years,
)
from zakup.goszakup import PORTAL_TZ, Client, parse_date

logger = logging.getLogger(__name__)


PAGE_SIZE = 50


SESSION_COOKIE = "zakup_session"


# check — соединение проверяется перед выдачей: после перезапуска базы (выкладка) старые
# соединения отбрасываются, а не дают ошибку на первом запросе.
# statement_timeout — зависший запрос (перебор фильтров) не держит соединение пула дольше 15 с.
pool = ConnectionPool(os.environ.get("DATABASE_URL", ""), min_size=1, max_size=5, open=False,
                      check=ConnectionPool.check_connection, kwargs={"options": "-c statement_timeout=15000"})


templates = Jinja2Templates(directory=str(files("zakup").joinpath("templates")))
# Версия файлов из static/ для адресов скриптов (…/app.js?v=…): после выкладки браузер
# берёт новые, а не старые из своего кэша.
templates.env.globals["asset_v"] = hashlib.sha1(b"".join(
    f.read_bytes() for f in sorted(Path(str(files("zakup").joinpath("static"))).iterdir()) if f.is_file()
)).hexdigest()[:10]


templates.env.globals["announcement_url"] = "/a/{}".format


def _money(value) -> str:
    # Суммы приходят числами из базы и GraphQL и строками из разобранного протокола.
    try:
        return f"{Decimal(str(value)):,.2f}".replace(",", " ")
    except (InvalidOperation, ValueError):
        return ""


templates.env.filters["money"] = _money


def xlsx_text(value):
    """Текст с портала — без управляющих символов: openpyxl на них падает (вся выгрузка — 500)."""
    if isinstance(value, str):
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value
# Целые числа с пробелами по тысячам: 12 345.
templates.env.filters["count"] = lambda value: f"{int(value or 0):,}".replace(",", " ")


templates.env.filters["local"] = lambda v: v.astimezone(PORTAL_TZ) if v else v


templates.env.globals["region_names"] = lambda kato: sorted(
    {i18n.gettext(REGIONS[k[:2]]) for k in kato or [] if k[:2] in REGIONS}
)


def _time_left(end: datetime | None) -> str:
    if end is None:
        return ""
    seconds = int((end - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return i18n.gettext("приём окончен")
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return i18n.gettext("через {d} д {h} ч", d=days, h=hours)
    return i18n.gettext("через {h} ч {m} мин", h=hours, m=rest // 60)


templates.env.globals["time_left"] = _time_left


def _hours_left(end: datetime | None) -> float | None:
    return (end - datetime.now(UTC)).total_seconds() / 3600 if end else None


templates.env.globals["hours_left"] = _hours_left


templates.env.globals["open_statuses"] = set(OPEN_STATUSES)


templates.env.globals["site_url"] = SITE_URL
# Компьютер: фильтр поиска — строкой условий сверху (как на телефоне) с выпадающими шторками.
# Не понравится — False: вернётся прежний фильтр целиком справа от результатов.
DESKTOP_CHIPS = True
templates.env.globals["desktop_chips"] = DESKTOP_CHIPS


def _structured_demand(*, name, description, url, end, amount, count, unit, customer) -> dict:
    """Закупка в разметке schema.org: Demand — «организация ищет товар или услугу». У Demand
    нет поля «покупатель» (seller — это продавец), поэтому заказчик — в описании."""
    demand: dict[str, Any] = {"@context": "https://schema.org", "@type": "Demand", "name": name, "url": url}
    text = " ".join(part for part in (f"{i18n.gettext('Заказчик')}: {customer}." if customer else "",
                                      str(description or "")) if part)
    if text:
        demand["description"] = text[:1000]
    if end:
        demand["availabilityEnds"] = end.isoformat()
    if amount is not None:
        demand["priceSpecification"] = {"@type": "PriceSpecification", "price": float(amount),
                                        "priceCurrency": "KZT"}
    if count:
        demand["eligibleQuantity"] = {"@type": "QuantitativeValue", "value": float(count),
                                      **({"unitText": unit} if unit else {})}
    return demand


templates.env.globals["structured_demand"] = _structured_demand


def _org_link(bin_: str | None, name: str | None) -> Markup:
    """Название организации — ссылкой на её страницу (БИН или ИИН)."""
    text = escape(name or bin_ or "")
    return Markup(f'<a href="/org/{bin_}">{text}</a>') if orgs.has_page(bin_) else text


templates.env.globals["org_link"] = _org_link
# Адрес страницы на текущем языке: казахская версия — с ?lang=kk (как canonical).
templates.env.globals["lang_url"] = lambda path: SITE_URL + path + ("?lang=kk" if i18n.language() == "kk" else "")
# У ИП и физлиц — ИИН, у организаций — БИН.
templates.env.globals["id_label"] = lambda number: i18n.gettext("ИИН" if subjects.is_iin(number) else "БИН")
# Подборки: название категории КПВЭД и региона на языке интерфейса.
templates.env.globals["division_name"] = lambda code: catalog.division_name(code, i18n.language())
templates.env.globals["region_name"] = lambda code: i18n.gettext(REGIONS[code]) if code in REGIONS else ""
templates.env.filters["region_name_filter"] = templates.env.globals["region_name"]
templates.env.filters["division_label"] = lambda code: catalog.division_name(code, i18n.language()) or code
templates.env.globals["division_label"] = templates.env.filters["division_label"]


def _tariff() -> dict:
    """Тариф для шаблонов (окно «платная функция», страница тарифа) — из настроек."""
    try:
        with pool.connection() as conn:
            return tariff.get(conn)
    except psycopg.Error:
        return tariff.DEFAULT


templates.env.globals["tariff"] = _tariff


def _requisites() -> dict:
    try:
        with pool.connection() as conn:
            return legal.get(conn)
    except psycopg.Error:
        return {key: "" for key in legal.FIELDS}


templates.env.globals["requisites"] = _requisites


def _brand(default_tagline: str) -> dict:
    """Логотип и текст под ним — из настроек «Оформление»."""
    data = branding.cached()
    if data is None:
        try:
            with pool.connection() as conn:
                data = branding.get(conn)
        except psycopg.Error:
            data = {}
    return branding.view(data, i18n.language(), default_tagline)


templates.env.globals["brand"] = _brand


def _spec_blocks(text: str | None) -> list[tuple[str, Any]]:
    """Текст спецификации кусками: строки с « | » (из Excel и таблиц Word) подряд —
    таблица, остальное — обычный текст."""
    blocks: list[tuple[str, Any]] = []
    for line in (text or "").splitlines():
        if " | " in line:
            cells = [c.strip() for c in line.split(" | ")]
            if blocks and blocks[-1][0] == "table":
                blocks[-1][1].append(cells)
            else:
                blocks.append(("table", [cells]))
        elif blocks and blocks[-1][0] == "text":
            blocks[-1] = ("text", blocks[-1][1] + "\n" + line)
        else:
            blocks.append(("text", line))
    return blocks


templates.env.globals["spec_blocks"] = _spec_blocks


templates.env.globals["_"] = i18n.gettext


templates.env.filters["tr"] = i18n.gettext


templates.env.globals["lang"] = i18n.language


templates.env.globals["loc"] = i18n.loc


templates.env.globals["pick"] = i18n.pick


templates.env.globals["languages"] = i18n.LANGUAGES


def _portal_date(value, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Дата из ответа портала → местное время; неразборчивая — прочерк, а не ошибка."""
    parsed = parse_date(value)
    return parsed.astimezone(PORTAL_TZ).strftime(fmt) if parsed else "—"


templates.env.filters["portal_date"] = _portal_date


# Ссылки из данных портала и ответов модели — только http(s), иначе пусто.
templates.env.filters["safe_url"] = (
    lambda url: url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""
)




# Страницы, просмотр которых пишем в журнал (лоты и объявления пишут свои маршруты).
VIEWED_PAGES = {"/", "/lots", "/announcements", "/my", "/research", "/subscriptions", "/matches",
                "/settings", "/log"}


def _spawn(job) -> None:
    """Фоновая запись статистики: ответ не ждёт её, ошибки не мешают сайту."""
    threading.Thread(target=job, daemon=True).start()


def _record_visit(**fields) -> None:
    try:
        with pool.connection() as conn:
            visits.record(conn, **fields)
    except Exception as error:  # статистика не должна мешать сайту
        logger.warning("visit not recorded: %s", type(error).__name__)


def _audit(request: Request, user: int | None, action: str, target: str | None = None,
           login: str | None = None, **details) -> None:
    """Запись в журнал действий; сбой журнала не должен ломать само действие.
    Под «входом как пользователь» — с пометкой, кто из администраторов это сделал."""
    admin = getattr(request.state, "impersonator_id", None)
    if admin and user != admin:
        details["by_admin"] = admin
    try:
        with pool.connection() as conn:
            audit.log(conn, user, action, target, details or None,
                      ip=request.client.host if request.client else None, login=login)
    except psycopg.Error as error:
        logger.warning("audit failed: %s", type(error).__name__)


class LoginRequired(Exception):
    pass


def optional_user(request: Request) -> int | None:
    """Кто смотрит: пользователь или гость (None). Открытые страницы доступны всем."""
    with pool.connection() as conn:
        user = auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE))
    request.state.email = user[1] if user else ""
    request.state.role = user[2] if user else ""
    request.state.user_id = user[0] if user else None
    # Платные функции: сотрудники всегда, клиенты — пока оплачено.
    request.state.paid = bool(user and user[3])
    request.state.staff = bool(user and user[2] in auth.STAFF_ROLES)
    request.state.searches = user[4] if user else 0
    # Администратор вошёл как этот пользователь — видно по самой сессии (cookie можно
    # удалить, сессию — нет). Его собственная сессия — в отдельной cookie.
    request.state.impersonator = None
    request.state.impersonator_id = user[5] if user else None
    if request.state.impersonator_id:
        with pool.connection() as conn:
            request.state.impersonator = conn.execute(
                "SELECT email FROM users WHERE id = %s", (user[5],)).fetchone()[0]
    return user[0] if user else None


RETURN_COOKIE = "zakup_return"


IMPERSONATE_TTL = timedelta(hours=2)


def current_user(request: Request) -> int:
    user = optional_user(request)
    if user is None:
        raise LoginRequired()
    return user


Viewer = Annotated[int | None, Depends(optional_user)]


User = Annotated[int, Depends(current_user)]


class PaymentRequired(Exception):
    pass


def current_paid(request: Request, user: User) -> int:
    if not request.state.paid:
        raise PaymentRequired()
    return user


Paid = Annotated[int, Depends(current_paid)]


def current_admin(request: Request, user: User) -> int:
    if request.state.role != "admin":
        raise HTTPException(403, "Доступно только администратору")
    return user


Admin = Annotated[int, Depends(current_admin)]


def not_impersonating(request: Request, user: User) -> None:
    """Пароль, Telegram, оплата, группа клиентов — только сам владелец аккаунта, не
    администратор, вошедший под ним (иначе он мог бы, например, привязать свой Telegram
    к чужому аккаунту и читать его уведомления)."""
    if request.state.impersonator_id:
        raise HTTPException(403, "Недоступно при входе как пользователь")


def current_staff(request: Request, user: User) -> int:
    if request.state.role not in auth.STAFF_ROLES:
        raise HTTPException(403, "Доступно только администратору и модераторам")
    return user


Staff = Annotated[int, Depends(current_staff)]


def same_origin(request: Request) -> None:
    # SameSite=Lax уже не пускает cookie с чужих сайтов, это второй рубеж.
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request")


def _safe_next(target: str | None) -> str:
    # Только пути этого сайта: иначе ссылка на вход уводила бы на чужой домен.
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _amount(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise HTTPException(422, "сумма должна быть числом") from None


# Дальше 500-й страницы никто не листает; огромный номер давал ошибку базы (OFFSET).
MAX_PAGE = 500


def _user_groups(conn, user: int) -> list[tuple[str, str]]:
    """Telegram-группы, куда пользователь может слать подписки: [(chat_id, название)].

    Группы подключает администратор, а выбирать их в подписках может каждый:
    это общие рабочие группы команды. Какие подписки слать в группу, а какие
    себе в личку, каждый решает сам.
    """
    # Клиентам уведомления — только в личку; группы — для владельца и сотрудников.
    role = conn.execute("SELECT role FROM users WHERE id = %s", (user,)).fetchone() if user else None
    if not role or role[0] not in auth.STAFF_ROLES:
        return []
    return conn.execute(
        "SELECT chat_id, coalesce(title, 'Группа') FROM telegram_groups ORDER BY title"
    ).fetchall()


PER_PAGE_OPTIONS = (20, 50, 100)


def _per_page(value: int) -> int:
    return value if value in PER_PAGE_OPTIONS else PAGE_SIZE


def _search_context(request: Request, current: Filters, scope: str, total: int, page: int,
                    form: dict, now: datetime, per_page: int = PAGE_SIZE) -> dict:
    today = now.astimezone(PORTAL_TZ).date()
    return {
        "scope": scope,
        "f": current,
        "total": total,
        "page": page,
        "pages": max((total + per_page - 1) // per_page, 1),
        "per_page": per_page,
        "per_page_options": PER_PAGE_OPTIONS,
        "deadline_date": current.deadline_date(today),
        "rule_date": deadline_by_rule(today),
        "today": today,
        "rule": DEADLINE_RULE,
        "stages": STAGES,
        "status_names": STATUS_NAMES,
        "regions": REGIONS,
        "categories": {code: catalog.division_name(code, i18n.language()) for code in catalog.DIVISIONS},
        "methods": METHODS,
        "method_names": METHOD_NAMES,
        "subjects": SUBJECTS,
        "years": fin_years(today),
        "sorts": SORTS,
        "disable_options": DISABLE_PERSON,
        # Ссылки на другую страницу поиска, сортировку и страницы — из самого фильтра,
        # а не из адреса: на первом заходе адрес пустой, а действуют значения по умолчанию.
        "query": urlsplit(current.to_query(scope)).query
        + (f"&per_page={per_page}" if per_page != PAGE_SIZE else ""),
        "here": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
    }


def _track(request: Request, name: str, props: dict | None = None) -> None:
    """Событие для статистики (в фоне, ошибки не мешают ответу)."""
    visitor = request.cookies.get(visits.COOKIE)
    user = getattr(request.state, "user_id", None)

    def write() -> None:
        try:
            with pool.connection() as conn:
                visits.event(conn, name, visitor, user, props)
        except Exception as error:
            logger.warning("event not recorded: %s", type(error).__name__)
    _spawn(write)


def _track_search(request: Request, scope: str, current: Filters, form: dict, page: int, total: int) -> None:
    # Только применённый поиск и первая страница: листание и сортировка — не новый поиск.
    if page != 1 or not form or visits.is_bot(request.headers.get("user-agent")):
        return
    _track(request, "search", {
        "scope": scope, "q": current.q[:120], "regions": current.regions, "methods": current.methods,
        "subjects": current.subjects, "min_sum": float(current.min_sum) if current.min_sum else None,
        "max_sum": float(current.max_sum) if current.max_sum else None,
        "customer": bool(current.customer), "total": total})


DETAIL_TTL = timedelta(minutes=15)


_client: Client | None = None


def _goszakup() -> Client:
    global _client
    if _client is None:
        _client = Client(config.goszakup_token(), delay=0, attempts=2)
    return _client


def _with_session(response: Response, token: str) -> Response:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


def _saved_filters(conn, user: int | None) -> list[dict]:
    """«Мои фильтры»: подписки пользователя по фильтру — ссылка на поиск с их условиями
    (в режиме правки: изменения можно сохранить обратно в подписку)."""
    if not user:
        return []
    rows = conn.execute(
        """SELECT id, name, scope, filters FROM subscriptions
           WHERE user_id = %s AND kind = 'announcements' AND filters IS NOT NULL ORDER BY id""",
        (user,)).fetchall()
    out = []
    for sub_id, name, scope, raw in rows:
        scope = scope if scope in ("lots", "announcements") else "lots"
        current = Filters.from_json(raw)
        out.append({"id": sub_id, "name": name, "scope": scope,
                    "url": f"{current.to_query(scope)}&edit={sub_id}", "describe": current.describe()})
    return out


def _editing(conn, user: int | None, form: dict) -> dict | None:
    """Поиск открыт кнопкой «Редактировать» подписки: её название и получатель."""
    raw = (form.get("edit") or [""])[0]
    if not user or not raw.isdigit():
        return None
    row = conn.execute(
        "SELECT id, name, chat_id FROM subscriptions WHERE id = %s AND user_id = %s AND kind = 'announcements'",
        (int(raw), user),
    ).fetchone()
    return {"id": row[0], "name": row[1], "chat_id": row[2]} if row else None
