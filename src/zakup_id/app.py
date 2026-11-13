"""Сайт сервиса учётных записей (id.tenderim.com).

Для браузера — только приём форм входа и выхода: страницы рисует основной сайт, форма
отправляется сюда, после ответа браузер возвращается на основной сайт (с жетоном в
cookie на весь домен или с кодом ошибки). Для основного сайта — /api/* по общему ключу.
"""

import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from psycopg_pool import ConnectionPool

from zakup_id import accounts, config, db, token

logger = logging.getLogger(__name__)
DEVICE_COOKIE = "tdev"
DEVICE_TTL = 365 * 24 * 3600

pool: ConnectionPool | None = None
CLEANUP_EVERY = 3600
_cleaned_at = 0.0


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    with db.connect(config.database_url()) as conn:
        db.migrate(conn)
    pool = ConnectionPool(config.database_url(), min_size=1, max_size=5, open=True,
                          kwargs={"options": "-c statement_timeout=10000"})
    try:
        yield
    finally:
        pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def safe_next(target: str | None) -> str:
    """Только пути основного сайта: иначе ссылка на вход уводила бы на чужой адрес."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def from_site(request: Request) -> None:
    """Формы принимаем только со страниц основного сайта (вторая защита после SameSite)."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parts = urlsplit(origin)
    if f"{parts.scheme}://{parts.netloc}" != config.site_url():
        raise HTTPException(403, "cross-origin request")


def service(request: Request) -> None:
    key = config.service_key()
    sent = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not key or not hmac.compare_digest(sent.encode(), key.encode()):
        raise HTTPException(401, "service key required")


def _ip(request: Request) -> str | None:
    # За прокси настоящий адрес подставляет uvicorn (--proxy-headers) из X-Forwarded-For —
    # только от адреса самого прокси (--forwarded-allow-ips=ID_PROXY_IP, не «*»): иначе
    # клиент подставил бы любой адрес и обошёл предел неудачных входов с одного IP.
    return request.client.host if request.client else None


def _back(path: str, **query: str) -> RedirectResponse:
    tail = f"?{urlencode(query)}" if query else ""
    return RedirectResponse(f"{config.site_url()}{path}{tail}", status_code=303)


def _token_cookie(response: RedirectResponse, value: str | None) -> RedirectResponse:
    if value is None:
        response.delete_cookie(token.COOKIE, domain=config.cookie_domain(), secure=True,
                               httponly=True, samesite="lax")
    else:
        response.set_cookie(token.COOKIE, value, max_age=int(accounts.SESSION_TTL.total_seconds()),
                            domain=config.cookie_domain(), secure=True, httponly=True, samesite="lax")
    return response


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/login", dependencies=[Depends(from_site)])
def login(request: Request, login: Annotated[str, Form()], password: Annotated[str, Form()],
          next: Annotated[str, Form()] = "/"):
    global _cleaned_at
    target = safe_next(next)
    with pool.connection() as conn:
        result = accounts.login(conn, login, password, _ip(request), request.headers.get("user-agent"),
                                request.cookies.get(DEVICE_COOKIE), config.device_key())
        if time.monotonic() - _cleaned_at > CLEANUP_EVERY:
            _cleaned_at = time.monotonic()
            accounts.cleanup(conn)
    if isinstance(result, str):
        # Логин обратно не передаём: телефон в адресе попал бы в журналы основного сайта.
        return _back("/login", error=result, next=target)
    account_id, sid, device_gen = result
    response = _token_cookie(_back(target), token.issue(config.signing_key(), account_id, sid))
    response.set_cookie(DEVICE_COOKIE, accounts.device_mark(config.device_key(), account_id, device_gen),
                        max_age=DEVICE_TTL, secure=True, httponly=True, samesite="lax")
    return response


@app.post("/logout", dependencies=[Depends(from_site)])
def logout(request: Request):
    claims = token.read(request.cookies.get(token.COOKIE), token.public_key(config.signing_key()))
    if claims:
        with pool.connection() as conn:
            accounts.end_session(conn, claims.sid)
    return _token_cookie(_back("/"), None)


@app.post("/api/renew", dependencies=[Depends(service)])
def renew(value: Annotated[str, Body(embed=True, alias="token")]):
    """Старый жетон → новый, если сессия жива. 401 — сессия закрыта или жетон уже обменян:
    сайт забывает вход."""
    claims = token.read(value, token.public_key(config.signing_key()))
    if claims is None:
        raise HTTPException(401, "bad token")
    with pool.connection() as conn:
        gen = accounts.renew(conn, claims.user_id, claims.sid, claims.gen)
    if gen is None:
        raise HTTPException(401, "session ended")
    return {"token": token.issue(config.signing_key(), claims.user_id, claims.sid, gen)}


@app.post("/api/revoke", dependencies=[Depends(service)])
def revoke(user_id: Annotated[int, Body()], block: Annotated[bool | None, Body()] = None):
    """Выйти везде: жетоны перестанут продлеваться. block=True — блокировка или удаление
    (и войти снова нельзя), False — снять блокировку."""
    with pool.connection() as conn:
        return {"ended": accounts.end_all_sessions(conn, user_id, block)}


@app.post("/api/notify", dependencies=[Depends(service)])
def notify(user_id: Annotated[int, Body()], text: Annotated[str, Body()],
           buttons: Annotated[list[list[dict]] | None, Body()] = None):
    """Сообщение пользователю в Telegram: основной сайт знает только номер, чат — здесь."""
    # Соединение не держим, пока ждём Telegram (до 15 с): иначе медленный Telegram
    # занял бы все соединения, и встали бы вход и продление.
    with pool.connection() as conn:
        row = conn.execute("SELECT telegram_chat_id FROM accounts WHERE id = %s AND NOT disabled",
                           (user_id,)).fetchone()
    if not row or not row[0]:
        return {"sent": False, "reason": "no_chat"}
    bot = config.telegram_token()
    if not bot:
        return {"sent": False, "reason": "not_configured"}
    payload = {"chat_id": row[0], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        reply = httpx.post(f"https://api.telegram.org/bot{bot}/sendMessage", json=payload, timeout=15)
        data = reply.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("telegram to %s failed: %s", user_id, type(error).__name__)
        raise HTTPException(502, "telegram unavailable") from None
    if data.get("ok"):
        return {"sent": True}
    # Пользователь удалил чат или заблокировал бота — больше не пытаемся.
    if data.get("error_code") == 403 or "chat not found" in str(data.get("description", "")):
        with pool.connection() as conn:
            conn.execute("UPDATE accounts SET telegram_chat_id = NULL WHERE id = %s AND telegram_chat_id = %s",
                         (user_id, row[0]))
        return {"sent": False, "reason": "chat_gone"}
    logger.warning("telegram to %s refused: %s", user_id, data.get("description"))
    return {"sent": False, "reason": "refused"}
