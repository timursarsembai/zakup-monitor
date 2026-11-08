"""Настройки: интерфейс, уведомления, аккаунт, ИИ, тариф, реквизиты, пользователи,
вход «как пользователь», Telegram.
"""

import time
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from zakup import (
    auth,
    bot,
    client_bot,
    credits,
    kaspi,
    legal,
    research,
    secrets_box,
    signup,
    tariff,
    telegram,
)
from zakup.goszakup import PORTAL_TZ
from zakup.web import core
from zakup.web.core import (
    IMPERSONATE_TTL,
    RETURN_COOKIE,
    SESSION_COOKIE,
    Admin,
    Paid,
    Staff,
    User,
    Viewer,
    _audit,
    _user_groups,
    _with_session,
    logger,
    same_origin,
    templates,
)

router = APIRouter()


@router.get("/settings")
def settings(request: Request, user: Viewer, sent: str = "", saved: str = ""):
    if user is None:
        # Гостю — только вкладка «Интерфейс»: тема, язык, режим для слабовидящих.
        return templates.TemplateResponse(request, "settings.html", {
            "guest": True, "is_admin": False, "is_staff": False, "tab": "interface",
            "notice": None, "error": None, "saved": "", "sent": ""})
    return _settings_page(request, user, sent=sent, saved=saved)


def _settings_page(request: Request, user: int, *, sent: str = "", saved: str = "",
                   notice: str | None = None, error: str | None = None, status_code: int = 200,
                   tab: str | None = None):
    """Страница настроек: данные каждой группы вкладок собирают функции ниже."""
    is_admin = request.state.role == "admin"
    is_staff = request.state.role in auth.STAFF_ROLES
    with core.pool.connection() as conn:
        chat_id, company_bin = conn.execute(
            "SELECT telegram_chat_id, company_bin FROM users WHERE id = %s", (user,)
        ).fetchone()
        context = {
            **_telegram_tab(conn, request, user, chat_id, is_staff),
            **_ai_tab(conn, user, is_admin),
            **_users_tab(conn, user, is_admin, is_staff),
            "groups": _user_groups(conn, user),
        }
    # Имя бота может спрашиваться у Telegram по сети — уже без соединения с базой.
    context.update(_telegram_bot(is_staff))
    context.update({
        "is_admin": is_admin,
        "is_staff": is_staff,
        "guest": False,
        "sent": sent,
        "saved": saved,
        "notice": notice,
        "error": error,
        "company_bin": company_bin,
        "tab": tab or {"ai": "ai", "bin": "company", "tariff": "tariff",
                       "requisites": "requisites"}.get(saved),
        "requisite_fields": legal.FIELDS,
        "me": user,
        "now": datetime.now(UTC),
        "min_password": auth.MIN_PASSWORD_LENGTH,
    })
    return templates.TemplateResponse(request, "settings.html", context, status_code=status_code)


def _telegram_tab(conn, request: Request, user: int, chat_id: str | None, is_staff: bool) -> dict:
    """Уведомления: подключён ли Telegram, группа клиентов."""
    client_group = None if is_staff else {
        "exists": bool(client_bot.group_id(conn)),
        "member": bool(conn.execute("SELECT 1 FROM client_group_members WHERE user_id = %s",
                                    (user,)).fetchone()),
        "linked": bool(chat_id and str(chat_id).startswith(telegram.CLIENT_PREFIX)),
        "paid": request.state.paid}
    return {"connected": bool(chat_id), "client_group": client_group}


def _telegram_bot(is_staff: bool) -> dict:
    # Клиентам — свой бот (@tenderim_kz_bot), сотрудникам — прежний.
    tg_bot = "main" if is_staff else "client"
    username = telegram.bot_username(tg_bot)
    return {"bot_username": username, "configured": bool(telegram.token(tg_bot) and username)}


