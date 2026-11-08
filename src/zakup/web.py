"""Веб-интерфейс: поиск по лотам, подписки, совпадения.

Вход по e-mail и паролю, сессия в cookie. Регистрации пока нет: пользователей
заводит `zakup set-password`.
"""

import io
import logging
import os
import time
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Annotated
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from zakup import (
    SITE_URL,
    audit,
    auth,
    bot,
    contacts,
    dashboard,
    details,
    i18n,
    marks,
    matcher,
    questions,
    research,
    results,
    secrets_box,
    sources,
    spec,
    subjects,
    telegram,
)
from zakup.filters import (
    DEADLINE_RULE,
    DISABLE_PERSON,
    METHOD_NAMES,
    METHODS,
    REGIONS,
    SORTS,
    STAGES,
    STATUS_NAMES,
    SUBJECTS,
    Filters,
    deadline_by_rule,
    fin_years,
)
from zakup.goszakup import PORTAL_TZ, Client, GoszakupError, parse_date
from zakup.matcher import PORTAL_ANNOUNCEMENT_URL

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
SESSION_COOKIE = "zakup_session"

pool = ConnectionPool(os.environ.get("DATABASE_URL", ""), min_size=1, max_size=5, open=False)
templates = Jinja2Templates(directory=str(files("zakup").joinpath("templates")))
templates.env.globals["announcement_url"] = "/a/{}".format
def _money(value) -> str:
    # Суммы приходят числами из базы и GraphQL и строками из разобранного протокола.
    try:
        return f"{Decimal(str(value)):,.2f}".replace(",", " ")
    except (InvalidOperation, ValueError):
        return ""


templates.env.filters["money"] = _money
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
templates.env.globals["site_url"] = SITE_URL
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(files("zakup").joinpath("static"))), name="static")


@app.middleware("http")
async def language(request: Request, call_next):
    i18n.set_language(request.cookies.get(i18n.COOKIE))
    return await call_next(request)


@app.get("/lang/{code}")
def switch_language(request: Request, code: str):
    """Переключатель Рус/Қаз: запоминает выбор и возвращает на ту же страницу."""
    if code not in i18n.LANGUAGES:
        raise HTTPException(404, "нет такого языка")
    back = request.headers.get("referer") or "/"
    parts = urlsplit(back)
    target = _safe_next(parts.path + (f"?{parts.query}" if parts.query else ""))
    if parts.netloc and parts.netloc != request.headers.get("host"):
        target = "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, secure=True, samesite="lax")
    return response


# Страницы, просмотр которых пишем в журнал (лоты и объявления пишут свои маршруты).
VIEWED_PAGES = {"/", "/lots", "/announcements", "/my", "/research", "/subscriptions", "/matches",
                "/settings", "/log"}


@app.middleware("http")
async def page_views(request: Request, call_next):
    response = await call_next(request)
    user = getattr(request.state, "user_id", None)
    if (user and request.method == "GET" and response.status_code == 200
            and request.url.path in VIEWED_PAGES):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        await run_in_threadpool(_audit, request, user, "view_page", target[:500])
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Сайт не встраивается в чужие страницы (кнопки удаления и т.п. — в один клик).
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/settings"):
        # Здесь показываются выданные пароли: не кэшировать.
        response.headers["Cache-Control"] = "no-store"
    return response


def _audit(request: Request, user: int | None, action: str, target: str | None = None,
           login: str | None = None, **details) -> None:
    """Запись в журнал действий; сбой журнала не должен ломать само действие."""
    try:
        with pool.connection() as conn:
            audit.log(conn, user, action, target, details or None,
                      ip=request.client.host if request.client else None, login=login)
    except psycopg.Error as error:
        logger.warning("audit failed: %s", type(error).__name__)


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
def _to_login(request: Request, _: LoginRequired):
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


def current_user(request: Request) -> int:
    with pool.connection() as conn:
        user = auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise LoginRequired()
    request.state.email = user[1]
    request.state.role = user[2]
    request.state.user_id = user[0]
    return user[0]


User = Annotated[int, Depends(current_user)]


def current_admin(request: Request, user: User) -> int:
    if request.state.role != "admin":
        raise HTTPException(403, "Доступно только администратору")
    return user


Admin = Annotated[int, Depends(current_admin)]


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


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    with pool.connection() as conn:
        if auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "email": "", "error": None}
    )


@app.post("/login", dependencies=[Depends(same_origin)])
def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    ip = request.client.host if request.client else None
    email_norm = auth.normalize_email(email)
    with pool.connection() as conn:
        locked = auth.too_many_failures(conn, email_norm, ip)
        if locked:
            token = None
            error = i18n.gettext("Слишком много неудачных попыток. Попробуйте через 15 минут.")
        else:
            token = auth.login(conn, email_norm, password, ip, request.headers.get("user-agent"))
            error = None if token else i18n.gettext("Неверный логин или пароль.")
    if token is None:
        _audit(request, None, "login_failed", login=email_norm[:100],
               reason="lock" if locked else "password")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "email": email, "error": error},
            status_code=429 if locked else 401,
        )
    with pool.connection() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE email = %s", (email_norm,)).fetchone()[0]
    _audit(request, user_id, "login")
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.post("/logout", dependencies=[Depends(same_origin)])
def logout(request: Request):
    with pool.connection() as conn:
        who = auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE))
        auth.logout(conn, request.cookies.get(SESSION_COOKIE))
    if who:
        _audit(request, who[0], "logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax")
    return response


