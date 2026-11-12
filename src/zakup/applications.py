"""Реестр заявок поставщиков OWS (/v3/trd-app) — ждём, когда заработает.

По нашему токену реестр всегда пуст (total: 0). Разработчики ЦЭФ 12.11.2026: «вопрос с trd-app
в работе, пока не разрешён» — это неисправность у них, а не закрытый доступ. Раз в сутки
проверяем одним запросом; как только записи появятся — одно сообщение владельцу. Тогда
можно делать число поданных заявок по открытым лотам (ROADMAP, раздел 5б).
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import psycopg

from zakup import notify
from zakup.goszakup import Client

logger = logging.getLogger(__name__)

PATH = "/v3/trd-app"
KEY = "trd_app_check"
EVERY = timedelta(days=1)
LOCK = 7_403  # два процесса с подробностями завершённых не проверяют (и не пишут владельцу) дважды


def _state(conn: psycopg.Connection) -> dict:
    row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (KEY,)).fetchone()
    try:
        state = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _save(conn: psycopg.Connection, state: dict) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (KEY, json.dumps(state)),
    )


def check(conn: psycopg.Connection, client: Client, now: datetime | None = None) -> str:
    """Проверить реестр, если с прошлой попытки прошли сутки. Любая ошибка — не сбой
    сборщика: попытка записана, следующая — через сутки."""
    now = now or datetime.now(UTC)
    try:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK,)).fetchone()[0]:
            return "skip"
        state = _state(conn)
        checked = state.get("checked_at")
        if checked and now - datetime.fromisoformat(checked) < EVERY:
            return "skip"
        # Попытку записываем до запроса: неисправный реестр не дёргаем на каждом круге
        # (клиент повторяет запрос до ~6 минут, прежде чем сдаться).
        state["checked_at"] = now.isoformat()
        _save(conn, state)
        conn.commit()
        try:
            total = client.total(PATH)
        except Exception as error:
            logger.warning("trd-app check failed: %s: %s", type(error).__name__, error)
            state["error"] = f"{type(error).__name__}: {str(error)[:200]}"
            _save(conn, state)
            return "error"
        state.update(total=total, error=None)
        if total and not state.get("notified"):
            state["notified"] = notify.send(
                f"Реестр заявок OWS (/v3/trd-app) заработал: записей {total}. Можно делать число "
                "поданных заявок по открытым лотам — см. ROADMAP, раздел 5б.")
        _save(conn, state)
        return f"total {total}"
    finally:
        conn.commit()  # и снять блокировку, и не оставлять открытую транзакцию
