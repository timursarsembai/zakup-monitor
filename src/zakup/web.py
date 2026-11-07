"""Веб-интерфейс: поиск по лотам, подписки, совпадения.

Вход по e-mail и паролю, сессия в cookie. Регистрации пока нет: пользователей
заводит `zakup set-password`.
"""

import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from zakup import auth, bot, telegram
from zakup.matcher import PORTAL_ANNOUNCEMENT_URL

PAGE_SIZE = 50
SESSION_COOKIE = "zakup_session"

pool = ConnectionPool(os.environ.get("DATABASE_URL", ""), min_size=1, max_size=5, open=False)
templates = Jinja2Templates(directory=str(files("zakup").joinpath("templates")))
templates.env.globals["announcement_url"] = PORTAL_ANNOUNCEMENT_URL.format
templates.env.filters["money"] = lambda v: "" if v is None else f"{v:,.2f}".replace(",", " ")


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
            error = None if token else "Неверный e-mail или пароль."
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
def search(
    request: Request,
    _: User,
    q: str = "",
    min_amount: str = "",
    max_amount: str = "",
    open_only: bool = False,
    page: int = 1,
):
    page = max(page, 1)
    where, params = [], {}
    if q.strip():
        where.append("l.tsv @@ websearch_to_tsquery('russian', %(q)s)")
        params["q"] = q
    if (low := _amount(min_amount)) is not None:
        where.append("l.amount >= %(min)s")
        params["min"] = low
    if (high := _amount(max_amount)) is not None:
        where.append("l.amount <= %(max)s")
        params["max"] = high
    if open_only:
        where.append("a.end_date > now()")
    sql = f"""SELECT l.id, l.trd_buy_id, l.number_anno, l.lot_number, l.name_ru, l.amount,
                     l.customer_name_ru, a.end_date
              FROM lots l LEFT JOIN announcements a ON a.id = l.trd_buy_id
              {"WHERE " + " AND ".join(where) if where else ""}
              ORDER BY l.id DESC LIMIT {PAGE_SIZE + 1} OFFSET {(page - 1) * PAGE_SIZE}"""
    with pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        stats = conn.execute(
            """SELECT (SELECT count(*) FROM announcements), (SELECT count(*) FROM lots),
                      (SELECT max(updated_at) FROM sync_state)"""
        ).fetchone()
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "rows": rows[:PAGE_SIZE],
            "has_next": len(rows) > PAGE_SIZE,
            "page": page,
            "q": q,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "open_only": open_only,
            "stats": stats,
        },
    )


@app.get("/subscriptions")
def subscriptions(request: Request, user: User):
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.query, s.min_amount, s.max_amount, s.active,
                      (SELECT count(*) FROM matches m WHERE m.subscription_id = s.id)
               FROM subscriptions s WHERE s.user_id = %s ORDER BY s.id""",
            (user,),
        ).fetchall()
    return templates.TemplateResponse(request, "subscriptions.html", {"rows": rows})


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
                      l.customer_name_ru, a.end_date
               FROM matches m
               JOIN subscriptions s ON s.id = m.subscription_id
               JOIN lots l ON l.id = m.lot_id
               LEFT JOIN announcements a ON a.id = l.trd_buy_id
               WHERE s.user_id = %s
               ORDER BY m.matched_at DESC, l.id DESC LIMIT 200""",
            (user,),
        ).fetchall()
    return templates.TemplateResponse(request, "matches.html", {"rows": rows})


@app.get("/settings")
def settings(request: Request, user: User, sent: str = ""):
    with pool.connection() as conn:
        chat_id = conn.execute(
            "SELECT telegram_chat_id FROM users WHERE id = %s", (user,)
        ).fetchone()[0]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": bool(chat_id),
            "bot_username": telegram.bot_username(),
            "configured": bool(telegram.token() and telegram.bot_username()),
            "sent": sent,
        },
    )


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
