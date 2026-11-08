"""Регистрация и восстановление пароля по SMS-коду (kazpayment).

Шаг 1: телефон → SMS с кодом, в адрес следующего шага идёт случайный flow.
Шаг 2: код (+ пароль) → номер подтверждён, flow одноразовый.

Каждая SMS стоит денег, поэтому пределы свои, поверх пределов kazpayment
(минута между кодами на номер, 300 в сутки на бизнес).
"""

import ipaddress
import secrets
from datetime import timedelta

import psycopg

from zakup import kazpayment, notify

FLOW_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5          # попыток ввести один код
PER_IP_HOUR = 5           # кодов в час с одного адреса
PER_NET_HOUR = 10         # кодов в час с одной подсети (/24, у IPv6 — /64)
PER_PHONE_DAY = 5         # кодов в сутки на один номер
# Кодов в сутки всего (≈40 ₸ за SMS) — у регистрации и восстановления свои: выбрав
# лимит регистраций, нельзя закрыть восстановление пароля настоящим клиентам.
PER_DAY = {"register": 100, "reset": 50}


class SignupError(Exception):
    pass


def registration_open(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'registration_open'").fetchone()
    return bool(row and row[0] == "1")


def set_registration_open(conn: psycopg.Connection, value: bool) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES ('registration_open', %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        ("1" if value else "0",),
    )


OTP_LOCK = 7_402


def _network(ip: str | None) -> str | None:
    """Подсеть адреса: у одного провайдера или хостинга адреса идут подряд."""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def start(conn: psycopg.Connection, phone: str, purpose: str, ip: str | None) -> str:
    """Отправляет код и возвращает flow для следующего шага."""
    # Счётчики и запись шага — под общей блокировкой и до отправки SMS: иначе пачка
    # одновременных запросов видит одни и те же старые счётчики и обходит лимиты.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (OTP_LOCK,))
    ip_count, net_count, phone_count, total = conn.execute(
        """SELECT count(*) FILTER (WHERE ip = %(ip)s AND created_at > now() - interval '1 hour'),
                  count(*) FILTER (WHERE %(net)s::cidr IS NOT NULL AND ip IS NOT NULL
                                   AND ip::inet <<= %(net)s::cidr AND created_at > now() - interval '1 hour'),
                  count(*) FILTER (WHERE phone = %(phone)s),
                  count(*) FILTER (WHERE purpose = %(purpose)s)
           FROM otp_requests WHERE created_at > now() - interval '1 day'""",
        {"ip": ip, "net": _network(ip), "phone": phone, "purpose": purpose},
    ).fetchone()
    if ip_count >= PER_IP_HOUR or net_count >= PER_NET_HOUR or phone_count >= PER_PHONE_DAY:
        conn.rollback()
        raise SignupError("Слишком много кодов. Попробуйте позже.")
    budget = PER_DAY[purpose]
    if total >= budget:
        conn.rollback()
        raise SignupError("Отправка кодов временно недоступна. Попробуйте завтра.")
    if total + 1 == budget // 2:
        notify.send(f"zakup: за сутки отправлено {total + 1} SMS-кодов ({purpose}) — половина "
                    f"дневного лимита {budget}. Если клиентов столько не было — похоже на перебор.")
    flow = secrets.token_urlsafe(24)
    conn.execute(
        """INSERT INTO otp_requests (flow, purpose, phone, request_id, ip)
           VALUES (%s, %s, %s, '', %s)""",
        (flow, purpose, phone, ip),
    )
    conn.commit()
    try:
        request_id = kazpayment.request_otp(phone)
    except kazpayment.KazpaymentError as error:
        # SMS не ушло — шаг не считается.
        conn.execute("DELETE FROM otp_requests WHERE flow = %s", (flow,))
        conn.commit()
        if error.code == "rate_limited":
            raise SignupError("Код уже отправлен — подождите минуту и попробуйте снова.") from None
        if error.code == "invalid_phone":
            raise SignupError("Номер не похож на казахстанский мобильный.") from None
        raise SignupError("Не удалось отправить SMS. Попробуйте позже.") from None
    conn.execute("UPDATE otp_requests SET request_id = %s WHERE flow = %s", (request_id, flow))
    conn.commit()
    return flow


def pending(conn: psycopg.Connection, flow: str, purpose: str) -> str | None:
    """Телефон незавершённого шага или None (нет, истёк, использован, исчерпан)."""
    row = conn.execute(
        f"""SELECT phone FROM otp_requests WHERE flow = %s AND purpose = %s AND used_at IS NULL
            AND attempts < {MAX_ATTEMPTS} AND created_at > now() - %s""",
        (flow, purpose, FLOW_TTL),
    ).fetchone()
    return row[0] if row else None


def verify(conn: psycopg.Connection, flow: str, purpose: str, code: str) -> str:
    """Проверяет код и закрывает flow. Возвращает подтверждённый номер."""
    row = conn.execute(
        f"""UPDATE otp_requests SET attempts = attempts + 1
            WHERE flow = %s AND purpose = %s AND used_at IS NULL
              AND attempts < {MAX_ATTEMPTS} AND created_at > now() - %s
            RETURNING request_id, phone, attempts""",
        (flow, purpose, FLOW_TTL),
    ).fetchone()
    conn.commit()
    if row is None:
        raise SignupError("Код устарел. Запросите новый.")
    request_id, phone, attempts = row
    code = "".join(c for c in code if c.isdigit())[:8]
    try:
        confirmed = kazpayment.verify_otp(request_id, code) if code else None
    except kazpayment.KazpaymentError:
        raise SignupError("Не удалось проверить код. Попробуйте ещё раз.") from None
    if not confirmed:
        left = MAX_ATTEMPTS - attempts
        raise SignupError(f"Неверный код. Осталось попыток: {left}." if left else
                          "Неверный код. Попытки закончились — запросите новый.")
    conn.execute("UPDATE otp_requests SET verified_at = now(), used_at = now() WHERE flow = %s", (flow,))
    conn.commit()
    # Номер — из ответа kazpayment, а не из формы: только его они подтвердили.
    from zakup.auth import normalize_phone

    return normalize_phone(confirmed) or phone