def _amount(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise HTTPException(422, "сумма должна быть числом") from None


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def home(request: Request, user: User, background: BackgroundTasks):
    # Старые ссылки вида /?applied=1&… вели на поиск — туда и отправляем.
    if request.url.query:
        return RedirectResponse(f"/announcements?{request.url.query}", status_code=307)
    market, stale = dashboard.market_cached()
    with pool.connection() as conn:
        if market is None:
            market = dashboard.market(conn)
        elif stale:
            # Показываем прежние цифры сразу, а пересчёт — после ответа.
            background.add_task(_recount_market)
        context = {"m": market, "now": datetime.now(UTC)}
    return templates.TemplateResponse(request, "dashboard.html", context)


def _user_groups(conn, user: int) -> list[tuple[str, str]]:
    """Telegram-группы, куда пользователь может слать подписки: [(chat_id, название)].

    Группы подключает администратор, а выбирать их в подписках может каждый:
    это общие рабочие группы команды. Какие подписки слать в группу, а какие
    себе в личку, каждый решает сам.
    """
    del user  # группы общие для всех пользователей
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


@app.get("/lots")
def lots_search(request: Request, user: User, page: int = 1, per_page: int = PAGE_SIZE):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form)
    page = max(page, 1)
    per_page = _per_page(per_page)
    now = datetime.now(UTC)
    where, params = current.where(now, "lots")
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT l.id, l.lot_number, a.id, a.number_anno, a.name_ru,
                       coalesce(a.customer_name_ru, l.customer_name_ru, a.org_name_ru),
                       l.name_ru, coalesce(l.extra_desc, l.description_ru), l.count,
                       l.unit_name, l.price, l.amount, l.prepayment, a.trade_method_id,
                       l.status_id, a.end_date, coalesce(nullif(l.kato, '{{}}'), a.kato),
                       r.outcome, r.winner_name, r.winner_total,
                       jsonb_array_length(r.offers), r.reason,
                       l.name_kz, a.name_kz,
                       l.extra_desc_kz, l.unit_name_kz
                FROM lots l JOIN announcements a ON true
                LEFT JOIN lot_results r ON r.lot_id = l.id
                WHERE {where} ORDER BY {current.order_by("lots")}
                LIMIT {per_page} OFFSET {(page - 1) * per_page}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) FROM lots l, announcements a WHERE {where}", params
        ).fetchone()[0]
        lot_marks = marks.of_user(conn, user, [row[0] for row in rows])
        stats = conn.execute(
            """SELECT (SELECT count(*) FROM announcements WHERE end_date > now()),
                      (SELECT count(*) FROM announcements
                       WHERE end_date > now() AND details_fetched_at IS NULL),
                      (SELECT max(updated_at) FROM sync_state)"""
        ).fetchone()
        groups = _user_groups(conn, user)
    context = _search_context(request, current, "lots", total, page, form, now, per_page)
    context.update(rows=rows, marks=lot_marks, stats=stats, groups=groups)
    return templates.TemplateResponse(request, "lots.html", context)


@app.get("/announcements")
def announcements_search(request: Request, user: User, page: int = 1, per_page: int = PAGE_SIZE):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form)
    page = max(page, 1)
    per_page = _per_page(per_page)
    now = datetime.now(UTC)
    where, params = current.where(now, "announcements")
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT a.id, a.number_anno,
                       coalesce(a.count_lots,
                                (SELECT count(*) FROM lots c WHERE c.trd_buy_id = a.id)),
                       a.name_ru, coalesce(a.org_name_ru, a.customer_name_ru),
                       a.trade_method_id, a.start_date, a.end_date, a.total_sum, a.status_id,
                       a.prepayment_max, a.detail->'RefBuyStatus'->>'nameRu',
                       a.detail->'RefTradeMethods'->>'nameRu',
                       -- Авансы лотов без повторов: 30 и 100, а не 30, 30, 100.
                       (SELECT array_agg(DISTINCT coalesce(p.prepayment, 0) ORDER BY coalesce(p.prepayment, 0))
                        FROM lots p WHERE p.trd_buy_id = a.id),
                       a.name_kz, a.detail->'RefTradeMethods'->>'nameKz',
                       a.detail->'RefBuyStatus'->>'nameKz', a.detail->'Organizer'->>'nameKz'
                FROM announcements a WHERE {where} ORDER BY {current.order_by("announcements")}
                LIMIT {per_page} OFFSET {(page - 1) * per_page}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) FROM announcements a WHERE {where}", params
        ).fetchone()[0]
        groups = _user_groups(conn, user)
    context = _search_context(request, current, "announcements", total, page, form, now,
                              per_page)
    context.update(rows=rows, groups=groups)
    return templates.TemplateResponse(request, "announcements.html", context)


EXPORT_LIMIT = 10_000


