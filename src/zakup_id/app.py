"""Сайт сервиса учётных записей (id.tenderim.com).

Вход и выход: форму рисует основной сайт, отправляется она сюда, после ответа браузер
возвращается на основной сайт (с жетоном в cookie на весь домен или с кодом ошибки).
Регистрация, восстановление и смена пароля — свои страницы (templates/page.html): телефон
и пароль вводятся только здесь. Для основного сайта — /api/* по общему ключу.
"""

import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from zakup import i18n, signup
from zakup.auth import MIN_PASSWORD_LENGTH, normalize_phone
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
templates = Jinja2Templates(directory=str(files("zakup_id").joinpath("templates")))
templates.env.globals["_"] = i18n.gettext
PHONE_CHECKS_PER_HOUR = 10


def safe_next(target: str | None) -> str:
    """Только пути основного сайта: иначе ссылка на вход уводила бы на чужой адрес."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def from_site(request: Request) -> None:
    """Формы принимаем только со страниц основного сайта и самого сервиса (вторая защита
    после SameSite)."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parts = urlsplit(origin)
    if (f"{parts.scheme}://{parts.netloc}" != config.site_url()
            and parts.netloc != request.headers.get("host")):
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


def _signed_in(response: RedirectResponse, account_id: int, sid: str,
               gen: int | None = None) -> RedirectResponse:
    """Жетон входа и метка браузера — после входа, регистрации, смены пароля.
    gen — поколение меток браузера, если уже известно (вход), иначе читаем."""
    if gen is None:
        with pool.connection() as conn:
            gen = conn.execute("SELECT device_gen FROM accounts WHERE id = %s", (account_id,)).fetchone()[0]
    response.set_cookie(DEVICE_COOKIE, accounts.device_mark(config.device_key(), account_id, gen),
                        max_age=DEVICE_TTL, secure=True, httponly=True, samesite="lax")
    return _token_cookie(response, token.issue(config.signing_key(), account_id, sid))


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
    return _signed_in(_back(target), account_id, sid, device_gen)


@app.post("/logout", dependencies=[Depends(from_site)])
def logout(request: Request):
    claims = token.read(request.cookies.get(token.COOKIE), token.public_key(config.signing_key()))
    if claims:
        with pool.connection() as conn:
            accounts.end_session(conn, claims.sid)
    return _token_cookie(_back("/"), None)


def _lang(value: str | None) -> str:
    i18n.set_language(value)
    return i18n.language()


def _page(request: Request, step: str, purpose: str, status_code: int = 200, **values):
    _ = i18n.gettext
    register = purpose == "register"
    titles = {"register": _("Регистрация"), "reset": _("Новый пароль") if step == "code" else
              _("Восстановление пароля"), "password": _("Смена пароля")}
    hints = {"register": _("Пришлём код по SMS, затем вы придумаете пароль."),
             "reset": _("Пришлём код по SMS на номер, с которым вы регистрировались.")}
    path = {"register": "/register", "reset": "/forgot", "password": "/password"}[purpose]
    context = {"lang": i18n.language(), "site": config.site_url(), "step": step, "purpose": purpose,
               "title": titles[purpose], "hint": hints.get(purpose) if step == "phone" else None,
               "action": path + ("/code" if step == "code" else ""), "phone": "", "error": None,
               "again": f"{path}?lang={i18n.language()}" if step == "code" else None}
    if register and step == "message":
        context["message"] = _("Регистрация временно закрыта.")
    context.update(values)
    return templates.TemplateResponse(request, "page.html", context, status_code=status_code)


@app.get("/register")
def register_page(request: Request, lang: str = "ru"):
    _lang(lang)
    with pool.connection() as conn:
        if not signup.registration_open(conn):
            return _page(request, "message", "register", 403)
    return _page(request, "phone", "register")


@app.get("/forgot")
def forgot_page(request: Request, lang: str = "ru"):
    _lang(lang)
    return _page(request, "phone", "reset")


@app.post("/register", dependencies=[Depends(from_site)])
def register_start(request: Request, phone: Annotated[str, Form()], lang: Annotated[str, Form()] = "ru"):
    _lang(lang)
    return _start(request, "register", phone)


@app.post("/forgot", dependencies=[Depends(from_site)])
def forgot_start(request: Request, phone: Annotated[str, Form()], lang: Annotated[str, Form()] = "ru"):
    _lang(lang)
    return _start(request, "reset", phone)


