"""API-ключи пользователей (OpenAI, Anthropic) хранятся в базе зашифрованными.

Ключ шифрования — ZAKUP_SECRET_KEY (Fernet, в .env/Infisical). Без него
сохранить ключ нельзя, а сохранённые не прочитать.
"""


import psycopg
from cryptography.fernet import Fernet, InvalidToken

from zakup import config


class NoSecretKey(Exception):
    pass


def _box() -> Fernet:
    key = config.secret_key()
    if not key:
        raise NoSecretKey("ZAKUP_SECRET_KEY не задан")
    return Fernet(key.encode())


def save_key(conn: psycopg.Connection, user_id: int, provider: str, api_key: str) -> None:
    conn.execute(
        """INSERT INTO user_ai_keys (user_id, provider, key_enc) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, provider) DO UPDATE SET key_enc = excluded.key_enc,
               updated_at = now()""",
        (user_id, provider, _box().encrypt(api_key.strip().encode())),
    )


def user_key(conn: psycopg.Connection, user_id: int, provider: str) -> str | None:
    row = conn.execute(
        "SELECT key_enc FROM user_ai_keys WHERE user_id = %s AND provider = %s",
        (user_id, provider),
    ).fetchone()
    if row is None:
        return None
    try:
        return _box().decrypt(bytes(row[0])).decode()
    except (InvalidToken, NoSecretKey):
        return None


def saved_providers(conn: psycopg.Connection, user_id: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT provider FROM user_ai_keys WHERE user_id = %s", (user_id,))}
