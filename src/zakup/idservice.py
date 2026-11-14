"""Основной сайт ↔ сервис учётных записей в Казахстане (src/zakup_id, docs/ACCOUNTS_KZ.md).

Включается, когда заданы ZAKUP_ID_URL и ZAKUP_ID_PUBLIC_KEY. Тогда вошедшего пользователя
сайт узнаёт по подписанному жетону (cookie tid) и проверяет подпись сам; раз в сутки жетон
обменивается на свежий (запрос в Казахстан), а события сервиса («сменён телефон», служебные
владельцу) бот забирает и отправляет в Telegram — Telegram у сайта (решение владельца).
"""

import html
import logging
from datetime import UTC, datetime

import httpx
import psycopg

from zakup import auth, config, notify, telegram
from zakup_id import token as idtoken

logger = logging.getLogger(__name__)
COOKIE = idtoken.COOKIE
# Сервис в другой стране: ждать его дольше нельзя — страница не должна висеть.
TIMEOUT = httpx.Timeout(3, connect=2)


class ServiceError(Exception):
    pass


def enabled() -> bool:
    # Без ключа сервиса продление ответило бы 401 всем — и всех бы выкинуло: не включаемся.
    return bool(config.id_service_url() and config.id_public_key() and config.id_service_key())


def page(path: str, lang: str | None = None) -> str:
    """Адрес страницы сервиса (регистрация, восстановление, смена пароля и телефона)."""
    tail = f"?lang={lang}" if lang and lang != "ru" else ""
    return f"{config.id_service_url()}{path}{tail}"