def _start(request: Request, purpose: str, raw_phone: str):
    _ = i18n.gettext
    phone = normalize_phone(raw_phone)
    if phone is None:
        return _page(request, "phone", purpose, 422, phone=raw_phone,
                     error=_("Нужен казахстанский мобильный номер: +7 7XX XXX XX XX."))
    ip = _ip(request)
    # Своё соединение, не из пула: отправка SMS (kazpayment) ждёт до ~25 с, и медленный
    # kazpayment не должен занять соединения входа и продления.
    with db.connect(config.database_url()) as conn:
        if purpose == "register" and not signup.registration_open(conn):
            return _page(request, "message", purpose, 403)
        # Ответ говорит, есть ли такой номер: проверок с одной подсети (/24, у IPv6 — /64,
        # как у пределов SMS) — не больше стольких в час, иначе по ответам можно перебрать
        # номера всех клиентов. IP не пишем: эти записи не должны считаться неудачными входами.
        if accounts.claim_attempt(conn, f"#phone-check:{signup._network(ip)}", None,
                                  PHONE_CHECKS_PER_HOUR, timedelta(hours=1)) is None:
            return _page(request, "phone", purpose, 429, phone=raw_phone,
                         error=_("Слишком много попыток. Попробуйте через час."))
        status = accounts.phone_status(conn, phone)
        if purpose == "register" and status == "taken":
            return _page(request, "phone", purpose, 422, phone=raw_phone,
                         error=_("Этот номер уже зарегистрирован — войдите или восстановите пароль."))
        if purpose == "register" and status == "closed":
            return _page(request, "phone", purpose, 403, phone=raw_phone,
                         error=_("Регистрация с этим номером недоступна. Обратитесь в поддержку."))
        reset = accounts.reset_status(conn, phone) if purpose == "reset" else None
        if reset == "none":
            return _page(request, "phone", purpose, 404, phone=raw_phone,
                         error=_("Такой номер не зарегистрирован."))
        if reset == "blocked":   # SMS платные — заблокированному не шлём
            return _page(request, "phone", purpose, 403, phone=raw_phone, error=_(
                "Восстановление пароля для этого номера недоступно. Обратитесь в поддержку."))
        try:
            flow = signup.start(conn, phone, purpose, ip)
        except signup.SignupError as error:
            return _page(request, "phone", purpose, 429, phone=raw_phone, error=error.translated())
    return _page(request, "code", purpose, flow=flow, phone=phone)


@app.post("/register/code", dependencies=[Depends(from_site)])
def register_finish(request: Request, flow: Annotated[str, Form()], code: Annotated[str, Form()],
                    password: Annotated[str, Form()], password2: Annotated[str, Form()],
                    lang: Annotated[str, Form()] = "ru"):
    _lang(lang)
    return _finish(request, "register", flow, code, password, password2)


@app.post("/forgot/code", dependencies=[Depends(from_site)])
def forgot_finish(request: Request, flow: Annotated[str, Form()], code: Annotated[str, Form()],
                  password: Annotated[str, Form()], password2: Annotated[str, Form()],
                  lang: Annotated[str, Form()] = "ru"):
    _lang(lang)
    return _finish(request, "reset", flow, code, password, password2)


def _finish(request: Request, purpose: str, flow: str, code: str, password: str, password2: str):
    _ = i18n.gettext
    with db.connect(config.database_url()) as conn:   # проверка кода — тоже запрос в kazpayment
        phone = signup.pending(conn, flow, purpose)
        if phone is None:
            return _page(request, "phone", purpose, 410, error=_("Код устарел. Запросите новый."))
        # Пароль проверяем до кода: ошибка в пароле не должна сжигать попытку кода.
        error = (_("Пароль — не короче 10 знаков.") if len(password) < MIN_PASSWORD_LENGTH else
                 _("Пароли не совпадают.") if password != password2 else None)
        if error:
            return _page(request, "code", purpose, 422, flow=flow, phone=phone, error=error)
        # Администратор мог закрыть регистрацию, пока клиент ждал SMS.
        if purpose == "register" and not signup.registration_open(conn):
            return _page(request, "message", purpose, 403)
        try:
            phone = signup.verify(conn, flow, purpose, code)
        except signup.SignupError as error:
            return _page(request, "code", purpose, 422, flow=flow, phone=phone, error=error.translated())
        if purpose == "register":
            account_id = accounts.register(conn, phone, password)
            if account_id == "taken":
                return _page(request, "phone", purpose, 422, phone=phone, error=_(
                    "Этот номер уже зарегистрирован — войдите или восстановите пароль."))
            if account_id == "closed":
                return _page(request, "phone", purpose, 403, phone=phone, error=_(
                    "Регистрация с этим номером недоступна. Обратитесь в поддержку."))
            target = "/welcome/company"
        else:
            account_id = accounts.reset_status(conn, phone)
            if account_id == "none":
                return _page(request, "phone", purpose, 404, error=_("Такой номер не зарегистрирован."))
            if account_id == "blocked":   # заблокировали, пока шла SMS: пароль не меняем
                return _page(request, "phone", purpose, 403, error=_(
                    "Восстановление пароля для этого номера недоступно. Обратитесь в поддержку."))
            accounts.set_password(conn, account_id, password)
            target = "/"
        sid = accounts.create_session(conn, account_id, _ip(request), request.headers.get("user-agent"))
        conn.commit()
    return _signed_in(_back(target), account_id, sid)