@app.get("/export.xlsx")
def export(request: Request, user: User):
    from openpyxl import Workbook

    _audit(request, user, "export", "/lots?" + request.url.query)
    current = Filters.from_form(parse_qs(request.url.query))
    where, params = current.where(datetime.now(UTC), "lots")
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT a.number_anno, a.name_ru, coalesce(a.customer_name_ru, l.customer_name_ru),
                       a.detail->'RefBuyStatus'->>'nameRu', a.detail->'RefTradeMethods'->>'nameRu',
                       a.end_date, l.lot_number, l.name_ru, coalesce(l.extra_desc, l.description_ru),
                       l.count, l.unit_name, l.price, l.amount, l.prepayment,
                       coalesce(nullif(l.kato, '{{}}'), a.kato), r.winner_name, r.winner_total,
                       a.id, l.id
                FROM lots l JOIN announcements a ON true
                LEFT JOIN lot_results r ON r.lot_id = l.id
                WHERE {where}
                ORDER BY {current.order_by("lots")} LIMIT {EXPORT_LIMIT}""",
            params,
        ).fetchall()
    book = Workbook()
    sheet = book.active
    sheet.title = "Лоты"
    sheet.append(["Объявление", "Название объявления", "Заказчик", "Статус", "Способ",
                  "Окончание приёма", "Лот", "Наименование лота", "Описание", "Кол-во",
                  "Ед.", "Цена за ед., ₸", "Сумма, ₸", "Аванс, %", "Место поставки",
                  "Победитель", "Сумма победителя, ₸", "Ссылка"])
    for row in rows:
        end = row[5].astimezone(PORTAL_TZ).replace(tzinfo=None) if row[5] else None
        sheet.append([row[0], row[1], row[2], row[3], row[4], end, row[6], row[7], row[8],
                      row[9], row[10], row[11], row[12], row[13],
                      ", ".join(templates.env.globals["region_names"](row[14])), row[15], row[16],
                      f"{SITE_URL}/lots/{row[18]}"])
    for column, width in zip("ABCDEFGHIJKLMNOPQR", (14, 50, 40, 22, 22, 17, 16, 40, 50, 9, 9,
                                                   14, 15, 9, 24, 36, 16, 40)):
        sheet.column_dimensions[column].width = width
    for cell in sheet["F"][1:]:
        cell.number_format = "dd.mm.yyyy hh:mm"
    sheet.freeze_panes = "A2"
    body = io.BytesIO()
    book.save(body)
    name = f"zakup-{datetime.now(PORTAL_TZ):%Y-%m-%d-%H%M}.xlsx"
    return Response(
        body.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


DETAIL_TTL = timedelta(minutes=15)
_client: Client | None = None


def _goszakup() -> Client:
    global _client
    if _client is None:
        _client = Client(os.environ.get("GOSZAKUP_TOKEN", ""), delay=0, attempts=2)
    return _client


@app.get("/a/{announcement_id}")
def announcement(request: Request, user: User, announcement_id: int, background: BackgroundTasks):
    context = _announcement_context(request, user, announcement_id, background)
    _audit(request, user, "view_announcement", f"/a/{announcement_id}")
    return templates.TemplateResponse(request, "announcement.html", context)


@app.get("/lots/{lot_id}")
def lot_page(request: Request, user: User, lot_id: int, background: BackgroundTasks):
    with pool.connection() as conn:
        row = conn.execute("SELECT trd_buy_id FROM lots WHERE id = %s", (lot_id,)).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(404, "Такого лота нет в базе")
    context = _announcement_context(request, user, row[0], background)
    lot = next((lot for lot in context["lots"] if lot["id"] == lot_id), None)
    if lot is None:
        raise HTTPException(404, "Лот удалён из объявления")
    with pool.connection() as conn:
        job = research.latest(conn, lot_id)
        done = research.latest_done(conn, lot_id)
        lot_info = research.lot_context(conn, lot_id)
        provider = (conn.execute(
            "SELECT ai_provider FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone() or ("claude_code",))[0]
    _audit(request, user, "view_lot", f"/lots/{lot_id}")
    context.update(lot=lot, others=[other for other in context["lots"] if other["id"] != lot_id],
                   here=request.url.path, job=job, done=done, lot_info=lot_info,
                   region=research.default_region(lot_info) if lot_info else "",
                   provider_name=research.PROVIDERS.get(provider, provider),
                   targets=research.TARGETS,
                   is_admin=request.state.role == "admin")
    return templates.TemplateResponse(request, "lot.html", context)


@app.post("/lots/{lot_id}/research", dependencies=[Depends(same_origin)])
def start_research(
    request: Request,
    user: User,
    lot_id: int,
    region: Annotated[str, Form()] = "",
    whole_kz: Annotated[str, Form()] = "",
    target: Annotated[int, Form()] = 10,
    more: Annotated[str, Form()] = "",
):
    with pool.connection() as conn:
        if research.lot_context(conn, lot_id) is None:
            raise HTTPException(404, "Нет данных лота: откройте страницу объявления")
        try:
            research.enqueue(conn, user, lot_id, region, whole_kz == "1",
                             is_admin=request.state.role == "admin", target=target,
                             more=more == "1")
            _audit(request, user, "research", f"/lots/{lot_id}", region=region[:120],
                   count=target, more=more == "1")
        except research.QuotaExceeded as error:
            raise HTTPException(429, f"Лимит поиска товаров: {error}") from None
    return RedirectResponse(f"/lots/{lot_id}#research", status_code=303)


@app.post("/lots/{lot_id}/spec-summary", dependencies=[Depends(same_origin)])
def request_spec_summary(request: Request, user: User, lot_id: int):
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM lot_specs WHERE lot_id = %s AND text IS NOT NULL",
                            (lot_id,)).fetchone():
            raise HTTPException(404, "Нет текста техспецификации")
        status = research.request_summary(conn, lot_id)
    _audit(request, user, "spec_summary", f"/lots/{lot_id}")
    return {"status": status}


@app.get("/lots/{lot_id}/spec-summary")
def spec_summary(request: Request, _: User, lot_id: int):
    with pool.connection() as conn:
        summary = research.summary_of(conn, lot_id)
    return templates.TemplateResponse(request, "summary_fragment.html", {"s": summary})


@app.post("/lots/{lot_id}/questions", dependencies=[Depends(same_origin)])
def request_questions(request: Request, user: User, lot_id: int, url: Annotated[str, Form()]):
    with pool.connection() as conn:
        status = questions.request(conn, lot_id, url)
    if status is None:
        raise HTTPException(404, "Такого предложения нет в результатах подбора")
    _audit(request, user, "supplier_questions", f"/lots/{lot_id}", url=url[:300])
    return {"status": status}


@app.get("/lots/{lot_id}/questions")
def questions_fragment(request: Request, _: User, lot_id: int, url: str):
    with pool.connection() as conn:
        found = questions.of(conn, lot_id, url)
    return templates.TemplateResponse(request, "questions_fragment.html", {"q": found})


@app.get("/lots/{lot_id}/research")
def research_fragment(request: Request, _: User, lot_id: int):
    with pool.connection() as conn:
        job = research.latest(conn, lot_id)
        done = research.latest_done(conn, lot_id)
        lot_info = research.lot_context(conn, lot_id)
    return templates.TemplateResponse(
        request, "research_fragment.html",
        {"job": job, "done": done, "lot_info": lot_info,
         "is_admin": request.state.role == "admin", "targets": research.TARGETS, "lot_id": lot_id},
    )


def _recount_market() -> None:
    with pool.connection() as conn:
        dashboard.market(conn)


_refreshing: set[int] = set()


def _refresh_in_background(announcement_id: int, with_detail: bool, with_results: bool) -> None:
    """Перечитывает объявление с портала после ответа страницы: пользователь не ждёт
    портал, а следующий заход увидит свежие данные. Одно объявление — один раз."""
    if announcement_id in _refreshing:
        return
    _refreshing.add(announcement_id)
    try:
        with pool.connection() as conn:
            if with_detail:
                details.fetch_one(conn, _goszakup(), announcement_id)
            if with_results:
                results.refresh(conn, _goszakup(), announcement_id)
    except (GoszakupError, httpx.HTTPError, psycopg.Error) as error:
        logger.warning("background refresh of %s failed: %s", announcement_id, error)
    finally:
        _refreshing.discard(announcement_id)


def _subjects_in_background(bins: list[str]) -> None:
    try:
        with pool.connection() as conn:
            subjects.ensure(conn, _goszakup(), *bins)
    except (GoszakupError, httpx.HTTPError, psycopg.Error) as error:
        logger.warning("background subjects %s failed: %s", bins, error)


def _announcement_context(request: Request, user: int, announcement_id: int,
                          background: BackgroundTasks) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT detail, details_fetched_at, end_date, results, results_fetched_at
               FROM announcements WHERE id = %s""",
            (announcement_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Такого объявления нет в базе")
        detail, fetched_at, end_date, outcome, results_at = row
        now = datetime.now(UTC)
        stale = fetched_at is None or now - fetched_at > (
            DETAIL_TTL if end_date and end_date > now else timedelta(days=1)
        )
        ended = bool(end_date and end_date < now)
        refresh_error = None
        if detail is None:
            # Подробностей ещё нет совсем (сборщик не дошёл) — тут без портала никак.
            try:
                details.fetch_one(conn, _goszakup(), announcement_id)
                if ended:
                    results.refresh(conn, _goszakup(), announcement_id)
            except GoszakupError:
                conn.rollback()
            detail, fetched_at, outcome, results_at = conn.execute(
                """SELECT detail, details_fetched_at, results, results_fetched_at
                   FROM announcements WHERE id = %s""",
                (announcement_id,),
            ).fetchone()
            if detail is None:
                raise HTTPException(502, "Портал сейчас не отвечает, попробуйте через минуту")
        elif stale or (ended and results.stale(results_at, now)):
            background.add_task(_refresh_in_background, announcement_id, stale,
                                ended and results.stale(results_at, now))
        subscribed = {
            r[0]: r[1]
            for r in conn.execute(
                """SELECT m.lot_id, string_agg(s.name, ', ') FROM matches m
                   JOIN subscriptions s ON s.id = m.subscription_id
                   JOIN lots l ON l.id = m.lot_id
                   WHERE s.user_id = %s AND l.trd_buy_id = %s GROUP BY m.lot_id""",
                (user, announcement_id),
            )
        }
    files, seen = [], set()
    for file in (detail.get("Files") or []) + [
        f for lot in detail.get("Lots") or [] for f in lot.get("Files") or []
    ]:
        if file.get("id") not in seen and file.get("filePath"):
            seen.add(file.get("id"))
            files.append(file)
    for file in detail.get("GeneralFiles") or []:
        if file.get("filePathRu"):
            files.append({"nameRu": file.get("nameRu"), "filePath": file["filePathRu"],
                          "originalName": None})
    protocols = [f for f in files if "протокол" in (f.get("nameRu") or "").lower()]
    documents = [f for f in files if f not in protocols]
    lots = [lot for lot in detail.get("Lots") or [] if not lot.get("isDeleted")]
    info = {lot["id"]: details.parse_lot(lot) for lot in lots}
    with pool.connection() as conn:
        specs = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT lot_id, text, error, text_kk FROM lot_specs WHERE lot_id = ANY(%s)",
            (list(info),))}
        summaries = {r[0]: {"status": r[1], "result": r[2], "error": r[3]} for r in conn.execute(
            "SELECT lot_id, status, result, error FROM spec_summaries WHERE lot_id = ANY(%s)",
            (list(info),))}
        lot_marks = marks.of_user(conn, user, list(info))
        lot_results = results.by_lot(conn, list(info))
        company_bin = conn.execute(
            "SELECT company_bin FROM users WHERE id = %s", (user,)
        ).fetchone()[0]
        org_bins = [detail.get("customerBin"), detail.get("orgBin")] + [
            lot.get("customerBin") for lot in lots]
        org_cards = {b: subjects.card(conn, b) for b in dict.fromkeys(filter(None, org_bins))}
    missing = [b for b, found in org_cards.items() if found is None]
    if missing:
        # Карточки организации ещё нет — догрузим после ответа, следующий заход увидит.
        background.add_task(_subjects_in_background, missing)
    text_phones = {lot["id"]: subjects.phones_in_text(lot.get("descriptionRu"),
                                                      info[lot["id"]].get("extra_desc"))
                   for lot in lots}
    # Сначала лоты с авансом побольше, дальше по номеру.
    lots.sort(key=lambda lot: (-(info[lot["id"]]["prepayment"] or 0), lot.get("lotNumber") or ""))
    return {
        "a": detail,
        "lots": lots,
        "info": info,
        "subscribed": subscribed,
        "marks": lot_marks,
        "specs": specs,
        "summaries": summaries,
        "results": lot_results,
        "contracts": (outcome or {}).get("contracts") or [],
        "contracts_by_lot": results.contracts_by_lot(outcome),
        "appeals": (outcome or {}).get("appeals") or [],
        "results_at": results_at,
        "now": now,
        "company_bin": company_bin,
        "org_cards": org_cards,
        "text_phones": text_phones,
        "lot_numbers": {lot["id"]: lot.get("lotNumber") for lot in lots},
        "here": request.url.path,
        "documents": documents,
        "protocols": protocols,
        "end": parse_date(detail.get("endDate")),
        "dt": parse_date,
        "fetched_at": fetched_at,
        "refresh_error": refresh_error,
        "spec_file": spec.spec_file,
        "portal_url": PORTAL_ANNOUNCEMENT_URL.format(announcement_id),
        "status_names": STATUS_NAMES,
    }


