"""Жетон входа: кто вошёл (номер пользователя), какая сессия и когда выдан, с подписью Ed25519.

Подписывает только сервис учётных записей (закрытый ключ — в Казахстане); основной сайт
проверяет открытым ключом сам, без запроса. Отозвать жетон сразу нельзя, поэтому он
«свежий» лишь FRESH: старше — сайт обменивает его здесь на новый (POST /api/renew), и
закрытая сессия (выход, смена пароля, блокировка) дальше не продлевается. При обмене
поколение растёт: прежний жетон той же сессии больше не обменять. Пока сервис
недоступен, сайт ещё MAX_AGE принимает старый жетон — вход не пропадает из-за сбоя связи.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

COOKIE = "tid"
FRESH = timedelta(days=1)
MAX_AGE = timedelta(days=3)


@dataclass(frozen=True)
class Claims:
    user_id: int
    sid: str
    issued_at: datetime
    gen: int = 0  # поколение: при продлении растёт, прежние жетоны сессии не продлеваются

    def fresh(self, now: datetime) -> bool:
        return now - self.issued_at < FRESH

    def usable(self, now: datetime) -> bool:
        return now - self.issued_at < MAX_AGE


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_keys() -> tuple[str, str]:
    """(закрытый, открытый) ключ в base64 — для первой настройки."""
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    return _b64(raw), public_key(_b64(raw))


def public_key(private_b64: str) -> str:
    private = Ed25519PrivateKey.from_private_bytes(_unb64(private_b64))
    return _b64(private.public_key().public_bytes(serialization.Encoding.Raw,
                                                  serialization.PublicFormat.Raw))


def issue(private_b64: str, user_id: int, sid: str, gen: int = 0, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    body = json.dumps({"u": user_id, "s": sid, "g": gen, "i": int(now.timestamp())},
                      separators=(",", ":")).encode()
    signature = Ed25519PrivateKey.from_private_bytes(_unb64(private_b64)).sign(body)
    return f"{_b64(body)}.{_b64(signature)}"


def read(token: str | None, public_b64: str) -> Claims | None:
    """Claims, если подпись верна (срок проверяет вызывающий: fresh/usable); иначе None."""
    if not token or token.count(".") != 1 or len(token) > 512:
        return None
    body_text, signature_text = token.split(".")
    try:
        body, signature = _unb64(body_text), _unb64(signature_text)
        Ed25519PublicKey.from_public_bytes(_unb64(public_b64)).verify(signature, body)
        data = json.loads(body)
        return Claims(int(data["u"]), str(data["s"]), datetime.fromtimestamp(int(data["i"]), UTC),
                      int(data["g"]))
    except (InvalidSignature, ValueError, binascii.Error, KeyError, TypeError):
        return None