def _ai_tab(conn, user: int, is_admin: bool) -> dict:
    """ИИ: провайдер, модели, сохранённые ключи, прокси для Kaspi."""
    ai = conn.execute("SELECT ai_provider, ai_model, ai_model_light FROM users WHERE id = %s",
                      (user,)).fetchone()
    return {
        "ai_provider": ai[0],
        "ai_model": ai[1] or "",
        "ai_model_light": ai[2] or research.SUMMARY_MODELS.get(ai[0] or "claude_code", ""),
        "ai_keys": secrets_box.saved_providers(conn, user),
        "proxy_host": kaspi.proxy_host(kaspi.proxy_url(conn)) if is_admin else "",
        "providers": research.PROVIDERS,
        "default_models": research.DEFAULT_MODELS,
        "models": research.MODELS,
        "light_defaults": research.SUMMARY_MODELS,
    }


def _users_tab(conn, user: int, is_admin: bool, is_staff: bool) -> dict:
    """Пользователи: список (модератор не видит администраторов), удалённые, регистрация."""
    users = conn.execute(
        """SELECT u.id, u.email, u.role, u.telegram_chat_id IS NOT NULL,
                  (SELECT max(created_at) FROM sessions s WHERE s.user_id = u.id),
                  u.disabled, o.name, o.bin, u.paid_until,
                  (SELECT coalesce(sum(g.delta), 0) FROM search_ledger g WHERE g.user_id = u.id)
           FROM users u LEFT JOIN client_orgs o ON o.user_id = u.id
           WHERE u.deleted_at IS NULL AND (%s OR u.role <> 'admin' OR u.id = %s)
           ORDER BY array_position(ARRAY['admin', 'moderator', 'user'], u.role), u.email""",
        (is_admin, user),
    ).fetchall() if is_staff else []
    deleted = conn.execute(
        """SELECT u.id, u.email, u.role, u.deleted_at, d.email, u.may_reregister
           FROM users u LEFT JOIN users d ON d.id = u.deleted_by
           WHERE u.deleted_at IS NOT NULL ORDER BY u.deleted_at DESC"""
    ).fetchall() if is_admin else []
    return {"users": users, "deleted": deleted, "roles": auth.ROLES,
            "registration_open": signup.registration_open(conn)}


@router.post("/settings/password", dependencies=[Depends(same_origin)])
def change_password(
    request: Request,
    user: User,
    current: Annotated[str, Form()],
    password: Annotated[str, Form()],
    repeat: Annotated[str, Form()],
):
    with core.pool.connection() as conn:
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


@router.post("/settings/users", dependencies=[Depends(same_origin)])
def add_user(request: Request, staff: Staff, login: Annotated[str, Form()]):
    login = auth.normalize_email(login)
    if not login or len(login) > 100 or any(c.isspace() for c in login):
        return _settings_page(request, staff, error="Логин — без пробелов, до 100 символов.",
                              status_code=422, tab="users")
    with core.pool.connection() as conn:
        existing = conn.execute("SELECT deleted_at FROM users WHERE email = %s", (login,)).fetchone()
        if existing:
            hint = " Он удалён — вернуть его может администратор." if existing[0] else ""
            return _settings_page(request, staff, error=f"Пользователь {login} уже есть.{hint}",
                                  status_code=422, tab="users")
        password = auth.generate_password()
        auth.set_password(conn, login, password)
    _audit(request, staff, "user_add", login)
    return _settings_page(request, staff, notice=f"Создан пользователь {login}. Пароль: {password} "
                          "— передайте его, больше он нигде не показывается.", tab="users")