@app.get("/a/{announcement_id}/lots/{lot_id}/spec")
def lot_spec(request: Request, _: User, announcement_id: int, lot_id: int, refresh: int = 0):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT detail FROM announcements WHERE id = %s", (announcement_id,)
        ).fetchone()
        lot = next(
            (l for l in ((row[0] or {}).get("Lots") or []) if l.get("id") == lot_id), None
        ) if row else None
        url = spec.spec_file(lot) if lot else None
        if url is None:
            text, error = None, "У лота нет файла технической спецификации."
        else:
            if refresh:
                # Повтор после ошибки: прошлую ошибку из кэша не берём.
                conn.execute(
                    "DELETE FROM lot_specs WHERE lot_id = %s AND text IS NULL", (lot_id,)
                )
            text, error = spec.get(conn, lot_id, url)
            if i18n.language() == "kk":
                kazakh = conn.execute(
                    "SELECT text_kk FROM lot_specs WHERE lot_id = %s", (lot_id,)
                ).fetchone()
                text = (kazakh and kazakh[0]) or text
    return templates.TemplateResponse(
        request, "spec_fragment.html", {"text": text, "error": error, "url": url, "lot_id": lot_id}
    )


@app.post("/lots/{lot_id}/mark", dependencies=[Depends(same_origin)])
def mark_lot(
    request: Request,
    user: User,
    lot_id: int,
    status: Annotated[str, Form()] = "",
    note: Annotated[str | None, Form()] = None,
    next: Annotated[str, Form()] = "/my",
):
    if status not in ("", "interested"):
        raise HTTPException(422, "неизвестная отметка")
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM lots WHERE id = %s", (lot_id,)).fetchone():
            raise HTTPException(404, "Такого лота нет в базе")
        if note is not None:
            marks.set_note(conn, user, lot_id, note)
        else:
            marks.set_mark(conn, user, lot_id, status)
    if note is not None:
        _audit(request, user, "note", f"/lots/{lot_id}", text=note.strip()[:200])
    else:
        _audit(request, user, "mark", f"/lots/{lot_id}", status=status or "—")
    target = _safe_next(next)
    return RedirectResponse(f"{target}#lot-{lot_id}" if "#" not in target else target, status_code=303)


