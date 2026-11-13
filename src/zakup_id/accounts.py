"""Вход, сессии и ограничение подбора пароля. Пароли — тем же scrypt, что на основном сайте
(zakup.auth): перенесённые хэши подходят без смены пароля. Правила подбора повторяют
zakup.auth; там они уйдут вместе с личными данными из основной базы (docs/ACCOUNTS_KZ.md)."""

import hashlib
import hmac
import secrets
import threading
from datetime import timedelta

import psycopg

from zakup.auth import normalize_email, normalize_phone, verify_password

SESSION_TTL = timedelta(days=30)
MAX_FAILURES_PER_LOGIN = 5
MAX_FAILURES_PER_IP = 20
FAILURE_WINDOW = timedelta(minutes=15)
# Два параллельных продления одного жетона (на сайте две копии) — второе получает уже
# новый жетон, а не выход: прежнее поколение принимается ещё столько.
RENEW_GRACE = timedelta(minutes=2)
# Проверка пароля — 16 МБ памяти; больше стольких сразу в одном процессе не пускаем.
_scrypt = threading.BoundedSemaphore(2)


def normalize_login(raw: str) -> str:
    """Клиенты входят по телефону; логин без цифр (сотрудники, старые аккаунты) — e-mail."""
    return normalize_phone(raw) or normalize_email(raw)


def _sid_hash(sid: str) -> bytes:
    return hashlib.sha256(sid.encode()).digest()


def device_mark(secret: str, account_id: int, gen: int) -> str:
    """Метка «с этого браузера уже входили»: чужие неверные пароли такой браузер не блокируют.
    «Выйти везде» меняет поколение — старые метки перестают действовать."""
    sign = hmac.new(secret.encode(), f"device:{account_id}:{gen}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{account_id}.{gen}.{sign}"


def _known_device(secret: str, mark: str | None, account_id: int, gen: int) -> bool:
    return bool(mark) and hmac.compare_digest(mark, device_mark(secret, account_id, gen))


def login(conn: psycopg.Connection, raw_login: str, password: str, ip: str | None,
          user_agent: str | None, device: str | None, secret: str) -> tuple[int, str, int] | str:
    """(номер пользователя, sid новой сессии, поколение метки браузера) или причина отказа:
    "lock" / "password".

    Пароль проверяется вне транзакции (scrypt ~50 мс): соединение и блокировка логина
    не держатся. Чтобы параллельные попытки не обходили предел, попытка записывается
    неудачной до проверки и исправляется на удачную после."""
    login_norm = normalize_login(raw_login)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("login:" + login_norm,))
    row = conn.execute("SELECT id, password_hash, disabled, device_gen FROM accounts WHERE login = %s",
                       (login_norm,)).fetchone()
    known = bool(row) and _known_device(secret, device, row[0], row[3])
    failures = conn.execute(
        """SELECT count(*) FILTER (WHERE login = %(login)s), count(*) FILTER (WHERE ip = %(ip)s)
           FROM login_attempts WHERE NOT ok AND created_at > now() - %(window)s
             AND (login = %(login)s OR ip = %(ip)s)""",
        {"login": login_norm, "ip": ip, "window": FAILURE_WINDOW}).fetchone()
    if (not known and failures[0] >= MAX_FAILURES_PER_LOGIN) or (ip and failures[1] >= MAX_FAILURES_PER_IP):
        conn.commit()
        return "lock"
    attempt = conn.execute("INSERT INTO login_attempts (login, ip, ok) VALUES (%s, %s, false) RETURNING id",
                           (login_norm, ip)).fetchone()[0]
    conn.commit()
    # verify_password и без аккаунта тратит время на хэш: по времени ответа не видно, есть ли логин.
    with _scrypt:
        ok = verify_password(password, row[1] if row else None) and not row[2]
    if not ok:
        return "password"
    conn.execute("UPDATE login_attempts SET ok = true WHERE id = %s", (attempt,))
    sid = create_session(conn, row[0], ip, user_agent)
    conn.commit()
    return row[0], sid, row[3]


def create_session(conn: psycopg.Connection, account_id: int, ip: str | None,
                   user_agent: str | None) -> str:
    sid = secrets.token_urlsafe(24)
    conn.execute(
        """INSERT INTO sessions (sid_hash, account_id, expires_at, ip, user_agent)
           VALUES (%s, %s, now() + %s, %s, %s)""",
        (_sid_hash(sid), account_id, SESSION_TTL, ip, (user_agent or "")[:300]))
    return sid


def renew(conn: psycopg.Connection, account_id: int, sid: str, gen: int) -> int | None:
    """Новое поколение жетона, если сессия жива, аккаунт не заблокирован и жетон — текущего
    поколения (или прежнего, но продлённого только что). Иначе None: утёкший старый жетон
    после продления законным владельцем уже не продлить."""
    row = conn.execute(
        """UPDATE sessions s SET expires_at = now() + %(ttl)s,
                  gen = CASE WHEN s.gen = %(gen)s THEN s.gen + 1 ELSE s.gen END,
                  renewed_at = CASE WHEN s.gen = %(gen)s THEN now() ELSE s.renewed_at END
           FROM accounts a
           WHERE s.sid_hash = %(sid)s AND s.account_id = %(account)s AND a.id = s.account_id
             AND s.expires_at > now() AND NOT a.disabled
             AND (s.gen = %(gen)s OR (s.gen = %(gen)s + 1 AND s.renewed_at > now() - %(grace)s))
           RETURNING s.gen""",
        {"ttl": SESSION_TTL, "sid": _sid_hash(sid), "account": account_id, "gen": gen,
         "grace": RENEW_GRACE}).fetchone()
    conn.commit()
    return row[0] if row else None


def end_session(conn: psycopg.Connection, sid: str) -> None:
    conn.execute("DELETE FROM sessions WHERE sid_hash = %s", (_sid_hash(sid),))
    conn.commit()


def end_all_sessions(conn: psycopg.Connection, account_id: int, block: bool | None = None) -> int:
    """Выйти везде; block=True — ещё и заблокировать вход, False — снять блокировку."""
    count = conn.execute("DELETE FROM sessions WHERE account_id = %s", (account_id,)).rowcount
    conn.execute("UPDATE accounts SET device_gen = device_gen + 1, disabled = coalesce(%s, disabled) "
                 "WHERE id = %s", (block, account_id))
    conn.commit()
    return count


def cleanup(conn: psycopg.Connection) -> None:
    """Старые попытки и истёкшие сессии (по индексам; вызывается не чаще раза в час)."""
    conn.execute("DELETE FROM login_attempts WHERE created_at < now() - interval '30 days'")
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    conn.commit()


def sync_ids(conn: psycopg.Connection) -> None:
    """После переноса с прежними номерами: счётчик новых — дальше самого большого номера."""
    conn.execute("SELECT setval(pg_get_serial_sequence('accounts', 'id'), "
                 "greatest((SELECT max(id) FROM accounts), 1))")
    conn.commit()
