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
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from zakup import (
    SITE_URL,
    auth,
    bot,
    details,
    marks,
    matcher,
    research,
    results,
    secrets_box,
    spec,
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
    {REGIONS[k[:2]] for k in kato or [] if k[:2] in REGIONS}
)


def _time_left(end: datetime | None) -> str:
    if end is None:
        return ""
    seconds = int((end - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "приём окончен"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return f"через {days} д {hours} ч" if days else f"через {hours} ч {rest // 60} мин"


templates.env.globals["time_left"] = _time_left
templates.env.globals["mark_statuses"] = marks.STATUSES
templates.env.globals["site_url"] = SITE_URL


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
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Сайт не встраивается в чужие страницы (кнопки удаления и т.п. — в один клик).
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/settings"):
        # Здесь показываются выданные пароли: не кэшировать.
        response.headers["Cache-Control"] = "no-store"
    return response


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
        if auth.too_many_failures(conn, email_norm, ip):
            error, token = "Слишком много неудачных попыток. Попробуйте через 15 минут.", None
        else:
            token = auth.login(conn, email_norm, password, ip, request.headers.get("user-agent"))
            error = None if token else "Неверный логин или пароль."
    if token is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "email": email, "error": error},
            status_code=429 if error.startswith("Слишком") else 401,
        )
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
        auth.logout(conn, request.cookies.get(SESSION_COOKIE))
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
def home(request: Request, _: User):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/announcements{query}", status_code=307)


def _user_groups(conn, user: int) -> list[tuple[str, str]]:
    """Подключённые пользователем Telegram-группы: [(chat_id, название)]."""
    return conn.execute(
        """SELECT g.chat_id, coalesce(g.title, 'Группа') FROM user_groups ug
           JOIN telegram_groups g ON g.chat_id = ug.chat_id
           WHERE ug.user_id = %s ORDER BY g.title""",
        (user,),
    ).fetchall()


def _search_context(request: Request, current: Filters, scope: str, total: int, page: int,
                    form: dict, now: datetime) -> dict:
    today = now.astimezone(PORTAL_TZ).date()
    return {
        "scope": scope,
        "f": current,
        "total": total,
        "page": page,
        "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
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
        "query": urlsplit(current.to_query(scope)).query,
        "here": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
    }


@app.get("/lots")
def lots_search(request: Request, user: User, page: int = 1, hidden: int = 0):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form)
    page = max(page, 1)
    now = datetime.now(UTC)
    where, params = current.where(now, "lots")
    params["user"] = user
    hidden_where = where
    if not hidden:
        # Лоты, отмеченные «Не подходит», в выдаче не мешают.
        where += """ AND NOT EXISTS (SELECT 1 FROM lot_marks m WHERE m.user_id = %(user)s
                                     AND m.lot_id = l.id AND m.status = 'rejected')"""
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT l.id, l.lot_number, a.id, a.number_anno, a.name_ru,
                       coalesce(a.customer_name_ru, l.customer_name_ru, a.org_name_ru),
                       l.name_ru, coalesce(l.extra_desc, l.description_ru), l.count,
                       l.unit_name, l.price, l.amount, l.prepayment, a.trade_method_id,
                       l.status_id, a.end_date, coalesce(nullif(l.kato, '{{}}'), a.kato),
                       r.outcome, r.winner_name, r.winner_total,
                       jsonb_array_length(r.offers), r.reason
                FROM lots l JOIN announcements a ON true
                LEFT JOIN lot_results r ON r.lot_id = l.id
                WHERE {where} ORDER BY {current.order_by("lots")}
                LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) FROM lots l, announcements a WHERE {where}", params
        ).fetchone()[0]
        hidden_count = conn.execute(
            f"""SELECT count(*) FROM lots l, announcements a, lot_marks m
                WHERE {hidden_where} AND m.user_id = %(user)s AND m.lot_id = l.id
                  AND m.status = 'rejected'""",
            params,
        ).fetchone()[0]
        lot_marks = marks.of_user(conn, user, [row[0] for row in rows])
        stats = conn.execute(
            """SELECT (SELECT count(*) FROM announcements WHERE end_date > now()),
                      (SELECT count(*) FROM announcements
                       WHERE end_date > now() AND details_fetched_at IS NULL),
                      (SELECT max(updated_at) FROM sync_state)"""
        ).fetchone()
        groups = _user_groups(conn, user)
    context = _search_context(request, current, "lots", total, page, form, now)
    context.update(rows=rows, marks=lot_marks, hidden=hidden, hidden_count=hidden_count,
                   stats=stats, groups=groups)
    return templates.TemplateResponse(request, "lots.html", context)


