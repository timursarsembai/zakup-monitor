"""Веб-интерфейс: поиск по лотам, подписки, совпадения.

Вход по e-mail и паролю, сессия в cookie. Регистрации пока нет: пользователей
заводит `zakup set-password`.
"""

import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Annotated
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from zakup import auth, bot, details, marks, results, spec, telegram
from zakup.filters import (
    DEADLINE_RULE,
    GROUP_ORDER_BY,
    LOT_ORDER_BY,
    REGIONS,
    STATUSES,
    Filters,
    deadline_by_rule,
)
from zakup.goszakup import PORTAL_TZ, Client, GoszakupError, parse_date
from zakup.matcher import PORTAL_ANNOUNCEMENT_URL

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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
    return user[0]


User = Annotated[int, Depends(current_user)]


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
def search(request: Request, user: User, page: int = 1, hidden: int = 0):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form)
    page = max(page, 1)
    now = datetime.now(UTC)
    where, params = current.where(now)
    params["user"] = user
    hidden_where = where
    if not hidden:
        # Лоты, отмеченные «Не подходит», в выдаче не мешают.
        where += """ AND NOT EXISTS (SELECT 1 FROM lot_marks m WHERE m.user_id = %(user)s
                                     AND m.lot_id = l.id AND m.status = 'rejected')"""
    with pool.connection() as conn:
        groups = conn.execute(
            f"""SELECT a.id, a.number_anno, a.name_ru, a.end_date,
                       coalesce(a.customer_name_ru, a.org_name_ru), count(*), sum(l.amount),
                       max(l.prepayment), a.count_lots,
                       a.detail->'RefBuyStatus'->>'nameRu'
                FROM lots l, announcements a WHERE {where}
                GROUP BY a.id ORDER BY {GROUP_ORDER_BY}
                LIMIT {PAGE_SIZE + 1} OFFSET {(page - 1) * PAGE_SIZE}""",
            params,
        ).fetchall()
        ids = [g[0] for g in groups[:PAGE_SIZE]]
        lots: dict[int, list] = {}
        for row in conn.execute(
            f"""SELECT l.trd_buy_id, l.id, l.lot_number, l.name_ru,
                       coalesce(l.extra_desc, l.description_ru), l.count, l.unit_name, l.price,
                       l.amount, l.prepayment, coalesce(nullif(l.kato, '{{}}'), a.kato),
                       r.outcome, r.winner_name, r.winner_total, jsonb_array_length(r.offers),
                       r.reason
                FROM lots l JOIN announcements a ON true
                LEFT JOIN lot_results r ON r.lot_id = l.id
                WHERE {where} AND a.id = ANY(%(ids)s)
                ORDER BY {LOT_ORDER_BY}""",
            {**params, "ids": ids},
        ):
            lots.setdefault(row[0], []).append(row)
        totals = conn.execute(
            f"SELECT count(DISTINCT a.id), count(*) FROM lots l, announcements a WHERE {where}",
            params,
        ).fetchone()
        hidden_count = conn.execute(
            f"""SELECT count(*) FROM lots l, announcements a, lot_marks m
                WHERE {hidden_where} AND m.user_id = %(user)s AND m.lot_id = l.id
                  AND m.status = 'rejected'""",
            params,
        ).fetchone()[0]
        lot_marks = marks.of_user(conn, user, [row[1] for rows in lots.values() for row in rows])
        stats = conn.execute(
            """SELECT (SELECT count(*) FROM announcements WHERE end_date > now()),
                      (SELECT count(*) FROM announcements
                       WHERE end_date > now() AND details_fetched_at IS NULL),
                      (SELECT max(updated_at) FROM sync_state)"""
        ).fetchone()
    query = {k: v for k, v in form.items() if k not in ("page", "hidden")}
    query["applied"] = ["1"]
    today = now.astimezone(PORTAL_TZ).date()
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "groups": groups[:PAGE_SIZE],
            "lots": lots,
            "has_next": len(groups) > PAGE_SIZE,
            "page": page,
            "f": current,
            "totals": totals,
            "deadline_date": current.deadline_date(today),
            "rule_date": deadline_by_rule(today),
            "statuses": STATUSES,
            "regions": REGIONS,
            "rule": DEADLINE_RULE,
            "query": urlencode(query, doseq=True),
            "stats": stats,
            "marks": lot_marks,
            "hidden": hidden,
            "hidden_count": hidden_count,
            "here": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        },
    )


DETAIL_TTL = timedelta(minutes=15)
_client: Client | None = None


def _goszakup() -> Client:
    global _client
    if _client is None:
        _client = Client(os.environ.get("GOSZAKUP_TOKEN", ""), delay=0, attempts=2)
    return _client


