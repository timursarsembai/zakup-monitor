"""Клиент REST API портала госзакупок (ows.goszakup.gov.kz/v3).

Списки отдаются по возрастанию id, страница за страницей: в ответе есть `items`
и `next_page` вида `/v3/lots?page=next&search_after=123`. Курсор search_after —
это id последней записи, поэтому дальше по API можно идти от любого id.
"""

import logging
import re
import time
from datetime import datetime
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://ows.goszakup.gov.kz"
ENTITIES = {"announcements": "/v3/trd-buy", "lots": "/v3/lots"}
# Даты в API приходят без часового пояса, по времени Астаны.
PORTAL_TZ = ZoneInfo("Asia/Almaty")
PAGE_LIMIT = 200


class GoszakupError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Page(NamedTuple):
    items: list[dict[str, Any]]
    next_after: int | None
    total: int | None


class Client:
    def __init__(self, token: str, delay: float = 0.5, base_url: str = BASE_URL):
        self._delay = delay
        self._last_request = 0.0
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )

    def close(self) -> None:
        self._http.close()

    def page(self, entity: str, after: int | None = None, limit: int = PAGE_LIMIT) -> Page:
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["search_after"] = after
        data = self._get(ENTITIES[entity], params)
        items = [normalize(item) for item in data.get("items") or []]
        return Page(items, next_after(data.get("next_page")), data.get("total"))

    def get(self, entity: str, item_id: int) -> dict[str, Any] | None:
        try:
            return normalize(self._get(f"{ENTITIES[entity]}/{item_id}", {}))
        except GoszakupError as error:
            if error.status == 404:
                return None
            raise

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(5):
            wait = self._delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as error:
                logger.warning("goszakup %s: %s, retry %d", path, type(error).__name__, attempt)
                time.sleep(2**attempt * 5)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                logger.warning("goszakup %s: HTTP %d, retry %d", path, response.status_code, attempt)
                time.sleep(2**attempt * 5)
                continue
            if response.status_code != 200:
                raise GoszakupError(
                    f"{path}: HTTP {response.status_code} {response.text[:200]}",
                    response.status_code,
                )
            return response.json()
        raise GoszakupError(f"{path}: no successful response after retries")


def next_after(next_page: str | None) -> int | None:
    if not next_page:
        return None
    values = parse_qs(urlsplit(next_page).query).get("search_after")
    return int(values[0]) if values else None


_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def normalize(item: dict[str, Any]) -> dict[str, Any]:
    """Ключи в snake_case: REST и GraphQL API называют поля по-разному."""
    return {_CAMEL.sub("_", key).lower(): value for key, value in item.items()}


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=PORTAL_TZ)
