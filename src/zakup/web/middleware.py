"""Обработчики каждого запроса: язык, статистика посещений, заголовки безопасности;
перенаправления «войдите» и «нужен тариф».
"""

import functools
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from zakup import (
    i18n,
    visits,
)
from zakup.web import core
from zakup.web.core import (
    VIEWED_PAGES,
    LoginRequired,
    PaymentRequired,
    _audit,
    _record_visit,
)


async def language(request: Request, call_next):
    i18n.set_language(request.cookies.get(i18n.COOKIE))
    return await call_next(request)


async def page_views(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    user = getattr(request.state, "user_id", None)
    path = request.url.path
    if visits.wanted(request.method, path, response.status_code,
                     response.headers.get("content-type", ""), request.headers.get("sec-fetch-dest")):
        visitor = request.cookies.get(visits.COOKIE)
        agent = request.headers.get("user-agent")
        if not visitor and not visits.is_bot(agent):
            visitor = visits.new_visitor_id()
            response.set_cookie(visits.COOKIE, visitor, max_age=visits.COOKIE_AGE, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https")
        # После ответа и в фоне: посетитель не ждёт записи.
        core._spawn(functools.partial(
            _record_visit, visitor=visitor, user_id=user, path=path, query=request.url.query,
            referrer=request.headers.get("referer"), user_agent=agent,
            ip=request.client.host if request.client else None,
            own_host=(request.headers.get("host") or "").split(":")[0].removeprefix("www."),
            status=response.status_code, ms=int((time.monotonic() - started) * 1000)))
    if (user and request.method == "GET" and response.status_code == 200
            and request.url.path in VIEWED_PAGES):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        await run_in_threadpool(_audit, request, user, "view_page", target[:500])
    return response


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Сайт не встраивается в чужие страницы (кнопки удаления и т.п. — в один клик).
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/settings"):
        # Здесь показываются выданные пароли: не кэшировать.
        response.headers["Cache-Control"] = "no-store"
    return response


def _to_login(request: Request, _: LoginRequired):
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


def _to_pricing(request: Request, _: PaymentRequired):
    if request.method == "GET":
        return RedirectResponse("/pricing", status_code=303)
    return JSONResponse({"paywall": True, "detail": "Платная функция"}, status_code=402)


def install(app) -> None:
    """Порядок как раньше: добавленный позже оборачивает раньше добавленные."""
    for handler in (language, page_views, security_headers):
        app.middleware("http")(handler)
    app.exception_handler(LoginRequired)(_to_login)
    app.exception_handler(PaymentRequired)(_to_pricing)