@app.get("/a/{announcement_id}")
def announcement(request: Request, user: User, announcement_id: int):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT detail, details_fetched_at, end_date FROM announcements WHERE id = %s",
            (announcement_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Такого объявления нет в базе")
        detail, fetched_at, end_date = row
        now = datetime.now(UTC)
        stale = fetched_at is None or now - fetched_at > (
            DETAIL_TTL if end_date and end_date > now else timedelta(days=1)
        )
        refresh_error = None
        if detail is None or stale:
            try:
                details.fetch_one(conn, _goszakup(), announcement_id)
                detail, fetched_at = conn.execute(
                    "SELECT detail, details_fetched_at FROM announcements WHERE id = %s",
                    (announcement_id,),
                ).fetchone()
            except GoszakupError:
                conn.rollback()
                refresh_error = "Портал сейчас не отвечает, показаны сохранённые данные."
        if detail is None:
            raise HTTPException(502, "Не удалось получить объявление с портала")
        outcome, results_at = conn.execute(
            "SELECT results, results_fetched_at FROM announcements WHERE id = %s",
            (announcement_id,),
        ).fetchone()
        # Итоги, договоры и апелляции появляются после окончания приёма заявок.
        if end_date and end_date < now and results.stale(results_at, now) and not refresh_error:
            try:
                results.refresh(conn, _goszakup(), announcement_id)
                outcome, results_at = conn.execute(
                    "SELECT results, results_fetched_at FROM announcements WHERE id = %s",
                    (announcement_id,),
                ).fetchone()
            except GoszakupError:
                conn.rollback()
                refresh_error = "Портал сейчас не отвечает, итоги могут быть неполными."
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
        lot_marks = marks.of_user(conn, user, list(info))
        lot_results = results.by_lot(conn, list(info))
        company_bin = conn.execute(
            "SELECT company_bin FROM users WHERE id = %s", (user,)
        ).fetchone()[0]
    # Сначала лоты с авансом побольше, дальше по номеру.
    lots.sort(key=lambda lot: (-(info[lot["id"]]["prepayment"] or 0), lot.get("lotNumber") or ""))
    return templates.TemplateResponse(
        request,
        "announcement.html",
        {
            "a": detail,
            "lots": lots,
            "info": info,
            "subscribed": subscribed,
            "marks": lot_marks,
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
        },
    )


@app.get("/a/{announcement_id}/lots/{lot_id}/spec")
def lot_spec(request: Request, _: User, announcement_id: int, lot_id: int):
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
                       FROM jsonb_array_elements(a.detail->'Lots') lot
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
    sections = {status: [] for status in marks.ORDER}
    for row in rows:
        sections.setdefault(row[0], []).append(row)
    return templates.TemplateResponse(
        request, "my.html", {"sections": sections, "now": datetime.now(UTC)}
    )


@app.post("/subscriptions/filter", dependencies=[Depends(same_origin)])
async def create_filter_subscription(request: Request, user: User):
    form = await request.form()
    data = {key: form.getlist(key) for key in form.keys()}
    current = Filters.from_form(data)
    name = (form.get("name") or "").strip() or "Мой фильтр"
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO subscriptions (user_id, name, query, kind, filters)
               VALUES (%s, %s, '', 'announcements', %s)""",
            (user, name[:100], Jsonb(current.to_json())),
        )
    return RedirectResponse("/subscriptions", status_code=303)


@app.get("/subscriptions")
def subscriptions(request: Request, user: User):
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.query, s.min_amount, s.max_amount, s.active,
                      (SELECT count(*) FROM matches m WHERE m.subscription_id = s.id),
                      s.kind, s.filters
               FROM subscriptions s WHERE s.user_id = %s ORDER BY s.id""",
            (user,),
        ).fetchall()
    described = {r[0]: Filters.from_json(r[8]).describe() for r in rows if r[7] == "announcements"}
    return templates.TemplateResponse(
        request, "subscriptions.html", {"rows": rows, "described": described}
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
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT m.matched_at, s.name, l.trd_buy_id, l.name_ru, l.amount,
                      coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date, l.prepayment,
                      coalesce(l.extra_desc, l.description_ru), a.number_anno
               FROM matches m
               JOIN subscriptions s ON s.id = m.subscription_id
               JOIN lots l ON l.id = m.lot_id
               LEFT JOIN announcements a ON a.id = l.trd_buy_id
               WHERE s.user_id = %(user)s
               ORDER BY m.matched_at DESC, a.end_date, l.trd_buy_id, l.id LIMIT 300""",
            {"user": user},
        ).fetchall()
    return templates.TemplateResponse(request, "matches.html", {"rows": rows})


@app.get("/settings")
def settings(request: Request, user: User, sent: str = "", saved: str = ""):
    with pool.connection() as conn:
        chat_id, company_bin = conn.execute(
            "SELECT telegram_chat_id, company_bin FROM users WHERE id = %s", (user,)
        ).fetchone()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": bool(chat_id),
            "bot_username": telegram.bot_username(),
            "configured": bool(telegram.token() and telegram.bot_username()),
            "sent": sent,
            "saved": saved,
            "company_bin": company_bin,
        },
    )


@app.post("/settings/company", dependencies=[Depends(same_origin)])
def save_company(user: User, company_bin: Annotated[str, Form()] = ""):
    company_bin = company_bin.strip()
    if company_bin and not (company_bin.isdigit() and len(company_bin) == 12):
        raise HTTPException(422, "БИН или ИИН — 12 цифр")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE users SET company_bin = %s WHERE id = %s", (company_bin or None, user)
        )
    return RedirectResponse("/settings?saved=bin", status_code=303)


@app.post("/settings/telegram/link", dependencies=[Depends(same_origin)])
def telegram_link(user: User):
    if not telegram.bot_username():
        raise HTTPException(503, "Telegram-бот не настроен")
    with pool.connection() as conn:
        code = bot.create_link_code(conn, user)
    return RedirectResponse(bot.link_url(code), status_code=303)


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
