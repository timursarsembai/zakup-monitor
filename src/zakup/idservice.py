"""Основной сайт ↔ сервис учётных записей в Казахстане (src/zakup_id, docs/ACCOUNTS_KZ.md).

Включается, когда заданы ZAKUP_ID_URL и ZAKUP_ID_PUBLIC_KEY. Тогда вошедшего пользователя
сайт узнаёт по подписанному жетону (cookie tid) и проверяет подпись сам; раз в сутки жетон
обменивается на свежий (запрос в Казахстан), а события сервиса («сменён телефон», служебные
владельцу) бот забирает и отправляет в Telegram — Telegram у сайта (решение владельца).
"""

import html
import logging
import threading
import time
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


def _api(method: str, path: str, timeout: httpx.Timeout = TIMEOUT, **kwargs) -> httpx.Response:
    try:
        response = httpx.request(method, f"{config.id_service_url()}{path}", timeout=timeout,
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


# --- Действия сотрудников (вкладка «Пользователи») ---
# Сначала — в Казахстане, потом у себя: не дошло туда — здесь ничего не меняем (ServiceError).


class Replaced(ServiceError):
    """Номер удалённого уже занят новой регистрацией: прежнюю запись не вернуть."""


def _detail(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data.get("detail") if isinstance(data, dict) else None


def _staff_call(path: str, body: dict, account: bool = True) -> None:
    """Записи в Казахстане нет (заведён здесь до переезда) — менять там нечего: только когда
    сервис так и ответил («no such account»), а не любой 404 (неверный адрес, прокси, старая
    сборка без этого пути — это ошибка, здесь тогда ничего не меняем)."""
    response = _api("POST", path, json=body)
    if response.status_code == 200:
        return
    detail = _detail(response)
    if account and response.status_code == 404 and detail == "no such account":
        logger.info("%s: account %s is not in the service, site only", path, body.get("user_id"))
        return
    if path == "/api/status" and response.status_code == 409:
        raise Replaced("record replaced")
    raise ServiceError(f"http {response.status_code}")


def block(user_id: int, blocked: bool) -> None:
    """Отключить (выйти везде и не пускать) или снова включить вход."""
    _staff_call("/api/revoke", {"user_id": user_id, "block": blocked})


def set_status(user_id: int, deleted: bool | None = None, may_reregister: bool | None = None) -> None:
    """Удалить (все входы закрываются), вернуть, разрешить зарегистрироваться заново."""
    _staff_call("/api/status", {"user_id": user_id, "deleted": deleted, "may_reregister": may_reregister})


def registration_open() -> bool:
    """Регистрация открыта в Казахстане (там она и идёт)."""
    response = _api("GET", "/api/registration")
    if response.status_code != 200:
        raise ServiceError(f"http {response.status_code}")
    try:
        return bool(response.json()["open"])
    except (ValueError, KeyError, TypeError):
        raise ServiceError("bad answer") from None


def set_registration(open_: bool) -> None:
    _staff_call("/api/registration", {"open": open_}, account=False)


# --- Организация клиента и IP журнала действий — хранятся в Казахстане ---
# Сайт спрашивает, когда нужно, и держит ответ недолго в памяти процесса (не в базе).
# Ответ держим недолго: у сайта несколько процессов (две копии и сборщик), сброс после
# изменения — только в своём. Где ответ окончательный (итоги в Telegram) — fresh, без памяти.
ORG_CACHE_SECONDS = 60
ORG_CHUNK = 1000
_org_cache: dict[int, tuple[float, dict | None]] = {}


def _post(path: str, body: dict, timeout: httpx.Timeout = TIMEOUT) -> dict:
    response = _api("POST", path, json=body, timeout=timeout)
    if response.status_code != 200:
        raise ServiceError(f"http {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise ServiceError("bad answer") from None


def orgs(ids: list[int], fresh: bool = False) -> dict[int, dict]:
    """Организации пользователей {номер: {kind, bin, name, results_bin}}; нет — нет ключа.
    ServiceError — сервис не ответил (вызывающий решает: показать без организации или ждать)."""
    now = time.monotonic()
    known = {} if fresh else {i: c[1] for i in ids if (c := _org_cache.get(i)) and c[0] > now}
    wanted = [i for i in dict.fromkeys(ids) if i not in known]
    for start in range(0, len(wanted), ORG_CHUNK):
        chunk = wanted[start:start + ORG_CHUNK]
        found = {int(k): v for k, v in _post("/api/orgs/get", {"ids": chunk},
                                             timeout=TIMEOUT if len(chunk) < 50 else BULK_TIMEOUT
                                             ).get("orgs", {}).items()}
        if len(_org_cache) > 10_000:
            _org_cache.clear()
        for i in chunk:
            known[i] = found.get(i)
            _org_cache[i] = (now + ORG_CACHE_SECONDS, found.get(i))
    return {i: org for i, org in known.items() if org}


def set_orgs(items: list[dict]) -> None:
    """Пачкой: {user_id, org?: {kind, bin, name} | None, results_bin?} (см. /api/orgs/set)."""
    for start in range(0, len(items), ORG_CHUNK):
        _post("/api/orgs/set", {"items": items[start:start + ORG_CHUNK]},
              timeout=TIMEOUT if len(items) < 50 else BULK_TIMEOUT)
    for item in items:
        _org_cache.pop(item["user_id"], None)


_ips: list[dict] = []
_ips_lock = threading.Lock()
_ips_wake = threading.Event()
_ips_thread: threading.Thread | None = None
IPS_MAX_QUEUE = 5_000
_ips_dropped = 0


def queue_ip(action_id: int, user_id: int | None, ip: str) -> None:
    """IP записи журнала — в Казахстан, в фоне (журнал пишется на каждый просмотр: ждать
    ответа нельзя). Не дошло (сервис лежит дольше очереди) — адрес теряется, журнал остаётся."""
    global _ips_thread, _ips_dropped
    with _ips_lock:
        if len(_ips) < IPS_MAX_QUEUE:
            _ips.append({"action_id": action_id, "user_id": user_id, "ip": ip[:64]})
        else:
            _ips_dropped += 1
            if _ips_dropped in (1, 100) or _ips_dropped % 1000 == 0:
                logger.warning("action ips queue full: %d dropped", _ips_dropped)
        if _ips_thread is None or not _ips_thread.is_alive():
            _ips_thread = threading.Thread(target=_send_ips, name="id-ips", daemon=True)
            _ips_thread.start()
    _ips_wake.set()


def _send_ips() -> None:
    while True:
        _ips_wake.wait(30)
        _ips_wake.clear()
        time.sleep(2)  # копим пачку
        with _ips_lock:
            batch = _ips[:500]
        if not batch:
            continue
        try:
            _post("/api/ips", {"items": batch})
        except ServiceError as error:
            if str(error).startswith("http 4"):
                # Сервис отказал (не сбой связи) — повтор не поможет: пачку не держим, очередь не встаёт.
                logger.warning("action ips rejected (%d): %s", len(batch), error)
            else:
                logger.warning("action ips not sent (%d): %s", len(batch), error)
                time.sleep(30)
                continue
        with _ips_lock:
            del _ips[:len(batch)]
        if _ips:
            _ips_wake.set()


def ips(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {int(k): v for k, v in _post("/api/ips/get", {"ids": ids}).get("ips", {}).items()}


# --- Переезд учётных записей в Казахстан (zakup id-migrate, zakup id-scrub) ---


def _local_accounts(conn: psycopg.Connection) -> list[dict]:
    """Пользователи, чьи логин и пароль ещё здесь (не заглушка «id:N»)."""
    rows = conn.execute(
        """SELECT id, email, password_hash, phone, disabled, deleted_at, may_reregister, created_at
           FROM users WHERE email NOT LIKE 'id:%%' ORDER BY id""").fetchall()
    keys = ("id", "login", "password_hash", "phone", "disabled", "deleted_at", "may_reregister", "created_at")
    return [dict(zip(keys, row, strict=True)) for row in rows]


# Перенос — разовый и большой (до тысяч записей одним запросом): ждём дольше обычного.
BULK_TIMEOUT = httpx.Timeout(300, connect=5)


def _bulk(path: str, rows: list[dict]) -> dict:
    response = _api("POST", path, json={"accounts": rows}, timeout=BULK_TIMEOUT)
    if response.status_code != 200:
        raise ServiceError(f"http {response.status_code} {_detail(response) or ''}".strip())
    return response.json()


def _local_ips(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, user_id, ip, at FROM user_actions WHERE ip IS NOT NULL ORDER BY id").fetchall()
    return [{"action_id": r[0], "user_id": r[1], "ip": r[2], "at": r[3].isoformat()} for r in rows]


def migrate(conn: psycopg.Connection) -> dict[str, list]:
    """Логины, хэши паролей и телефоны — в сервис (с прежними номерами), туда же организации
    клиентов и IP журнала действий. Повторять можно: перенесённое сервис пропускает или
    перезаписывает тем же. Здесь ничего не меняется (это делает scrub)."""
    rows = [{**row, **{k: row[k].isoformat() for k in ("deleted_at", "created_at") if row[k]}}
            for row in _local_accounts(conn)]
    from zakup import client_orgs

    # Здесь остаются только те, кого после включения сервиса не меняли (изменение сразу
    # уходит туда и стирает здешнюю копию) — повтор переноса свежее не затрёт.
    local_orgs, local_ips = client_orgs.local(conn), _local_ips(conn)
    conn.commit()  # данные прочитаны — соединение не держим, пока ждём Казахстан
    result = _bulk("/api/import", rows)
    set_orgs([{"user_id": i, **client_orgs.as_item(org)} for i, org in local_orgs.items()])
    for start in range(0, len(local_ips), 1000):
        _post("/api/ips", {"items": local_ips[start:start + 1000]}, timeout=BULK_TIMEOUT)
    return {**result, "orgs": list(local_orgs), "ips": [len(local_ips)]}


def check(conn: psycopg.Connection) -> dict[str, list[int]]:
    """Кого можно стирать здесь: same — в Казахстане та же запись; missing — нет;
    different — под этим номером там чужая (стирать нельзя)."""
    rows = [{"id": a["id"], "login": a["login"], "password_hash": a["password_hash"]}
            for a in _local_accounts(conn)]
    conn.commit()
    return _bulk("/api/verify", rows) if rows else {"same": [], "missing": [], "different": []}


_PERSONAL = r"^\+?[78]\d{10}$|@"  # телефон или e-mail в журнале действий


def scrub(conn: psycopg.Connection, dry_run: bool = False) -> dict:
    """Личные данные — прочь отсюда, когда запись уже есть в Казахстане и та же (check):
    логин становится «id:N», пароль и телефон стираются; в журнале действий у этих людей
    логин — «id:N», упоминания их логинов в target — тоже. Когда переехали все — из журнала
    стираются оставшиеся телефоны и e-mail (коды на номера без аккаунта, старые логины),
    удаляются неудачные входы и SMS-коды. Остальных не трогаем."""
    if not enabled():
        raise ServiceError("service is not configured")
    found = check(conn)
    stats: dict = {"scrubbed": 0, "missing": found["missing"], "different": found["different"]}
    # Организации и IP: стираем здесь только то, что в Казахстане уже есть то же самое.
    from zakup import client_orgs

    local_orgs, local_ips = client_orgs.local(conn), _local_ips(conn)
    conn.commit()
    remote_orgs = orgs(list(local_orgs), fresh=True) if local_orgs else {}
    orgs_ready = [i for i, org in local_orgs.items() if client_orgs.same(remote_orgs.get(i), org)]
    remote_ips: dict[int, str] = {}
    for start in range(0, len(local_ips), 5000):
        chunk = [i["action_id"] for i in local_ips[start:start + 5000]]
        remote_ips.update({int(k): v for k, v in _post("/api/ips/get", {"ids": chunk},
                                                       timeout=BULK_TIMEOUT).get("ips", {}).items()})
    ips_ready = [i["action_id"] for i in local_ips if remote_ips.get(i["action_id"]) == i["ip"]]
    stats.update(orgs_missing=[i for i in local_orgs if i not in orgs_ready],
                 ips_missing=len(local_ips) - len(ips_ready))
    if dry_run:
        return {**stats, "would_scrub": len(found["same"]), "orgs": len(orgs_ready), "ips": len(ips_ready)}
    if orgs_ready:
        conn.execute("DELETE FROM client_orgs WHERE user_id = ANY(%s)", (orgs_ready,))
        conn.execute("UPDATE users SET company_bin = NULL WHERE id = ANY(%s)", (orgs_ready,))
    if ips_ready:
        conn.execute("UPDATE user_actions SET ip = NULL WHERE id = ANY(%s)", (ips_ready,))
    # БИН в журнале действий («Указал организацию», «БИН для итогов») — у ИП это ИИН.
    conn.execute("UPDATE user_actions SET target = NULL WHERE action IN ('client_org', 'settings_company') "
                 "AND target ~ '^[0-9]{12}$'")
    stats.update(orgs=len(orgs_ready), ips=len(ips_ready))
    if not found["same"]:
        conn.commit()
        return stats
    ids = found["same"]
    conn.execute(
        """UPDATE user_actions a SET target = 'id:' || u.id FROM users u
           WHERE u.id = ANY(%s) AND a.target = u.email""", (ids,))
    conn.execute("UPDATE user_actions SET login = 'id:' || user_id WHERE user_id = ANY(%s)", (ids,))
    stats["scrubbed"] = conn.execute(
        "UPDATE users SET email = 'id:' || id, password_hash = NULL, phone = NULL WHERE id = ANY(%s)",
        (ids,)).rowcount
    if not found["missing"] and not found["different"]:
        conn.execute("UPDATE user_actions SET login = NULL WHERE login ~ %s", (_PERSONAL,))
        conn.execute("UPDATE user_actions SET target = NULL WHERE target ~ %s", (_PERSONAL,))
        stats["login_attempts"] = conn.execute("DELETE FROM login_attempts").rowcount
        stats["otp_requests"] = conn.execute("DELETE FROM otp_requests").rowcount
    conn.commit()
    return stats


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
