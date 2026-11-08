"""Вход, регистрация и восстановление пароля по SMS, шаг «Ваша организация», выход."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from zakup import (
    auth,
    i18n,
    signup,
    subjects,
)
from zakup.goszakup import GoszakupError
from zakup.web import core
from zakup.web.core import (
    RETURN_COOKIE,
    SESSION_COOKIE,
    User,
    _audit,
    _safe_next,
    _with_session,
    same_origin,
    templates,
)

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/", notice: str = ""):
    return _login_form(request, next, staff=False, notice=notice)


@router.get("/staff/login")
def staff_login_page(request: Request, next: str = "/"):
    return _login_form(request, next, staff=True)


def _login_form(request: Request, next: str, staff: bool, notice: str = ""):
    with core.pool.connection() as conn:
        if auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(_safe_next(next), status_code=303)
    notices = {"reset": i18n.gettext("Пароль изменён — войдите с новым паролем.")}
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "email": "", "error": None, "staff": staff,
        "notice": notices.get(notice)})


@router.post("/login", dependencies=[Depends(same_origin)])
def login(request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()],
          next: Annotated[str, Form()] = "/"):
    # Клиенты входят по телефону; логин без цифр (старые аккаунты) тоже принимаем.
    return _login_attempt(request, auth.normalize_phone(email) or email, password, next, staff=False)


@router.post("/staff/login", dependencies=[Depends(same_origin)])
def staff_login(request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()],
                next: Annotated[str, Form()] = "/"):
    return _login_attempt(request, email, password, next, staff=True)


DEVICE_COOKIE = "zakup_dev"
DEVICE_TTL = 365 * 24 * 3600


def _remember_device(response, user_id: int):
    """Браузер, где пользователь уже входил: чужие неверные пароли его не блокируют."""
    if mark := auth.device_mark(user_id):
        response.set_cookie(DEVICE_COOKIE, mark, max_age=DEVICE_TTL, httponly=True, secure=True,
                            samesite="lax")
    return response


def _login_attempt(request: Request, email: str, password: str, next: str, staff: bool):
    ip = request.client.host if request.client else None
    email_norm = auth.normalize_email(email)
    with core.pool.connection() as conn:
        # Проверка лимита, пароль и запись попытки — по очереди для одного логина: иначе
        # параллельные запросы видят одно и то же старое число неудач.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("login:" + email_norm,))
        account = conn.execute("SELECT id FROM users WHERE email = %s", (email_norm,)).fetchone()
        known = auth.known_device(request.cookies.get(DEVICE_COOKIE), account[0] if account else None)
        locked = auth.too_many_failures(conn, email_norm, ip, known)
        if locked:
            token = None
            error = i18n.gettext("Слишком много неудачных попыток. Попробуйте через 15 минут.")
        else:
            token = auth.login(conn, email_norm, password, ip, request.headers.get("user-agent"))
            error = None if token else i18n.gettext(
                "Неверный логин или пароль." if staff else "Неверный телефон или пароль.")
    if token is None:
        # Логин — только если такой аккаунт есть: в поле логина бывает вставлен пароль.
        _audit(request, None, "login_failed", login=email_norm[:100] if account else None,
               reason="lock" if locked else "password")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "email": email, "error": error, "staff": staff, "notice": None},
            status_code=429 if locked else 401,
        )
    with core.pool.connection() as conn:
        user_id, role = conn.execute("SELECT id, role FROM users WHERE email = %s", (email_norm,)).fetchone()
    _audit(request, user_id, "login")
    target = _safe_next(next)
    if target == "/" and role in auth.STAFF_ROLES:
        target = "/dashboard"  # главная сотрудника — дашборд платформы
    return _remember_device(_with_session(RedirectResponse(target, status_code=303), token), user_id)


def _phone_page(request: Request, purpose: str, phone: str = "", error: str | None = None,
                status_code: int = 200, closed: bool = False):
    register = purpose == "register"
    return templates.TemplateResponse(request, "phone_step.html", {
        "title": i18n.gettext("Регистрация" if register else "Восстановление пароля"),
        "hint": i18n.gettext("Пришлём код по SMS, затем вы придумаете пароль." if register else
                             "Пришлём код по SMS на номер, с которым вы регистрировались."),
        "action": "/register" if register else "/forgot", "phone": phone, "error": error,
        "closed": closed}, status_code=status_code)


def _code_page(request: Request, purpose: str, flow: str, phone: str, error: str | None = None,
               status_code: int = 200):
    register = purpose == "register"
    return templates.TemplateResponse(request, "code_step.html", {
        "title": i18n.gettext("Регистрация" if register else "Новый пароль"),
        "action": "/register/code" if register else "/forgot/code", "flow": flow, "phone": phone,
        "reset": not register, "error": error,
        "again": "/register" if register else "/forgot"}, status_code=status_code)


@router.get("/register")
def register_page(request: Request):
    with core.pool.connection() as conn:
        closed = not signup.registration_open(conn)
    return _phone_page(request, "register", closed=closed)


@router.get("/forgot")
def forgot_page(request: Request):
    return _phone_page(request, "reset")


@router.post("/register", dependencies=[Depends(same_origin)])
def register_start(request: Request, phone: Annotated[str, Form()]):
    return _start(request, "register", phone)


@router.post("/forgot", dependencies=[Depends(same_origin)])
def forgot_start(request: Request, phone: Annotated[str, Form()]):
    return _start(request, "reset", phone)


PHONE_CHECKS_PER_HOUR = 10


def _start(request: Request, purpose: str, raw_phone: str):
    phone = auth.normalize_phone(raw_phone)
    if phone is None:
        return _phone_page(request, purpose, raw_phone,
                           i18n.gettext("Нужен казахстанский мобильный номер: +7 7XX XXX XX XX."), 422)
    ip = request.client.host if request.client else None
    with core.pool.connection() as conn:
        if purpose == "register" and not signup.registration_open(conn):
            return _phone_page(request, purpose, closed=True, status_code=403)
        # Ответ говорит, есть ли такой номер: проверок с одного адреса — не больше
        # стольких в час, иначе по ответам можно перебрать номера всех клиентов.
        checked = conn.execute(
            """SELECT count(*) FROM login_attempts WHERE email = '#phone-check' AND ip = %s
               AND created_at > now() - interval '1 hour'""", (ip,)).fetchone()[0]
        if checked >= PHONE_CHECKS_PER_HOUR:
            return _phone_page(request, purpose, raw_phone,
                               i18n.gettext("Слишком много попыток. Попробуйте через час."), 429)
        conn.execute("INSERT INTO login_attempts (email, ip, ok) VALUES ('#phone-check', %s, true)", (ip,))
        conn.commit()
        user = conn.execute("SELECT deleted_at, may_reregister FROM users WHERE email = %s",
                            (phone,)).fetchone()
        if purpose == "register" and user and user[0] is None:
            return _phone_page(request, purpose, raw_phone, i18n.gettext(
                "Этот номер уже зарегистрирован — войдите или восстановите пароль."), 422)
        if purpose == "register" and user and not user[1]:
            return _phone_page(request, purpose, raw_phone, i18n.gettext(
                "Регистрация с этим номером недоступна. Обратитесь в поддержку."), 403)
        if purpose == "reset" and (user is None or user[0] is not None):
            return _phone_page(request, purpose, raw_phone, i18n.gettext(
                "Такой номер не зарегистрирован."), 404)
        try:
            flow = signup.start(conn, phone, purpose, ip)
        except signup.SignupError as error:
            return _phone_page(request, purpose, raw_phone, i18n.gettext(str(error)), 429)
    _audit(request, None, "otp_sent", login=phone, purpose=purpose)
    return _code_page(request, purpose, flow, phone)


@router.post("/register/code", dependencies=[Depends(same_origin)])
def register_finish(request: Request, flow: Annotated[str, Form()], code: Annotated[str, Form()],
                    password: Annotated[str, Form()], password2: Annotated[str, Form()]):
    return _finish(request, "register", flow, code, password, password2)


@router.post("/forgot/code", dependencies=[Depends(same_origin)])
def forgot_finish(request: Request, flow: Annotated[str, Form()], code: Annotated[str, Form()],
                  password: Annotated[str, Form()], password2: Annotated[str, Form()]):
    return _finish(request, "reset", flow, code, password, password2)


def _finish(request: Request, purpose: str, flow: str, code: str, password: str, password2: str):
    with core.pool.connection() as conn:
        phone = signup.pending(conn, flow, purpose)
        if phone is None:
            return _phone_page(request, purpose, error=i18n.gettext("Код устарел. Запросите новый."),
                               status_code=410)
        # Пароль проверяем до кода: ошибка в пароле не должна сжигать попытку кода.
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            return _code_page(request, purpose, flow, phone,
                              i18n.gettext("Пароль — не короче 10 знаков."), 422)
        if password != password2:
            return _code_page(request, purpose, flow, phone, i18n.gettext("Пароли не совпадают."), 422)
        # Администратор мог закрыть регистрацию, пока клиент ждал SMS.
        if purpose == "register" and not signup.registration_open(conn):
            return _phone_page(request, purpose, closed=True, status_code=403)
        try:
            phone = signup.verify(conn, flow, purpose, code)
        except signup.SignupError as error:
            return _code_page(request, purpose, flow, phone, i18n.gettext(str(error)), 422)
        if purpose == "register":
            old = conn.execute("SELECT id, deleted_at, may_reregister FROM users WHERE email = %s",
                               (phone,)).fetchone()
            if old and old[1] is None:
                return _phone_page(request, purpose, phone, i18n.gettext(
                    "Этот номер уже зарегистрирован — войдите или восстановите пароль."), 422)
            taken = conn.execute("SELECT 1 FROM users WHERE phone = %s AND id <> %s",
                                 (phone, old[0] if old else 0)).fetchone()
            if (old and not old[2]) or taken:
                return _phone_page(request, purpose, phone, i18n.gettext(
                    "Регистрация с этим номером недоступна. Обратитесь в поддержку."), 403)
            if old:
                # Удалённому разрешили заново: старая запись остаётся под другим логином.
                conn.execute("UPDATE users SET email = %s, phone = NULL WHERE id = %s",
                             (f"{phone}#deleted-{old[0]}", old[0]))
            user_id = auth.set_password(conn, phone, password)
            conn.execute("UPDATE users SET phone = %s, role = 'user' WHERE id = %s", (phone, user_id))
        else:
            user_id = auth.set_password(conn, phone, password)
        conn.commit()
        token = auth.login(conn, phone, password, request.client.host if request.client else None,
                           request.headers.get("user-agent"))
    _audit(request, user_id, "register" if purpose == "register" else "password_reset_sms")
    if token is None:  # аккаунт успели отключить или удалить
        return RedirectResponse("/login", status_code=303)
    target = "/welcome/company" if purpose == "register" else "/"
    return _remember_device(_with_session(RedirectResponse(target, status_code=303), token), user_id)


ORG_KINDS = {"ip": "ИП", "too": "ТОО", "other": "Другое"}


@router.get("/welcome/company")
def company_step(request: Request, user: User):
    with core.pool.connection() as conn:
        row = conn.execute("SELECT kind, bin, name FROM client_orgs WHERE user_id = %s", (user,)).fetchone()
    kind, bin_, name = row or ("", "", "")
    return templates.TemplateResponse(request, "company_step.html", {
        "kinds": ORG_KINDS, "kind": kind, "bin": bin_, "name": name, "error": None})


@router.get("/welcome/company/lookup")
def company_lookup(_: User, bin: str):
    if not (bin.isdigit() and len(bin) == 12):
        return {"name": None}
    with core.pool.connection() as conn:
        found = subjects.card(conn, bin)
        if found is None:
            try:
                subjects.ensure(conn, core._goszakup(), bin)
                found = subjects.card(conn, bin)
            except (GoszakupError, httpx.HTTPError):
                found = None
    return {"name": (found or {}).get("name_ru")}


@router.post("/welcome/company", dependencies=[Depends(same_origin)])
def company_save(request: Request, user: User, kind: Annotated[str, Form()],
                 bin: Annotated[str, Form()], name: Annotated[str, Form()]):
    bin = bin.strip()
    name = " ".join(name.split())[:300]
    if kind not in ORG_KINDS or not (bin.isdigit() and len(bin) == 12) or not name:
        return templates.TemplateResponse(request, "company_step.html", {
            "kinds": ORG_KINDS, "kind": kind, "bin": bin, "name": name,
            "error": i18n.gettext("Выберите форму, укажите 12 цифр БИН/ИИН и название.")},
            status_code=422)
    with core.pool.connection() as conn:
        conn.execute(
            """INSERT INTO client_orgs (user_id, kind, bin, name) VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind, bin = excluded.bin,
                   name = excluded.name, updated_at = now()""",
            (user, kind, bin, name),
        )
        # Этот же БИН отмечает «Вы выиграли / не выиграли» в итогах.
        conn.execute("UPDATE users SET company_bin = %s WHERE id = %s AND company_bin IS NULL",
                     (bin, user))
    _audit(request, user, "client_org", bin, kind=kind)
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(same_origin)])
def logout(request: Request):
    with core.pool.connection() as conn:
        who = auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE))
        auth.logout(conn, request.cookies.get(SESSION_COOKIE))
        # Выход «из-под пользователя» целиком: сессия администратора тоже закрывается.
        auth.logout(conn, request.cookies.get(RETURN_COOKIE))
    if who:
        _audit(request, who[0], "logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax")
    response.delete_cookie(RETURN_COOKIE, httponly=True, secure=True, samesite="lax")
    return response