def _other_user(conn, request: Request, actor: int, user_id: int, deleted: bool = False) -> str:
    """Логин пользователя, которым управляет сотрудник. Себя трогать нельзя;
    модератор управляет только обычными пользователями."""
    if user_id == actor:
        raise HTTPException(422, "Свою роль, доступ и аккаунт изменить здесь нельзя")
    row = conn.execute("SELECT email, role, deleted_at FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None or (row[2] is not None) != deleted:
        raise HTTPException(404, "Нет такого пользователя")
    if request.state.role != "admin" and row[1] != "user":
        raise HTTPException(403, "Модератор управляет только обычными пользователями")
    return row[0]


@router.post("/settings/registration", dependencies=[Depends(same_origin)])
def toggle_registration(request: Request, admin: Admin):
    with core.pool.connection() as conn:
        now_open = not signup.registration_open(conn)
        signup.set_registration_open(conn, now_open)
    _audit(request, admin, "settings_registration", "открыта" if now_open else "закрыта")
    return _settings_page(request, admin, tab="users", notice=(
        "Регистрация открыта: любой может зарегистрироваться по телефону (SMS — 40 ₸)."
        if now_open else "Регистрация закрыта."))


@router.post("/settings/users/{user_id}/access", dependencies=[Depends(same_origin)])
def grant_access(request: Request, admin: Admin, user_id: int, days: Annotated[int, Form()] = 30):
    """Доступ к платному вручную (до подключения оплаты, для проверки, по договорённости).
    days=0 — снять доступ."""
    if not 0 <= days <= 400:
        raise HTTPException(422, "от 0 до 400 дней")
    with core.pool.connection() as conn:
        login = _other_user(conn, request, admin, user_id)
        until = conn.execute(
            """UPDATE users SET paid_until = CASE WHEN %s = 0 THEN NULL
                   ELSE greatest(coalesce(paid_until, now()), now()) + make_interval(days => %s) END
               WHERE id = %s RETURNING paid_until""",
            (days, days, user_id),
        ).fetchone()[0]
    _audit(request, admin, "user_access", login, days=days)
    text = (f"{login}: доступ к платному до {until.astimezone(PORTAL_TZ):%d.%m.%Y}." if until
            else f"{login}: доступ к платному снят.")
    return _settings_page(request, admin, tab="users", notice=text)


@router.post("/settings/users/{user_id}/searches", dependencies=[Depends(same_origin)])
def grant_searches(request: Request, admin: Admin, user_id: int, count: Annotated[int, Form()] = 20):
    """Начислить (или списать, если отрицательно) поиски вручную — до подключения оплаты."""
    if not -1000 <= count <= 1000 or count == 0:
        raise HTTPException(422, "от −1000 до 1000, не ноль")
    with core.pool.connection() as conn:
        login = _other_user(conn, request, admin, user_id)
        credits.add(conn, user_id, count, "grant", by=admin, note="начислил администратор")
        left = credits.balance(conn, user_id)
    _audit(request, admin, "user_searches", login, count=count)
    return _settings_page(request, admin, tab="users", notice=f"{login}: поисков на балансе — {left}.")


@router.post("/settings/users/{user_id}/impersonate", dependencies=[Depends(same_origin)])
def impersonate(request: Request, admin: Admin, user_id: int):
    """Войти как пользователь, не зная пароля: увидеть сайт его глазами.
    Своя сессия администратора остаётся и возвращается кнопкой на плашке."""
    with core.pool.connection() as conn:
        login = _other_user(conn, request, admin, user_id)
        role = conn.execute("SELECT role FROM users WHERE id = %s", (user_id,)).fetchone()[0]
        if role == "admin":
            raise HTTPException(403, "Входить как другой администратор нельзя")
        token = auth.create_session(conn, user_id, request.headers.get("user-agent"), IMPERSONATE_TTL)
        conn.commit()
    _audit(request, admin, "impersonate", login)
    response = _with_session(RedirectResponse("/", status_code=303), token)
    response.set_cookie(RETURN_COOKIE, request.cookies.get(SESSION_COOKIE, ""),
                        max_age=int(IMPERSONATE_TTL.total_seconds()), httponly=True, secure=True,
                        samesite="lax")
    return response


@router.post("/impersonate/stop", dependencies=[Depends(same_origin)])
def impersonate_stop(request: Request):
    """Вернуться в свой аккаунт: сессию «под пользователем» закрываем."""
    back = request.cookies.get(RETURN_COOKIE)
    with core.pool.connection() as conn:
        admin = auth.user_by_session(conn, back) if back else None
        who = auth.user_by_session(conn, request.cookies.get(SESSION_COOKIE))
        auth.logout(conn, request.cookies.get(SESSION_COOKIE))
    if admin is None or admin[2] != "admin":
        response = RedirectResponse("/login", status_code=303)
    else:
        if who:
            _audit(request, admin[0], "impersonate_stop", who[1])
        response = _with_session(RedirectResponse("/settings#users", status_code=303), back)
    response.delete_cookie(RETURN_COOKIE, httponly=True, secure=True, samesite="lax")
    return response


@router.post("/settings/users/{user_id}/role", dependencies=[Depends(same_origin)])
def set_role(request: Request, admin: Admin, user_id: int, role: Annotated[str, Form()]):
    if role not in auth.ROLES:
        raise HTTPException(422, "неизвестная роль")
    with core.pool.connection() as conn:
        login = _other_user(conn, request, admin, user_id)
        conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
    _audit(request, admin, "user_role", login, role=role)
    return _settings_page(request, admin, tab="users", notice=f"{login}: роль — {auth.ROLES[role]}.")


@router.post("/settings/users/{user_id}/disable", dependencies=[Depends(same_origin)])
def toggle_disabled(request: Request, staff: Staff, user_id: int):
    with core.pool.connection() as conn:
        login = _other_user(conn, request, staff, user_id)
        disabled = conn.execute(
            "UPDATE users SET disabled = NOT disabled WHERE id = %s RETURNING disabled", (user_id,)
        ).fetchone()[0]
        if disabled:
            # Отключение действует сразу: выкидываем из всех открытых сессий.
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    _audit(request, staff, "user_disable", login, disabled=disabled)
    return _settings_page(request, staff, tab="users", notice=f"{login}: аккаунт "
                          f"{'отключён — войти нельзя' if disabled else 'снова включён'}.")


@router.post("/settings/users/{user_id}/delete", dependencies=[Depends(same_origin)])
def delete_user(request: Request, staff: Staff, user_id: int):
    """Удаление мягкое: человек не войдёт и ничего не получит, но запись, подписки и
    избранное остаются — администратор может вернуть."""
    with core.pool.connection() as conn:
        login = _other_user(conn, request, staff, user_id)
        conn.execute("UPDATE users SET deleted_at = now(), deleted_by = %s WHERE id = %s",
                     (staff, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        conn.execute("UPDATE subscriptions SET active = false WHERE user_id = %s", (user_id,))
    _audit(request, staff, "user_delete", login)
    return _settings_page(request, staff, tab="users",
                          notice=f"Пользователь {login} удалён. Вернуть его может администратор.")


@router.post("/settings/users/{user_id}/restore", dependencies=[Depends(same_origin)])
def restore_user(request: Request, admin: Admin, user_id: int):
    with core.pool.connection() as conn:
        login = _other_user(conn, request, admin, user_id, deleted=True)
        conn.execute("UPDATE users SET deleted_at = NULL, deleted_by = NULL WHERE id = %s", (user_id,))
    _audit(request, admin, "user_restore", login)
    return _settings_page(request, admin, tab="users", notice=f"Пользователь {login} восстановлен. "
                          "Подписки выключены — он включит их сам.")


@router.post("/settings/users/{user_id}/reregister", dependencies=[Depends(same_origin)])
def toggle_reregister(request: Request, admin: Admin, user_id: int):
    with core.pool.connection() as conn:
        login = _other_user(conn, request, admin, user_id, deleted=True)
        allowed = conn.execute(
            "UPDATE users SET may_reregister = NOT may_reregister WHERE id = %s RETURNING may_reregister",
            (user_id,),
        ).fetchone()[0]
    _audit(request, admin, "user_reregister", login, allowed=allowed)
    return _settings_page(request, admin, tab="users", notice=f"{login}: повторная регистрация "
                          f"{'разрешена' if allowed else 'запрещена'}.")


@router.post("/settings/users/{user_id}/reset", dependencies=[Depends(same_origin)])
def reset_password(request: Request, staff: Staff, user_id: int):
    with core.pool.connection() as conn:
        if user_id == staff:
            login = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()[0]
        elif request.state.role != "admin":
            # Новый пароль клиента в руках модератора — это вход под клиентом, а он только у
            # администратора. Клиент восстанавливает пароль сам, по SMS.
            raise HTTPException(403, "Сбросить пароль клиента может только администратор")
        else:
            login = _other_user(conn, request, staff, user_id)
        password = auth.generate_password()
        auth.set_password(conn, login, password)
    _audit(request, staff, "user_reset", login)
    if user_id == staff:
        return RedirectResponse("/login", status_code=303)
    return _settings_page(request, staff, notice=f"Новый пароль для {login}: {password} — "
                          "передайте его, больше он нигде не показывается. Старые входы сброшены.",
                          tab="users")


@router.post("/settings/ai", dependencies=[Depends(same_origin)])
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
    with core.pool.connection() as conn:
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


@router.post("/settings/tariff", dependencies=[Depends(same_origin)])
def save_tariff(request: Request, admin: Admin, name_ru: Annotated[str, Form()],
                price: Annotated[str, Form()], features_ru: Annotated[str, Form()],
                name_kk: Annotated[str, Form()] = "", features_kk: Annotated[str, Form()] = ""):
    digits = "".join(c for c in price if c.isdigit())
    if not digits or not 0 < int(digits) <= 10_000_000 or not name_ru.strip() or not tariff.lines(features_ru):
        return _settings_page(request, admin, error="Укажите название, цену в тенге и хотя бы один пункт.",
                              status_code=422, tab="tariff")
    data = {"name_ru": name_ru.strip()[:60], "name_kk": (name_kk.strip() or name_ru.strip())[:60],
            "price": int(digits), "features_ru": tariff.lines(features_ru),
            "features_kk": tariff.lines(features_kk) or tariff.lines(features_ru)}
    with core.pool.connection() as conn:
        tariff.save(conn, data)
    _audit(request, admin, "settings_tariff", data["name_ru"], price=data["price"])
    return RedirectResponse("/settings?saved=tariff#tariff", status_code=303)


@router.post("/settings/requisites", dependencies=[Depends(same_origin)])
async def save_requisites(request: Request, admin: Admin):
    form = await request.form()
    with core.pool.connection() as conn:
        legal.save(conn, {key: form.get(key, "") for key in legal.FIELDS})
    _audit(request, admin, "settings_requisites", (form.get("name") or "")[:100])
    return RedirectResponse("/settings?saved=requisites#requisites", status_code=303)


@router.post("/settings/proxy", dependencies=[Depends(same_origin)])
def save_proxy(request: Request, user: Admin, proxy: Annotated[str, Form()] = "",
               action: Annotated[str, Form()] = "save"):
    """Прокси (IPRoyal) для Kaspi и других закрытых для серверов сайтов."""
    with core.pool.connection() as conn:
        if action == "delete":
            conn.execute("DELETE FROM user_ai_keys WHERE user_id = %s AND provider = %s",
                         (user, kaspi.PROXY_KEY))
            _audit(request, user, "settings_proxy", "удалён")
            return RedirectResponse("/settings?saved=proxy#ai", status_code=303)
        if action == "test":
            current = kaspi.proxy_url(conn)
            try:
                found = kaspi.search("кровать", current) if current else []
                message = (f"Прокси работает: Kaspi ответил, товаров {len(found)}." if found
                           else "Прокси не задан." if not current else "Kaspi ответил, но товаров нет.")
            except kaspi.Blocked as error:
                message = f"Не работает: {error}."
            return _settings_page(request, user, notice=message, tab="ai")
        if not kaspi.valid_proxy(proxy):
            return _settings_page(request, user, error="Адрес прокси вида http://логин:пароль@хост:порт.",
                                  status_code=422, tab="ai")
        try:
            secrets_box.save_key(conn, user, kaspi.PROXY_KEY, proxy.strip())
        except secrets_box.NoSecretKey:
            raise HTTPException(503, "На сервере не настроено шифрование ключей") from None
    _audit(request, user, "settings_proxy", kaspi.proxy_host(proxy))
    return RedirectResponse("/settings?saved=proxy#ai", status_code=303)


_models_cache: dict[tuple[int, str], tuple[float, list]] = {}


@router.get("/settings/ai/models")
def ai_models(user: Admin, provider: str):
    if provider not in research.MODELS:
        raise HTTPException(404, "нет такого провайдера")
    cached = _models_cache.get((user, provider))
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with core.pool.connection() as conn:
        key = secrets_box.user_key(conn, user, provider)
    if not key:
        return []
    try:
        models = research.provider_models(provider, key)
    except (httpx.HTTPError, KeyError, ValueError):
        return []
    _models_cache[(user, provider)] = (time.monotonic() + 3600, models)
    return models


@router.post("/settings/company", dependencies=[Depends(same_origin)])
def save_company(request: Request, user: Admin, company_bin: Annotated[str, Form()] = ""):
    company_bin = company_bin.strip()
    if company_bin and not (company_bin.isdigit() and len(company_bin) == 12):
        raise HTTPException(422, "БИН или ИИН — 12 цифр")
    with core.pool.connection() as conn:
        conn.execute(
            "UPDATE users SET company_bin = %s WHERE id = %s", (company_bin or None, user)
        )
    _audit(request, user, "settings_company", company_bin or "—")
    return RedirectResponse("/settings?saved=bin#company", status_code=303)


@router.post("/settings/telegram/link", dependencies=[Depends(same_origin)])
def telegram_link(request: Request, user: User):
    which = "main" if request.state.staff else "client"
    username = telegram.bot_username(which)
    if not username:
        raise HTTPException(503, "Telegram-бот не настроен")
    with core.pool.connection() as conn:
        code = bot.create_link_code(conn, user, "private" if which == "main" else "client")
    _audit(request, user, "telegram_link")
    return RedirectResponse(f"https://t.me/{username}?start={code}", status_code=303)


@router.post("/settings/client-group", dependencies=[Depends(same_origin)])
def client_group_join(request: Request, user: Paid):
    """Ссылка-заявка в группу клиентов: бот одобрит её, если тариф оплачен."""
    with core.pool.connection() as conn:
        try:
            link = client_bot.invite_link(conn, request.state.email)
        except telegram.TelegramError as error:
            logger.warning("invite link failed: %s", error)
            link = None
    if not link:
        raise HTTPException(503, "Группа клиентов пока не подключена")
    _audit(request, user, "client_group_join")
    return RedirectResponse(link, status_code=303)


@router.post("/settings/telegram/group", dependencies=[Depends(same_origin)])
def telegram_group_link(request: Request, user: Admin):
    if not telegram.bot_username():
        raise HTTPException(503, "Telegram-бот не настроен")
    with core.pool.connection() as conn:
        code = bot.create_link_code(conn, user, "group")
    _audit(request, user, "group_link")
    return RedirectResponse(bot.group_link_url(code), status_code=303)


@router.post("/settings/groups/remove", dependencies=[Depends(same_origin)])
def telegram_group_remove(request: Request, user: Admin, chat_id: Annotated[str, Form()]):
    # Группы общие: отключение убирает её у всех, их подписки уходят в личку.
    with core.pool.connection() as conn:
        title = conn.execute(
            "SELECT title FROM telegram_groups WHERE chat_id = %s", (chat_id,)
        ).fetchone()
        bot.unlink_group(conn, chat_id)
    _audit(request, user, "group_remove", title[0] if title else chat_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/telegram/unlink", dependencies=[Depends(same_origin)])
def telegram_unlink(request: Request, user: User):
    with core.pool.connection() as conn:
        conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE id = %s", (user,))
    _audit(request, user, "telegram_unlink")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/telegram/test", dependencies=[Depends(same_origin)])
def telegram_test(user: User):
    with core.pool.connection() as conn:
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