@app.get("/announcements")
def announcements_search(request: Request, user: User, page: int = 1):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form)
    page = max(page, 1)
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
                        FROM lots p WHERE p.trd_buy_id = a.id)
                FROM announcements a WHERE {where} ORDER BY {current.order_by("announcements")}
                LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) FROM announcements a WHERE {where}", params
        ).fetchone()[0]
        groups = _user_groups(conn, user)
    context = _search_context(request, current, "announcements", total, page, form, now)
    context.update(rows=rows, groups=groups)
    return templates.TemplateResponse(request, "announcements.html", context)


EXPORT_LIMIT = 10_000


@app.get("/export.xlsx")
def export(request: Request, user: User):
    from openpyxl import Workbook

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
        lot_info = research.lot_context(conn, lot_id)
        provider = (conn.execute(
            "SELECT ai_provider FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone() or ("claude_code",))[0]
    context.update(lot=lot, others=[other for other in context["lots"] if other["id"] != lot_id],
                   here=request.url.path, job=job, lot_info=lot_info,
                   region=research.default_region(lot_info) if lot_info else "",
                   provider_name=research.PROVIDERS.get(provider, provider),
                   is_admin=request.state.role == "admin")
    return templates.TemplateResponse(request, "lot.html", context)


@app.post("/lots/{lot_id}/research", dependencies=[Depends(same_origin)])
def start_research(
    request: Request,
    user: User,
    lot_id: int,
    region: Annotated[str, Form()] = "",
    whole_kz: Annotated[str, Form()] = "",
):
    with pool.connection() as conn:
        if research.lot_context(conn, lot_id) is None:
            raise HTTPException(404, "Нет данных лота: откройте страницу объявления")
        try:
            research.enqueue(conn, user, lot_id, region, whole_kz == "1",
                             is_admin=request.state.role == "admin")
        except research.QuotaExceeded as error:
            raise HTTPException(429, f"Лимит поиска товаров: {error}") from None
    return RedirectResponse(f"/lots/{lot_id}#research", status_code=303)


@app.get("/lots/{lot_id}/research")
def research_fragment(request: Request, _: User, lot_id: int):
    with pool.connection() as conn:
        job = research.latest(conn, lot_id)
        lot_info = research.lot_context(conn, lot_id)
    return templates.TemplateResponse(
        request, "research_fragment.html",
        {"job": job, "lot_info": lot_info, "is_admin": request.state.role == "admin"},
    )


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
        specs = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT lot_id, text, error FROM lot_specs WHERE lot_id = ANY(%s)", (list(info),))}
        lot_marks = marks.of_user(conn, user, list(info))
        lot_results = results.by_lot(conn, list(info))
        company_bin = conn.execute(
            "SELECT company_bin FROM users WHERE id = %s", (user,)
        ).fetchone()[0]
    # Сначала лоты с авансом побольше, дальше по номеру.
    lots.sort(key=lambda lot: (-(info[lot["id"]]["prepayment"] or 0), lot.get("lotNumber") or ""))
    return {
        "a": detail,
        "lots": lots,
        "info": info,
        "subscribed": subscribed,
        "marks": lot_marks,
        "specs": specs,
        "results": lot_results,
        "contracts": (outcome or {}).get("contracts") or [],
        "contracts_by_lot": results.contracts_by_lot(outcome),
        "appeals": (outcome or {}).get("appeals") or [],
        "results_at": results_at,
        "now": now,
        "company_bin": company_bin,
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
    return templates.TemplateResponse(
        request, "spec_fragment.html", {"text": text, "error": error, "url": url}
    )


@app.post("/lots/{lot_id}/mark", dependencies=[Depends(same_origin)])
def mark_lot(
    user: User,
    lot_id: int,
    status: Annotated[str, Form()] = "",
    note: Annotated[str | None, Form()] = None,
    next: Annotated[str, Form()] = "/my",
):
    if status and status not in marks.STATUSES:
        raise HTTPException(422, "неизвестная отметка")
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM lots WHERE id = %s", (lot_id,)).fetchone():
            raise HTTPException(404, "Такого лота нет в базе")
        if note is not None:
            marks.set_note(conn, user, lot_id, note)
        else:
            marks.set_mark(conn, user, lot_id, status)
    target = _safe_next(next)
    return RedirectResponse(f"{target}#lot-{lot_id}" if "#" not in target else target, status_code=303)


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
    chosen = [v for v in parse_qs(request.url.query).get("status", []) if v in marks.STATUSES]
    sections = {status: [] for status in marks.ORDER}
    for row in rows:
        sections.setdefault(row[0], []).append(row)
    counts = {status: len(items) for status, items in sections.items()}
    if chosen:
        sections = {status: items for status, items in sections.items() if status in chosen}
    return templates.TemplateResponse(
        request, "my.html",
        {"sections": sections, "counts": counts, "chosen": chosen, "now": datetime.now(UTC),
         "here": request.url.path + (f"?{request.url.query}" if request.url.query else "")},
    )


@app.post("/subscriptions/filter", dependencies=[Depends(same_origin)])
async def create_filter_subscription(request: Request, user: User):
    form = await request.form()
    data = {key: form.getlist(key) for key in form.keys()}
    current = Filters.from_form(data)
    if not current.relative():
        raise HTTPException(
            422, "Подписка работает только без конкретных дат: срок — «через 2 рабочих дня» "
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
    return RedirectResponse("/subscriptions", status_code=303)


def _own_chat(conn, user: int, chat_id: str) -> str | None:
    """Группа, в которую пользователь вправе слать; пусто — личный чат."""
    if not chat_id:
        return None
    if chat_id not in {group[0] for group in _user_groups(conn, user)}:
        raise HTTPException(403, "Эта группа не подключена к вашему аккаунту")
    return chat_id


@app.post("/subscriptions/{sub_id}/chat", dependencies=[Depends(same_origin)])
def subscription_chat(user: User, sub_id: int, chat_id: Annotated[str, Form()] = ""):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE subscriptions SET chat_id = %s WHERE id = %s AND user_id = %s",
            (_own_chat(conn, user, chat_id), sub_id, user),
        )
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
    return RedirectResponse("/subscriptions", status_code=303)


@app.post("/subscriptions/{sub_id}/toggle", dependencies=[Depends(same_origin)])
def toggle_subscription(user: User, sub_id: int):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE subscriptions SET active = NOT active WHERE id = %s AND user_id = %s",
            (sub_id, user),
        )
    return RedirectResponse("/subscriptions", status_code=303)


@app.post("/subscriptions/{sub_id}/delete", dependencies=[Depends(same_origin)])
def delete_subscription(user: User, sub_id: int):
    with pool.connection() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id = %s AND user_id = %s", (sub_id, user))
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
        ai = conn.execute("SELECT ai_provider, ai_model FROM users WHERE id = %s", (user,)).fetchone()
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
            "ai_keys": ai_keys,
            "providers": research.PROVIDERS,
            "default_models": research.DEFAULT_MODELS,
            "models": research.MODELS,
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
    return _settings_page(request, admin, tab="users", notice=f"{login}: аккаунт "
                          f"{'отключён — войти нельзя' if disabled else 'снова включён'}.")


@app.post("/settings/users/{user_id}/delete", dependencies=[Depends(same_origin)])
def delete_user(request: Request, admin: Admin, user_id: int):
    with pool.connection() as conn:
        login = _other_user(conn, admin, user_id)
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
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
        conn.execute(
            "UPDATE users SET ai_provider = %s, ai_model = %s WHERE id = %s",
            (provider, chosen, user),
        )
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
def save_company(user: Admin, company_bin: Annotated[str, Form()] = ""):
    company_bin = company_bin.strip()
    if company_bin and not (company_bin.isdigit() and len(company_bin) == 12):
        raise HTTPException(422, "БИН или ИИН — 12 цифр")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE users SET company_bin = %s WHERE id = %s", (company_bin or None, user)
        )
    return RedirectResponse("/settings?saved=bin#company", status_code=303)


@app.post("/settings/telegram/link", dependencies=[Depends(same_origin)])
def telegram_link(user: User):
    if not telegram.bot_username():
        raise HTTPException(503, "Telegram-бот не настроен")
    with pool.connection() as conn:
        code = bot.create_link_code(conn, user)
    return RedirectResponse(bot.link_url(code), status_code=303)


@app.post("/settings/telegram/group", dependencies=[Depends(same_origin)])
def telegram_group_link(user: Admin):
    if not telegram.bot_username():
        raise HTTPException(503, "Telegram-бот не настроен")
    with pool.connection() as conn:
        code = bot.create_link_code(conn, user, "group")
    return RedirectResponse(bot.group_link_url(code), status_code=303)


@app.post("/settings/groups/remove", dependencies=[Depends(same_origin)])
def telegram_group_remove(user: Admin, chat_id: Annotated[str, Form()]):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE subscriptions SET chat_id = NULL WHERE user_id = %s AND chat_id = %s",
            (user, chat_id),
        )
        conn.execute(
            "DELETE FROM user_groups WHERE user_id = %s AND chat_id = %s", (user, chat_id)
        )
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/telegram/unlink", dependencies=[Depends(same_origin)])
def telegram_unlink(user: User):
    with pool.connection() as conn:
        conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE id = %s", (user,))
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
