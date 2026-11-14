"""Настройки сервиса учётных записей — только из окружения (секреты из Infisical)."""

import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_url() -> str:
    url = _get("ID_DATABASE_URL")
    if not url:
        raise RuntimeError("ID_DATABASE_URL is not set")
    return url


def signing_key() -> str:
    """Закрытый ключ Ed25519 (base64): им подписываются жетоны входа."""
    key = _get("ID_SIGNING_KEY")
    if not key:
        raise RuntimeError("ID_SIGNING_KEY is not set")
    return key


def device_key() -> str:
    """Свой ключ меток «этот браузер уже входил» — не ключ подписи: их меняют независимо."""
    key = _get("ID_DEVICE_KEY")
    if not key:
        raise RuntimeError("ID_DEVICE_KEY is not set")
    return key


def service_key() -> str:
    """Общий ключ запросов основного сайта сюда (заголовок Authorization: Bearer)."""
    return _get("ID_SERVICE_KEY")


def site_url() -> str:
    """Адрес основного сайта: туда возвращаем после входа и только оттуда принимаем формы."""
    return _get("ID_SITE_URL", "https://tenderim.com").rstrip("/")


def cookie_domain() -> str | None:
    """Домен cookie входа (".tenderim.com" — видна и основному сайту); пусто — только этот адрес."""
    return _get("ID_COOKIE_DOMAIN") or None

