"""Все переменные окружения проекта — здесь. Значения читаются при каждом вызове, а не
при импорте: тесты подменяют их на лету (monkeypatch.setenv), сервисы читают .env при запуске.

Секреты лежат в Infisical и попадают в .env через scripts/fetch-secrets.sh.
"""

import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_url() -> str:
    return os.environ["DATABASE_URL"]


def site_url() -> str:
    return _get("ZAKUP_SITE_URL", "https://zakup.sarsembai.com").rstrip("/")


# --- Портал госзакупок ---
def goszakup_token() -> str:
    return _get("GOSZAKUP_TOKEN")


def goszakup_delay() -> float:
    return float(_get("GOSZAKUP_REQUEST_DELAY") or 0.5)


# --- Telegram ---
TELEGRAM_TOKENS = {"main": "ZAKUP_TELEGRAM_BOT_TOKEN", "client": "ZAKUP_CLIENT_BOT_TOKEN"}


def telegram_token(bot: str = "main") -> str | None:
    return _get(TELEGRAM_TOKENS[bot]) or None


def telegram_bot_username() -> str | None:
    return _get("ZAKUP_TELEGRAM_BOT_USERNAME") or None


def owner_chat_id() -> str | None:
    """Личный чат владельца: служебные оповещения и право подключить группу клиентов."""
    return _get("ZAKUP_TELEGRAM_CHAT_ID") or None


# --- Оплата (kazpayment) ---
def kazpayment_key() -> str:
    return _get("KAZPAYMENT_API_KEY")


def kazpayment_webhook_secret() -> str:
    return _get("KAZPAYMENT_WEBHOOK_SECRET")


# --- Шифрование ключей в базе ---
def secret_key() -> str:
    return _get("ZAKUP_SECRET_KEY")


# --- Модели для дежурного и ночного ревьюера (Claude Code на хосте) ---
def autofix_model() -> str:
    return _get("ZAKUP_AUTOFIX_MODEL", "opus")


def nightly_model() -> str:
    return _get("ZAKUP_NIGHTLY_MODEL", "opus")