@app.get("/research")
def research_list(request: Request, user: User, q: str = "", sources_open: int = 0):
    """Сохранённые подборки товаров: последний готовый поиск по каждому лоту."""
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ON (r.lot_id) r.lot_id, r.finished_at, r.region, r.result,
                      l.name_ru, l.name_kz, l.lot_number, l.amount, l.count,
                      coalesce(l.price, l.amount / nullif(l.count, 0)), l.prepayment,
                      a.id, a.number_anno, a.end_date
               FROM lot_research r JOIN lots l ON l.id = r.lot_id
               JOIN announcements a ON a.id = l.trd_buy_id
               WHERE r.status = 'done'
                 AND (%(q)s = '' OR l.name_ru ILIKE %(like)s OR l.name_kz ILIKE %(like)s
                      OR a.number_anno ILIKE %(like)s)
               ORDER BY r.lot_id, r.id DESC""",
            {"q": q.strip(), "like": f"%{q.strip()}%"},
        ).fetchall()
        running = conn.execute(
            """SELECT r.lot_id, r.status, r.base_id IS NOT NULL, l.name_ru, l.name_kz, r.created_at
               FROM lot_research r JOIN lots l ON l.id = r.lot_id
               WHERE r.status IN ('queued', 'running') ORDER BY r.id"""
        ).fetchall()
        active = len(running)
        source_rows = sources.listing(conn)
    items = []
    for row in rows:
        found = (row[3] or {}).get("items") or []
        price = float(row[9]) if row[9] else None
        # Маржа — по лучшему уровню соответствия, который есть: полностью → частично → аналоги.
        margins, margin_basis = [], None
        for fit in ("полностью", "частично", "аналог"):
            margins = [(price - i["unit_price"]) / price * 100 for i in found
                       if price and i.get("unit_price") and i.get("fit") == fit]
            if margins:
                margin_basis = fit
                break
        cheapest = min((i["unit_price"] for i in found if i.get("unit_price")), default=None)
        items.append({
            "lot_id": row[0], "finished_at": row[1], "region": row[2], "name": row[4],
            "name_kz": row[5], "lot_number": row[6], "amount": row[7], "price": price,
            "prepayment": row[10], "announcement_id": row[11], "number": row[12],
            "end_date": row[13], "found": len(found),
            "in_region": sum(1 for i in found if i.get("in_region")),
            "full_fit": sum(1 for i in found if i.get("fit") == "полностью"),
            "best_margin": max(margins) if margins else None, "cheapest": cheapest,
            "margin_basis": margin_basis,
            "partial_fit": sum(1 for i in found if i.get("fit") == "частично"),
        })
    items.sort(key=lambda i: i["finished_at"], reverse=True)
    return templates.TemplateResponse(
        request, "research_list.html",
        {"items": items, "q": q, "active": active, "running": running, "now": datetime.now(UTC),
         "sources": source_rows, "sources_open": sources_open, "me": user,
         "is_admin": request.state.role == "admin"},
    )


@app.post("/sources", dependencies=[Depends(same_origin)])
def add_source(
    request: Request,
    user: User,
    url: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    categories: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    with pool.connection() as conn:
        domain = sources.add(conn, user, url, name, categories, city, note)
    if domain is None:
        raise HTTPException(422, i18n.gettext("Укажите адрес сайта, например zeta.kz"))
    _audit(request, user, "source_add", domain)
    return RedirectResponse("/research?sources_open=1#sources", status_code=303)


@app.get("/sources/{source_id}/contacts")
def source_contacts(request: Request, _: User, source_id: int):
    with pool.connection() as conn:
        row = conn.execute("SELECT domain, name FROM sources WHERE id = %s", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Нет такого источника")
        sellers = [(name, contacts.links(phones), checked)
                   for name, phones, checked in contacts.of_domain(conn, row[0])]
    return templates.TemplateResponse(request, "source_contacts.html",
                                      {"domain": row[0], "name": row[1], "sellers": sellers})


@app.post("/sources/{source_id}/toggle", dependencies=[Depends(same_origin)])
def toggle_source(request: Request, user: User, source_id: int):
    with pool.connection() as conn:
        row = conn.execute(
            """UPDATE sources SET active = NOT active
               WHERE id = %s AND (%s OR added_by = %s OR auto) RETURNING domain, active""",
            (source_id, request.state.role == "admin", user),
        ).fetchone()
    if row:
        _audit(request, user, "source_toggle", row[0], active=row[1])
    return RedirectResponse("/research?sources_open=1#sources", status_code=303)


LOG_PAGE_SIZES = (20, 50, 100)


@app.get("/log")
def action_log(request: Request, _: Admin, page: int = 1, per_page: int = 50):
    """Журнал действий: фильтр по пользователю, действию и датам, постранично."""
    query = parse_qs(request.url.query)
    users = [int(v) for v in query.get("user", []) if v.isdigit()]
    actions = [v for v in query.get("action", []) if v in audit.ACTIONS]
    date_from = (query.get("from") or [""])[0]
    date_to = (query.get("to") or [""])[0]
    per_page = per_page if per_page in LOG_PAGE_SIZES else 50
    page = max(page, 1)
    clauses, params = ["true"], {}
    if users:
        clauses.append("a.user_id = ANY(%(users)s)")
        params["users"] = users
    if actions:
        clauses.append("a.action = ANY(%(actions)s)")
        params["actions"] = actions
    for name, value, op, shift in (("from", date_from, ">=", 0), ("to", date_to, "<", 1)):
        try:
            day = datetime.fromisoformat(value).date() if value else None
        except ValueError:
            day = None
        if day:
            clauses.append(f"a.at {op} %({name})s")
            params[name] = datetime.combine(day + timedelta(days=shift), datetime.min.time(),
                                            PORTAL_TZ)
    where = " AND ".join(clauses)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT a.at, a.user_id, coalesce(u.email, a.login), a.action, a.target,
                       a.details, a.ip
                FROM user_actions a LEFT JOIN users u ON u.id = a.user_id
                WHERE {where} ORDER BY a.at DESC, a.id DESC
                LIMIT {per_page} OFFSET {(page - 1) * per_page}""",
            params,
        ).fetchall()
        total = conn.execute(f"SELECT count(*) FROM user_actions a WHERE {where}", params).fetchone()[0]
        all_users = conn.execute("SELECT id, email FROM users ORDER BY email").fetchall()
    keep = [(k, v) for k, vs in query.items() if k not in ("page",) for v in vs]
    return templates.TemplateResponse(request, "log.html", {
        "rows": rows, "total": total, "page": page, "per_page": per_page,
        "pages": max((total + per_page - 1) // per_page, 1), "page_sizes": LOG_PAGE_SIZES,
        "users": all_users, "chosen_users": users, "chosen_actions": actions,
        "actions": audit.ACTIONS, "date_from": date_from, "date_to": date_to,
        "query": urlencode([(k, v) for k, v in keep if k != "per_page"]
                           + [("per_page", per_page)]),
    })


@app.get("/my")
def my_lots(request: Request, user: User):
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT m.status, m.note, m.status_at, l.id, l.trd_buy_id, l.name_ru,
                      coalesce(l.extra_desc, l.description_ru), l.amount, l.prepayment,
                      coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date,
                      a.number_anno, l.lot_number,
                      (SELECT lot->'RefLotsStatus'->>'nameRu'
                       FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(a.detail->'Lots') = 'array'
                                THEN a.detail->'Lots' ELSE '[]' END) lot
                       WHERE (lot->>'id')::bigint = l.id),
                      a.detail->'RefBuyStatus'->>'nameRu',
                      r.outcome, r.winner_name, r.winner_total, r.reason,
                      r.winner_bin = u.company_bin
               FROM lot_marks m
               JOIN users u ON u.id = m.user_id
               JOIN lots l ON l.id = m.lot_id
               LEFT JOIN announcements a ON a.id = l.trd_buy_id
               LEFT JOIN lot_results r ON r.lot_id = l.id
               WHERE m.user_id = %s AND m.status <> ''
               ORDER BY a.end_date NULLS LAST, l.trd_buy_id, l.id""",
            (user,),
        ).fetchall()
    now = datetime.now(UTC)
    # Сначала открытые — по сроку, потом завершённые.
    rows.sort(key=lambda r: (r[10] is None or r[10] < now, r[10] or now))
    return templates.TemplateResponse(
        request, "my.html",
        {"rows": rows, "now": now, "here": request.url.path},
    )


@app.post("/subscriptions/filter", dependencies=[Depends(same_origin)])
async def create_filter_subscription(request: Request, user: User):
    form = await request.form()
    data = {key: form.getlist(key) for key in form.keys()}
    current = Filters.from_form(data)
    if not current.relative():
        raise HTTPException(
            422, "Подписка работает только без конкретных дат: срок — «в течение 2 рабочих дней» "
                 "или без ограничения."
        )
    scope = "announcements" if form.get("scope") == "announcements" else "lots"
    name = (form.get("name") or "").strip() or current.describe()[:100] or "Мой фильтр"
    with pool.connection() as conn:
        chat_id = _own_chat(conn, user, form.get("chat_id") or "")
        sub_id = conn.execute(
            """INSERT INTO subscriptions (user_id, name, query, kind, filters, scope, chat_id)
               VALUES (%s, %s, '', 'announcements', %s, %s, %s) RETURNING id""",
            (user, name[:100], Jsonb(current.to_json()), scope, chat_id),
        ).fetchone()[0]
        # Если подходит слишком много — не заваливаем чат историей, шлём только новое.
        matcher.seed(conn, sub_id, current, scope)
    _audit(request, user, "subscription_create", name[:100], scope=scope,
           to="group" if chat_id else "personal", conditions=current.describe()[:300])
    return RedirectResponse("/subscriptions", status_code=303)


def _own_chat(conn, user: int, chat_id: str) -> str | None:
    """Группа, в которую пользователь вправе слать; пусто — личный чат."""
    if not chat_id:
        return None
    if chat_id not in {group[0] for group in _user_groups(conn, user)}:
        raise HTTPException(403, "Эта группа не подключена к вашему аккаунту")
    return chat_id


@app.post("/subscriptions/{sub_id}/chat", dependencies=[Depends(same_origin)])
def subscription_chat(request: Request, user: User, sub_id: int,
                      chat_id: Annotated[str, Form()] = ""):
    with pool.connection() as conn:
        name = conn.execute(
            "UPDATE subscriptions SET chat_id = %s WHERE id = %s AND user_id = %s RETURNING name",
            (_own_chat(conn, user, chat_id), sub_id, user),
        ).fetchone()
    if name:
        _audit(request, user, "subscription_chat", name[0], to="group" if chat_id else "personal")
    return RedirectResponse("/subscriptions", status_code=303)


@app.get("/subscriptions")
def subscriptions(request: Request, user: User):
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.query, s.min_amount, s.max_amount, s.active,
                      (SELECT count(*) FROM matches m WHERE m.subscription_id = s.id),
                      s.kind, s.filters, s.scope, s.chat_id
               FROM subscriptions s WHERE s.user_id = %s ORDER BY s.id""",
            (user,),
        ).fetchall()
        groups = _user_groups(conn, user)
    filters = {r[0]: Filters.from_json(r[8]) for r in rows if r[7] == "announcements"}
    return templates.TemplateResponse(
        request, "subscriptions.html",
        {"rows": rows, "filters": filters, "groups": groups},
    )


