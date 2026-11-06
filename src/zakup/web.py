"""Веб-интерфейс: поиск по лотам, подписки, совпадения.

Пока один владелец за HTTP Basic. Для сервиса на многих пользователей сюда встанет
нормальный вход (ссылка на почту или Telegram), таблица users уже готова.
"""

import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from zakup.matcher import PORTAL_ANNOUNCEMENT_URL

PAGE_SIZE = 50

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
security = HTTPBasic()


def current_user(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> int:
    user = os.environ.get("ZAKUP_WEB_USER", "")
    password = os.environ.get("ZAKUP_WEB_PASSWORD", "")
    user_ok = secrets.compare_digest(credentials.username.encode(), user.encode())
    password_ok = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (password and user_ok and password_ok):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = %s", (user,)
        ).fetchone() or conn.execute(
            """INSERT INTO users (email) VALUES (%s)
               ON CONFLICT (email) DO UPDATE SET email = excluded.email RETURNING id""",
            (user,),
        ).fetchone()
    return row[0]


User = Annotated[int, Depends(current_user)]


def same_origin(request: Request) -> None:
    # Браузер сам подставляет Basic-пароль, поэтому чужой сайт мог бы отправить форму за нас.
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request")


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
               VALUES (%s, %s, %s, %s, %s, (SELECT coalesce(max(id), 0) FROM lots))""",
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