def _api(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        response = httpx.request(method, f"{config.id_service_url()}{path}", timeout=TIMEOUT,
                                 headers={"authorization": f"Bearer {config.id_service_key()}"}, **kwargs)
    except httpx.HTTPError as error:
        raise ServiceError(type(error).__name__) from None
    if response.status_code >= 500:
        raise ServiceError(f"http {response.status_code}")
    return response


def resolve(value: str | None, now: datetime | None = None) -> tuple[int, str | None] | None:
    """(номер пользователя, новый жетон или None) по cookie tid; None — не вошёл.

    Свежий жетон — без запросов. Старше суток — обмен в Казахстане: 401 значит сессия
    закрыта (выход, смена пароля, блокировка). Сервис недоступен — старый жетон годится
    ещё до 3 суток: вход не пропадает из-за сбоя связи."""
    claims = idtoken.read(value, config.id_public_key())
    if claims is None:
        return None
    now = now or datetime.now(UTC)
    if claims.fresh(now):
        return claims.user_id, None
    try:
        response = _api("POST", "/api/renew", json={"token": value})
        if response.status_code == 401 and response.json().get("detail") == "session ended":
            return None   # выход, смена пароля, блокировка — забываем вход
        if response.status_code != 200:
            raise ServiceError(f"http {response.status_code}")
        fresh = response.json().get("token")
    except (ServiceError, ValueError) as error:
        # Сбой связи, прокси, неверный ключ — не повод выкидывать: старый жетон ещё годится.
        logger.warning("token renewal failed: %s", error)
        return (claims.user_id, None) if claims.usable(now) else None
    renewed = idtoken.read(fresh, config.id_public_key())
    if renewed is None or renewed.user_id != claims.user_id:
        logger.warning("token renewal returned a bad token")
        return (claims.user_id, None) if claims.usable(now) else None
    return renewed.user_id, fresh


def previous_record(user_id: int) -> int | None:
    """Прежняя запись того же номера телефона (сервис знает) — запрос в Казахстан: вызывать
    без соединения из пула."""
    try:
        response = _api("GET", f"/api/accounts/{user_id}")
        if response.status_code == 200:
            return response.json().get("previous_id")
    except (ServiceError, ValueError) as error:
        logger.warning("account %s lookup failed: %s", user_id, error)
    return None


def ensure_user(conn: psycopg.Connection, user_id: int, lang: str, previous: int | None) -> None:
    """Новый пользователь (зарегистрировался в Казахстане) — строка здесь с тем же номером:
    роль, пробный срок, язык. Логина здесь нет — только заглушка «id:N» (личные данные —
    в Казахстане). Пробный срок — один на номер телефона: прежняя запись того же номера
    (previous_record) передаёт свой. Номера выдаёт только сервис: свои записи сайт при
    включённом сервисе не создаёт (иначе номера бы столкнулись)."""
    conn.execute(
        """INSERT INTO users (id, email, role, ui_lang, trial_until)
           VALUES (%s, %s, 'user', %s, coalesce((SELECT trial_until FROM users WHERE id = %s),
                                                now() + make_interval(days => %s)))
           ON CONFLICT (id) DO NOTHING""",
        (user_id, f"id:{user_id}", lang, previous, auth.TRIAL_DAYS))
    conn.commit()


def is_placeholder(login: str | None) -> bool:
    return bool(login) and login.startswith("id:")


def label(login: str, user_id: int) -> str:
    """Как назвать пользователя на сайте: логин здесь — или «№ N», если он в Казахстане."""
    return f"№ {user_id}" if is_placeholder(login) else login


def logout(value: str | None) -> None:
    """Закрыть сессию жетона в Казахстане (кнопка «Выйти» на сайте)."""
    if not value or idtoken.read(value, config.id_public_key()) is None:
        return
    try:
        _api("POST", "/api/logout", json={"token": value})
    except ServiceError as error:
        logger.warning("logout not sent: %s", error)


# --- События сервиса → Telegram (вызывает бот) ---

_TEXTS = {
    "ru": "Tenderim: телефон для входа изменён: {before}{new}. Если это были не вы — срочно "
          "напишите в поддержку.",
    "kk": "Tenderim: кіруге арналған телефон өзгертілді: {before}{new}. Бұл сіз болмасаңыз — "
          "қолдау қызметіне дереу жазыңыз.",
}


def _phone_changed(conn: psycopg.Connection, event: dict) -> None:
    row = conn.execute("SELECT telegram_chat_id, coalesce(notify_lang, ui_lang, 'ru') FROM users WHERE id = %s",
                       (event["user_id"],)).fetchone()
    if not row or not row[0]:
        return
    before = f"{event['before']} → " if event.get("before") else ""
    langs = ["ru", "kk"] if row[1] == "both" else [row[1] if row[1] in _TEXTS else "ru"]
    text = "\n\n".join(_TEXTS[lang].format(before=before, new=event["detail"]) for lang in langs)
    try:
        telegram.send_message(row[0], html.escape(text))
    except telegram.TelegramError as error:
        logger.warning("phone change notice to %s not sent: %s", event["user_id"], error)


def poll_events(conn: psycopg.Connection) -> int:
    """Забрать события сервиса, отправить, подтвердить. Неподтверждённые придут снова."""
    if not enabled():
        return 0
    try:
        response = _api("GET", "/api/events")
        if response.status_code != 200:
            raise ServiceError(f"http {response.status_code}")
        events = response.json().get("events") or []
    except (ServiceError, ValueError) as error:
        logger.warning("events not fetched: %s", error)
        return 0
    done = []
    for event in events:
        # Одно испорченное событие не держит остальные: иначе уже отправленные приходили бы
        # снова на каждом круге. Испорченное подтверждаем тоже — повтор его не исправит.
        try:
            if event.get("kind") == "phone_changed" and event.get("user_id"):
                _phone_changed(conn, event)
            elif event.get("kind") == "owner_alert":
                notify.send(f"id.tenderim.com: {event.get('detail')}")
        except (psycopg.Error, KeyError, TypeError) as error:
            conn.rollback()
            logger.warning("account event %s failed: %s", event.get("id"), error)
        if isinstance(event.get("id"), int):
            done.append(event["id"])
    if done:
        try:
            _api("POST", "/api/events/ack", json={"ids": done})
        except ServiceError as error:
            logger.warning("events not acknowledged (will repeat): %s", error)
    return len(done)
