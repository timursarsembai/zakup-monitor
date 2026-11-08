"""Настройки ИИ администратора: какой провайдер и модели работают для всех пользователей,
его API-ключи и прокси. Раньше один и тот же запрос был в восьми местах."""

import psycopg

from zakup import secrets_box

DEFAULT_PROVIDER = "claude_code"


def admin_id(conn: psycopg.Connection) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def models(conn: psycopg.Connection) -> tuple[str, str | None, str | None]:
    """(провайдер, модель поиска, модель коротких задач); модели — None, если не выбраны."""
    row = conn.execute(
        "SELECT ai_provider, ai_model, ai_model_light FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return DEFAULT_PROVIDER, None, None
    return row[0] or DEFAULT_PROVIDER, row[1], row[2]


def provider(conn: psycopg.Connection) -> str:
    return models(conn)[0]


def admin_key(conn: psycopg.Connection, name: str) -> str | None:
    """Зашифрованный ключ администратора (API-ключ провайдера, прокси); None — не задан."""
    admin = admin_id(conn)
    return secrets_box.user_key(conn, admin, name) if admin else None
