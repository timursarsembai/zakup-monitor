"""Пароли, сессии и ограничение подбора. Только стандартная библиотека."""

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

import psycopg

from zakup import config

SESSION_TTL = timedelta(days=30)
# Не больше стольких неудачных попыток за окно — на e-mail и на IP.
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20
FAILURE_WINDOW = timedelta(minutes=15)

# scrypt: n=2**14 — около 50 мс и 16 МБ памяти на проверку.
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 10


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Тратим столько же времени, сколько на настоящую проверку: иначе по
        # времени ответа видно, есть ли такой e-mail.
        hash_password(password)
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            maxmem=_MAXMEM, dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def set_password(conn: psycopg.Connection, email: str, password: str) -> int:
    row = conn.execute(
        """INSERT INTO users (email, password_hash) VALUES (%s, %s)
           ON CONFLICT (email) DO UPDATE SET password_hash = excluded.password_hash
           RETURNING id""",
        (normalize_email(email), hash_password(password)),
    ).fetchone()
    # Смена пароля выкидывает все старые сессии.
    conn.execute("DELETE FROM sessions WHERE user_id = %s", (row[0],))
    conn.commit()
    return row[0]


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(raw: str) -> str | None:
    """Казахстанский мобильный в виде +7XXXXXXXXXX: «8 701 111-22-33», «+7 (701)…».
    Клиенты входят по телефону: он же их логин в users.email."""
    digits = "".join(c for c in raw or "" if c.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10 or digits[0] != "7" or digits[1] not in "04567":
        return None
    return "+7" + digits


def is_phone_login(login: str) -> bool:
    return login.startswith("+7") and len(login) == 12 and login[1:].isdigit()


def device_mark(user_id: int) -> str | None:
    """Метка «с этого браузера уже входили»: id и подпись. Без ключа — меток нет."""
    key = config.secret_key()
    if not key:
        return None
    sign = hmac.new(key.encode(), f"device:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{sign}"


def known_device(mark: str | None, user_id: int | None) -> bool:
    expected = device_mark(user_id) if user_id else None
    return bool(mark and expected and hmac.compare_digest(mark, expected))


def too_many_failures(conn: psycopg.Connection, email: str, ip: str | None,
                      known: bool = False) -> bool:
    """Слишком много неудачных попыток. Входы по логину из браузера, где этот пользователь
    уже входил (known), не блокируются: иначе чужой мог бы держать аккаунт закрытым,
    просто вводя неверный пароль."""
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE email = %(email)s),
                  count(*) FILTER (WHERE ip = %(ip)s)
           FROM login_attempts
           WHERE NOT ok AND created_at > now() - %(window)s
             AND (email = %(email)s OR ip = %(ip)s)""",
        {"email": email, "ip": ip, "window": FAILURE_WINDOW},
    ).fetchone()
    return (not known and row[0] >= MAX_FAILURES_PER_EMAIL) or (ip is not None and row[1] >= MAX_FAILURES_PER_IP)


def log_attempt(conn: psycopg.Connection, email: str, ip: str | None, ok: bool) -> None:
    conn.execute(
        "INSERT INTO login_attempts (email, ip, ok) VALUES (%s, %s, %s)", (email, ip, ok)
    )
    conn.execute("DELETE FROM login_attempts WHERE created_at < now() - interval '30 days'")


def login(
    conn: psycopg.Connection, email: str, password: str, ip: str | None, user_agent: str | None
) -> str | None:
    """Проверяет пароль и создаёт сессию. Возвращает токен для cookie или None."""
    email = normalize_email(email)
    row = conn.execute(
        "SELECT id, password_hash, disabled, deleted_at FROM users WHERE email = %s", (email,)
    ).fetchone()
    # Отключённый и удалённый аккаунт не входит даже с верным паролем.
    ok = verify_password(password, row[1] if row else None) and not row[2] and row[3] is None
    log_attempt(conn, email, ip, ok)
    token = create_session(conn, row[0], user_agent) if ok else None
    conn.commit()
    return token


def create_session(conn: psycopg.Connection, user_id: int, user_agent: str | None,
                   ttl=None, impersonated_by: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO sessions (token_hash, user_id, expires_at, user_agent, impersonated_by)
           VALUES (%s, %s, now() + %s, %s, %s)""",
        (_token_hash(token), user_id, ttl or SESSION_TTL, (user_agent or "")[:300], impersonated_by),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    return token


# До какого момента у клиента платный доступ: оплата или пробный месяц, что позже
# (greatest пропускает NULL). Условие на таблицу users под псевдонимом u.
ACCESS_UNTIL = "greatest(u.paid_until, u.trial_until)"
# Пробный месяц после регистрации: всё платное, кроме поиска товаров с ИИ (он — по балансу поисков).
TRIAL_DAYS = 30


def user_by_session(conn: psycopg.Connection, token: str | None) -> tuple | None:
    """(id, логин, роль, есть ли доступ к платному, остаток поисков, кто из администраторов
    вошёл под этим пользователем — или None) по cookie сессии.

    Платное доступно сотрудникам всегда, клиентам — пока оплачено или идёт проба (ACCESS_UNTIL)."""
    if not token:
        return None
    return conn.execute(
        f"""SELECT u.id, u.email, u.role,
                  u.role IN ('admin', 'moderator') OR coalesce({ACCESS_UNTIL} > now(), false),
                  (SELECT coalesce(sum(g.delta), 0) FROM search_ledger g WHERE g.user_id = u.id),
                  s.impersonated_by
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.disabled
             AND u.deleted_at IS NULL""",
        (_token_hash(token),),
    ).fetchone()


# Срок входа скользящий: заходит человек — отсчёт SESSION_TTL начинается заново (не чаще раза
# в RENEW_AFTER), и активному не приходится входить снова. Вход администратора под пользователем
# (impersonated_by) не продлевается — у него свой короткий срок.
RENEW_AFTER = timedelta(days=1)


def renew_session(conn: psycopg.Connection, token: str | None) -> bool:
    """Продлить сессию на SESSION_TTL от сейчас; True — продлили (тогда и cookie обновить)."""
    if not token:
        return False
    return conn.execute(
        """UPDATE sessions SET expires_at = now() + %s
           WHERE token_hash = %s AND impersonated_by IS NULL
             AND expires_at > now() AND expires_at < now() + %s""",
        (SESSION_TTL, _token_hash(token), SESSION_TTL - RENEW_AFTER)).rowcount == 1


def logout(conn: psycopg.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))
        conn.commit()


# Сотрудники: полный бесплатный доступ, управление обычными пользователями, журнал.
STAFF_ROLES = ("admin", "moderator")
# Клиент — не сотрудник и не удалён. Условие на таблицу users под псевдонимом u.
CLIENT_SQL = "u.role = 'user' AND u.deleted_at IS NULL"
# Кому положены уведомления (подписки, напоминания): сотрудникам и клиентам с тарифом.
# Условие на таблицу users под псевдонимом u.
ENTITLED_SQL = (f"(u.role IN ('admin', 'moderator') OR {ACCESS_UNTIL} > now()) "
                "AND NOT u.disabled AND u.deleted_at IS NULL")
ROLES = {"admin": "администратор", "moderator": "модератор", "user": "пользователь"}


def generate_password() -> str:
    """Пароль для выдачи пользователю: 12 букв и цифр без похожих (0/O, 1/l)."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def check_password(conn: psycopg.Connection, user_id: int, password: str) -> bool:
    row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,)).fetchone()
    return bool(row and row[0] and verify_password(password, row[0]))