def _session(request: Request) -> int | None:
    """Номер вошедшего пользователя по жетону в cookie (страницы самого сервиса)."""
    claims = token.read(request.cookies.get(token.COOKIE), token.public_key(config.signing_key()))
    if claims is None:
        return None
    if not claims.usable(datetime.now(UTC)):
        return None
    with pool.connection() as conn:
        alive = accounts.session_account(conn, claims.user_id, claims.sid, claims.gen)
    return claims.user_id if alive else None


@app.get("/password")
def password_page(request: Request, lang: str = "ru"):
    _lang(lang)
    if _session(request) is None:
        return _back("/login", next="/settings")
    return _page(request, "password", "password")


@app.post("/password", dependencies=[Depends(from_site)])
def password_change(request: Request, current: Annotated[str, Form()], password: Annotated[str, Form()],
                    password2: Annotated[str, Form()], lang: Annotated[str, Form()] = "ru"):
    _ = i18n.gettext
    _lang(lang)
    account_id = _session(request)
    if account_id is None:
        return _back("/login", next="/settings")
    mark = f"#password:{account_id}"   # подбор текущего пароля по чужой сессии — тот же предел
    with pool.connection() as conn:
        # Попытка пишется неудачной до проверки: параллельные запросы не обходят предел.
        attempt = accounts.claim_attempt(conn, mark, None, accounts.MAX_FAILURES_PER_LOGIN)
        if attempt is None:
            error = _("Слишком много неудачных попыток. Попробуйте через 15 минут.")
        elif not accounts.check_password(conn, account_id, current):
            error = _("Текущий пароль неверный.")
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = _("Пароль — не короче 10 знаков.")
        elif password != password2:
            error = _("Пароли не совпадают.")
        else:
            error = None
            accounts.confirm_attempt(conn, attempt)
            accounts.set_password(conn, account_id, password)   # закрывает все входы…
            sid = accounts.create_session(conn, account_id, _ip(request), request.headers.get("user-agent"))
            conn.commit()                                       # …кроме этого, нового
    if error:
        return _page(request, "password", "password", 422, error=error)
    return _signed_in(_back("/settings", notice="password"), account_id, sid)


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
        row = conn.execute("""SELECT telegram_chat_id FROM accounts
                              WHERE id = %s AND NOT disabled AND deleted_at IS NULL""",
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


@app.post("/api/registration", dependencies=[Depends(service)])
def registration(open: Annotated[bool, Body(embed=True)]):
    """Открыть или закрыть регистрацию (переключатель — у сотрудников на основном сайте)."""
    with pool.connection() as conn:
        signup.set_registration_open(conn, open)
    return {"open": open}


@app.get("/api/accounts/{user_id}", dependencies=[Depends(service)])
def account(user_id: int):
    """Что сайту нужно о новой записи без личных данных: прежняя запись того же номера."""
    with pool.connection() as conn:
        row = conn.execute("SELECT previous_id, deleted_at IS NOT NULL FROM accounts WHERE id = %s",
                           (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such account")
    return {"previous_id": row[0], "deleted": row[1]}


@app.post("/api/status", dependencies=[Depends(service)])
def status(user_id: Annotated[int, Body()], deleted: Annotated[bool | None, Body()] = None,
           may_reregister: Annotated[bool | None, Body()] = None):
    """Удаление (закрывает все входы) и разрешение зарегистрироваться заново."""
    with pool.connection() as conn:
        result = accounts.set_status(conn, user_id, deleted, may_reregister)
    if result == "missing":
        raise HTTPException(404, "no such account")
    if result == "replaced":
        raise HTTPException(409, "number re-registered: this record cannot be restored")
    return {"ok": True}