@app.post("/subscriptions", dependencies=[Depends(same_origin)])
def create_subscription(
    request: Request,
    user: User,
    name: Annotated[str, Form()],
    query: Annotated[str, Form()],
    min_amount: Annotated[str, Form()] = "",
    max_amount: Annotated[str, Form()] = "",
):
    if not name.strip() or not query.strip():
        raise HTTPException(422, "нужны название и запрос")
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO subscriptions (user_id, name, query, min_amount, max_amount,
                                          matched_up_to)
               VALUES (%s, %s, %s, %s, %s,
                       (SELECT coalesce(max(seen_seq), 0) FROM lots))""",
            (user, name.strip(), query.strip(), _amount(min_amount), _amount(max_amount)),
        )
    _audit(request, user, "subscription_create", name.strip()[:100], words=query.strip()[:200])
    return RedirectResponse("/subscriptions", status_code=303)


@app.post("/subscriptions/{sub_id}/toggle", dependencies=[Depends(same_origin)])
def toggle_subscription(request: Request, user: User, sub_id: int):
    with pool.connection() as conn:
        row = conn.execute(
            """UPDATE subscriptions SET active = NOT active WHERE id = %s AND user_id = %s
               RETURNING name, active""",
            (sub_id, user),
        ).fetchone()
    if row:
        _audit(request, user, "subscription_toggle", row[0], active=row[1])
    return RedirectResponse("/subscriptions", status_code=303)


@app.post("/subscriptions/{sub_id}/delete", dependencies=[Depends(same_origin)])
def delete_subscription(request: Request, user: User, sub_id: int):
    with pool.connection() as conn:
        row = conn.execute(
            "DELETE FROM subscriptions WHERE id = %s AND user_id = %s RETURNING name",
            (sub_id, user),
        ).fetchone()
    if row:
        _audit(request, user, "subscription_delete", row[0])
    return RedirectResponse("/subscriptions", status_code=303)


@app.get("/matches")
def matches(request: Request, user: User):
    query = parse_qs(request.url.query)
    chosen = [int(v) for v in query.get("sub", []) if v.isdigit()]
    with pool.connection() as conn:
        subs = conn.execute(
            """SELECT s.id, s.name, s.active,
                      (SELECT count(*) FROM matches m WHERE m.subscription_id = s.id)
               FROM subscriptions s WHERE s.user_id = %s ORDER BY s.id""",
            (user,),
        ).fetchall()
        # Первый заход — первая подписка; сняты все галочки — пусто.
        if "filtered" not in query and not chosen and subs:
            chosen = [subs[0][0]]
        rows = conn.execute(
            """SELECT m.matched_at, s.name, l.trd_buy_id, l.name_ru, l.amount,
                      coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date, l.prepayment,
                      coalesce(l.extra_desc, l.description_ru), a.number_anno
               FROM matches m
               JOIN subscriptions s ON s.id = m.subscription_id
               JOIN lots l ON l.id = m.lot_id
               LEFT JOIN announcements a ON a.id = l.trd_buy_id
               WHERE s.user_id = %(user)s
                 AND s.id = ANY(%(chosen)s)
               ORDER BY m.matched_at DESC, a.end_date, l.trd_buy_id, l.id LIMIT 300""",
            {"user": user, "chosen": chosen},
        ).fetchall()
    return templates.TemplateResponse(
        request, "matches.html", {"rows": rows, "subs": subs, "chosen": chosen}
    )


@app.get("/settings")
def settings(request: Request, user: User, sent: str = "", saved: str = ""):
    return _settings_page(request, user, sent=sent, saved=saved)


def _settings_page(request: Request, user: int, *, sent: str = "", saved: str = "",
                   notice: str | None = None, error: str | None = None, status_code: int = 200,
                   tab: str | None = None):
    is_admin = request.state.role == "admin"
    with pool.connection() as conn:
        chat_id, company_bin = conn.execute(
            "SELECT telegram_chat_id, company_bin FROM users WHERE id = %s", (user,)
        ).fetchone()
        groups = _user_groups(conn, user)
        ai = conn.execute("SELECT ai_provider, ai_model, ai_model_light FROM users WHERE id = %s",
                          (user,)).fetchone()
        ai_keys = secrets_box.saved_providers(conn, user)
        users = conn.execute(
            """SELECT u.id, u.email, u.role, u.telegram_chat_id IS NOT NULL,
                      (SELECT max(created_at) FROM sessions s WHERE s.user_id = u.id),
                      u.disabled
               FROM users u ORDER BY u.role, u.email"""
        ).fetchall() if is_admin else []
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "is_admin": is_admin,
            "connected": bool(chat_id),
            "bot_username": telegram.bot_username(),
            "configured": bool(telegram.token() and telegram.bot_username()),
            "sent": sent,
            "saved": saved,
            "notice": notice,
            "error": error,
            "company_bin": company_bin,
            "groups": groups,
            "ai_provider": ai[0],
            "ai_model": ai[1] or "",
            "ai_model_light": ai[2] or research.SUMMARY_MODELS.get(ai[0] or "claude_code", ""),
            "ai_keys": ai_keys,
            "providers": research.PROVIDERS,
            "default_models": research.DEFAULT_MODELS,
            "models": research.MODELS,
            "light_defaults": research.SUMMARY_MODELS,
            "tab": tab or {"ai": "ai", "bin": "company"}.get(saved),
            "users": users,
            "me": user,
            "min_password": auth.MIN_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )


@app.post("/settings/password", dependencies=[Depends(same_origin)])
def change_password(
    request: Request,
    user: User,
    current: Annotated[str, Form()],
    password: Annotated[str, Form()],
    repeat: Annotated[str, Form()],
):
    with pool.connection() as conn:
        if not auth.check_password(conn, user, current):
            error = "Текущий пароль неверный."
        elif password != repeat:
            error = "Новые пароли не совпали."
        elif len(password) < auth.MIN_PASSWORD_LENGTH:
            error = f"Пароль — не короче {auth.MIN_PASSWORD_LENGTH} символов."
        else:
            error = None
            auth.set_password(conn, request.state.email, password)
    if error:
        return _settings_page(request, user, error=error, status_code=422, tab="account")
    _audit(request, user, "password_change")
    # Смена пароля сбрасывает все сессии, в том числе эту: входим заново.
    return RedirectResponse("/login", status_code=303)


@app.post("/settings/users", dependencies=[Depends(same_origin)])
def add_user(request: Request, admin: Admin, login: Annotated[str, Form()]):
    login = auth.normalize_email(login)
    if not login or len(login) > 100 or any(c.isspace() for c in login):
        return _settings_page(request, admin, error="Логин — без пробелов, до 100 символов.",
                              status_code=422, tab="users")
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = %s", (login,)).fetchone():
            return _settings_page(request, admin, error=f"Пользователь {login} уже есть.",
                                  status_code=422, tab="users")
        password = auth.generate_password()
        auth.set_password(conn, login, password)
    _audit(request, admin, "user_add", login)
    return _settings_page(request, admin, notice=f"Создан пользователь {login}. Пароль: {password} "
                          "— передайте его, больше он нигде не показывается.", tab="users")


def _other_user(conn, admin: int, user_id: int) -> str:
    """Логин пользователя, которым управляет администратор; себя трогать нельзя."""
    if user_id == admin:
        raise HTTPException(422, "Свою роль, доступ и аккаунт изменить здесь нельзя")
    row = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Нет такого пользователя")
    return row[0]


@app.post("/settings/users/{user_id}/role", dependencies=[Depends(same_origin)])
def set_role(request: Request, admin: Admin, user_id: int, role: Annotated[str, Form()]):
    if role not in ("admin", "user"):
        raise HTTPException(422, "неизвестная роль")
    with pool.connection() as conn:
        login = _other_user(conn, admin, user_id)
        conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
    _audit(request, admin, "user_role", login, role=role)
    return _settings_page(request, admin, tab="users", notice=f"{login}: роль — "
                          f"{'администратор' if role == 'admin' else 'пользователь'}.")


@app.post("/settings/users/{user_id}/disable", dependencies=[Depends(same_origin)])
def toggle_disabled(request: Request, admin: Admin, user_id: int):
    with pool.connection() as conn:
        login = _other_user(conn, admin, user_id)
        disabled = conn.execute(
            "UPDATE users SET disabled = NOT disabled WHERE id = %s RETURNING disabled", (user_id,)
        ).fetchone()[0]
        if disabled:
            # Отключение действует сразу: выкидываем из всех открытых сессий.
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    _audit(request, admin, "user_disable", login, disabled=disabled)
    return _settings_page(request, admin, tab="users", notice=f"{login}: аккаунт "
                          f"{'отключён — войти нельзя' if disabled else 'снова включён'}.")


@app.post("/settings/users/{user_id}/delete", dependencies=[Depends(same_origin)])
def delete_user(request: Request, admin: Admin, user_id: int):
    with pool.connection() as conn:
        login = _other_user(conn, admin, user_id)
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
    _audit(request, admin, "user_delete", login)
    return _settings_page(request, admin, tab="users",
                          notice=f"Пользователь {login} удалён вместе с подписками и отметками.")


@app.post("/settings/users/{user_id}/reset", dependencies=[Depends(same_origin)])
def reset_password(request: Request, admin: Admin, user_id: int):
    with pool.connection() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Нет такого пользователя")
        password = auth.generate_password()
        auth.set_password(conn, row[0], password)
    _audit(request, admin, "user_reset", row[0])
    if user_id == admin:
        return RedirectResponse("/login", status_code=303)
    return _settings_page(request, admin, notice=f"Новый пароль для {row[0]}: {password} — "
                          "передайте его, больше он нигде не показывается. Старые входы сброшены.",
                          tab="users")


@app.post("/settings/ai", dependencies=[Depends(same_origin)])
def save_ai(
    request: Request,
    user: Admin,
    provider: Annotated[str, Form()],
    model: Annotated[str, Form()] = "",
    model_light: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
):
    if provider not in research.PROVIDERS:
        raise HTTPException(422, "неизвестный провайдер")
    with pool.connection() as conn:
        if api_key.strip() and provider != "claude_code":
            try:
                secrets_box.save_key(conn, user, provider, api_key)
            except secrets_box.NoSecretKey:
                raise HTTPException(503, "На сервере не настроено шифрование ключей") from None
        if provider != "claude_code" and provider not in secrets_box.saved_providers(conn, user):
            conn.rollback()
            return _settings_page(request, user, error="Для этого способа нужен API-ключ.",
                                  status_code=422, tab="ai")
        # У Claude Code модель не выбирается: берётся модель по умолчанию.
        chosen = None if provider == "claude_code" else (model.strip()[:100] or None)
        light = None if provider == "claude_code" else (model_light.strip()[:100] or None)
        conn.execute(
            "UPDATE users SET ai_provider = %s, ai_model = %s, ai_model_light = %s WHERE id = %s",
            (provider, chosen, light, user),
        )
    _audit(request, user, "settings_ai", provider, model=chosen, model_light=light,
           new_key=bool(api_key.strip()))
    return RedirectResponse("/settings?saved=ai#ai", status_code=303)


_models_cache: dict[tuple[int, str], tuple[float, list]] = {}


@app.get("/settings/ai/models")
def ai_models(user: Admin, provider: str):
    if provider not in research.MODELS:
        raise HTTPException(404, "нет такого провайдера")
    cached = _models_cache.get((user, provider))
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with pool.connection() as conn:
        key = secrets_box.user_key(conn, user, provider)
    if not key:
        return []
    try:
        models = research.provider_models(provider, key)
    except (httpx.HTTPError, KeyError, ValueError):
        return []
    _models_cache[(user, provider)] = (time.monotonic() + 3600, models)
    return models


@app.post("/settings/company", dependencies=[Depends(same_origin)])
def save_company(request: Request, user: Admin, company_bin: Annotated[str, Form()] = ""):
    company_bin = company_bin.strip()
    if company_bin and not (company_bin.isdigit() and len(company_bin) == 12):
        raise HTTPException(422, "БИН или ИИН — 12 цифр")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE users SET company_bin = %s WHERE id = %s", (company_bin or None, user)
        )
    _audit(request, user, "settings_company", company_bin or "—")
    return RedirectResponse("/settings?saved=bin#company", status_code=303)


@app.post("/settings/telegram/link", dependencies=[Depends(same_origin)])
def telegram_link(request: Request, user: User):
    if not telegram.bot_username():
        raise HTTPException(503, "Telegram-бот не настроен")
    with pool.connection() as conn:
        code = bot.create_link_code(conn, user)
    _audit(request, user, "telegram_link")
    return RedirectResponse(bot.link_url(code), status_code=303)


@app.post("/settings/telegram/group", dependencies=[Depends(same_origin)])
def telegram_group_link(request: Request, user: Admin):
    if not telegram.bot_username():
        raise HTTPException(503, "Telegram-бот не настроен")
    with pool.connection() as conn:
        code = bot.create_link_code(conn, user, "group")
    _audit(request, user, "group_link")
    return RedirectResponse(bot.group_link_url(code), status_code=303)


@app.post("/settings/groups/remove", dependencies=[Depends(same_origin)])
def telegram_group_remove(request: Request, user: Admin, chat_id: Annotated[str, Form()]):
    # Группы общие: отключение убирает её у всех, их подписки уходят в личку.
    with pool.connection() as conn:
        title = conn.execute(
            "SELECT title FROM telegram_groups WHERE chat_id = %s", (chat_id,)
        ).fetchone()
        bot.unlink_group(conn, chat_id)
    _audit(request, user, "group_remove", title[0] if title else chat_id)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/telegram/unlink", dependencies=[Depends(same_origin)])
def telegram_unlink(request: Request, user: User):
    with pool.connection() as conn:
        conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE id = %s", (user,))
    _audit(request, user, "telegram_unlink")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/telegram/test", dependencies=[Depends(same_origin)])
def telegram_test(user: User):
    with pool.connection() as conn:
        chat_id = conn.execute(
            "SELECT telegram_chat_id FROM users WHERE id = %s", (user,)
        ).fetchone()[0]
    if not chat_id:
        return RedirectResponse("/settings", status_code=303)
    try:
        telegram.send_message(chat_id, "Проверка: уведомления zakup приходят сюда.")
        result = "ok"
    except telegram.TelegramError:
        result = "fail"
    return RedirectResponse(f"/settings?sent={result}", status_code=303)
