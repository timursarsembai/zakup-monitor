"""kazpayment.kz: SMS-коды подтверждения телефона (а позже — оплаты).

Ключ — KAZPAYMENT_API_KEY (Infisical → .env). Каждый код — настоящая SMS за счёт
баланса бизнеса (40 ₸), поэтому отправку ограничиваем у себя (zakup.signup).
Документация: https://kazpayment.kz/llms.txt
"""

import os
import secrets
from typing import Any

import httpx

API = "https://kazpayment.kz/api/v1"


class KazpaymentError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _key() -> str:
    key = os.environ.get("KAZPAYMENT_API_KEY", "").strip()
    if not key:
        raise KazpaymentError("not_configured", "KAZPAYMENT_API_KEY не задан")
    return key


def _post(path: str, payload: dict[str, Any], transport: httpx.BaseTransport | None = None) -> dict:
    try:
        with httpx.Client(timeout=httpx.Timeout(20, connect=5), transport=transport) as http:
            response = http.post(f"{API}{path}", json=payload,
                                 headers={"Authorization": f"Bearer {_key()}"})
    except httpx.HTTPError as error:
        raise KazpaymentError("network", type(error).__name__) from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        raise KazpaymentError(str(data.get("code") or f"http_{response.status_code}"),
                              str(data.get("message") or data.get("error") or ""))
    return data


def request_otp(phone: str, transport: httpx.BaseTransport | None = None) -> str:
    """Отправляет SMS с кодом. Возвращает requestId для проверки."""
    data = _post("/otp/request", {"phone": phone, "idempotency_key": "zakup-" + secrets.token_hex(12)},
                 transport)
    request_id = data.get("requestId")
    if not request_id:
        raise KazpaymentError("bad_response", "нет requestId")
    return str(request_id)


def verify_otp(request_id: str, code: str, transport: httpx.BaseTransport | None = None) -> str | None:
    """Проверяет код. Возвращает подтверждённый номер (только из ответа kazpayment) или None."""
    data = _post("/otp/verify", {"requestId": request_id, "code": code}, transport)
    phone = data.get("phone")
    return str(phone) if data.get("validated") and phone else None


def _get(path: str, transport: httpx.BaseTransport | None = None) -> dict:
    try:
        with httpx.Client(timeout=httpx.Timeout(20, connect=5), transport=transport) as http:
            response = http.get(f"{API}{path}", headers={"Authorization": f"Bearer {_key()}"})
    except httpx.HTTPError as error:
        raise KazpaymentError("network", type(error).__name__) from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        raise KazpaymentError(str(data.get("code") or f"http_{response.status_code}"))
    return data


def create_order(ref: str, amount: int, comment: str, phone: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> str:
    """Заводит заказ (повтор с тем же ref безопасен). Возвращает ссылку на оплату."""
    payload: dict[str, Any] = {"external_ref": ref, "amount": amount, "comment": comment[:200]}
    if phone:
        payload["customer_phone"] = phone
    data = _post("/orders", payload, transport)
    if not data.get("payUrl"):
        raise KazpaymentError("bad_response", "нет payUrl")
    return str(data["payUrl"])


def order_status(ref: str, transport: httpx.BaseTransport | None = None) -> dict:
    from urllib.parse import quote

    return _get(f"/orders/{quote(ref, safe='')}", transport)
