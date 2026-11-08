"""Сбои сборщиков: что сообщать владельцу и что отдавать на автоисправление.

Недоступность портала — не ошибка в коде: сборщики сами повторят через пару минут.
О ней пишем один раз, если перерыв затянулся, и ещё раз, когда портал вернулся.
Остальные ошибки записываются в таблицу incidents; их подхватывает `zakup autofix`
на хосте (см. autofix.py). Одна и та же ошибка не плодит записи и сообщения:
пока она не разобрана, растёт только счётчик.
"""

import json
import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

from zakup import db, notify
from zakup.goszakup import GoszakupError, PortalUnavailable

logger = logging.getLogger(__name__)

# Через сколько непрерывной недоступности портала писать владельцу.
OUTAGE_NOTICE = timedelta(minutes=30)
# Статусы, при которых повтор той же ошибки не заводит новую запись.
OPEN = ("new", "working")
# Сколько молчать о повторах уже разобранной ошибки (кроме исправленной).
QUIET = timedelta(days=1)

_SECRETS = [
    (re.compile(r"postgres(?:ql)?://\S+"), "postgresql://…"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),
    (re.compile(r"(?i)(token|key|secret|password|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s\"',]+"),
     r"\1\2…"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


# Портал временно отказывает (ограничение частоты, защита от ботов) — это не ошибка в коде.
TEMPORARY_REFUSALS = (403, 429)


def is_outage(error: BaseException) -> bool:
    if isinstance(error, GoszakupError) and getattr(error, "status", None) in TEMPORARY_REFUSALS:
        return True
    return isinstance(error, (PortalUnavailable, httpx.TransportError))


def fingerprint(error: BaseException) -> str:
    """Тип ошибки и последнее место в нашем коде: одна причина — один отпечаток."""
    frames = [f for f in traceback.extract_tb(error.__traceback__) if "/zakup/" in f.filename]
    where = f"{frames[-1].filename.rsplit('/zakup/', 1)[-1]}:{frames[-1].name}" if frames else "?"
    return f"{type(error).__name__}@{where}"


def _connect(conn):
    if conn is not None:
        return conn, False
    return db.connect(os.environ["DATABASE_URL"]), True


def failed(stage: str, error: BaseException, conn: psycopg.Connection | None = None) -> None:
    """Вызывается из except сборщика. stage — по-русски, что упало («синхронизация»)."""
    try:
        conn, own = _connect(conn)
    except Exception:
        logger.exception("incident not recorded")
        notify.send(f"zakup: {stage} — ошибка {type(error).__name__} (база недоступна).")
        return
    try:
        if is_outage(error):
            _outage(conn)
        else:
            _record(conn, stage, error)
        conn.commit()
    except Exception:
        logger.exception("incident not recorded")
        conn.rollback()
    finally:
        if own:
            conn.close()


def _record(conn, stage: str, error: BaseException) -> None:
    mark = fingerprint(error)
    # Та же ошибка, пока её разбирают, и сутки после разбора (кроме «исправлено» —
    # тогда исправление не помогло и нужен новый разбор) — только счётчик, без сообщений.
    row = conn.execute(
        """UPDATE incidents SET count = count + 1, last_at = now()
           WHERE id = (SELECT id FROM incidents WHERE fingerprint = %s
                         AND (status = ANY(%s)
                              OR (status <> 'fixed' AND finished_at > now() - %s))
                       ORDER BY id DESC LIMIT 1)
           RETURNING id""",
        (mark, list(OPEN), QUIET),
    ).fetchone()
    if row:
        return
    trace = redact("".join(traceback.format_exception(error))[-6000:])
    incident = conn.execute(
        "INSERT INTO incidents (fingerprint, stage, error_type, trace) VALUES (%s, %s, %s, %s) "
        "RETURNING id",
        (mark, stage, type(error).__name__, trace),
    ).fetchone()[0]
    notify.send(f"zakup: {stage} — ошибка {type(error).__name__} (сбой №{incident}). "
                "Разбираю автоматически, пришлю отчёт.")


def _get_outage(conn) -> dict | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'portal_outage'").fetchone()
    return json.loads(row[0]) if row else None


def _outage(conn) -> None:
    now = datetime.now(timezone.utc)
    state = _get_outage(conn)
    if state is None:
        state = {"since": now.isoformat(), "notified": False}
    elif not state["notified"] and now - datetime.fromisoformat(state["since"]) >= OUTAGE_NOTICE:
        since = datetime.fromisoformat(state["since"]).astimezone(timezone(timedelta(hours=5)))
        notify.send(f"zakup: портал goszakup не отвечает или отказывает в доступе с {since:%H:%M}. "
                    "Это на их стороне; "
                    "сборщики повторяют сами, напишу, когда портал вернётся.")
        state["notified"] = True
    else:
        return
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('portal_outage', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (json.dumps(state),),
    )


def portal_ok(conn: psycopg.Connection) -> None:
    """Проход сборщика удался: если портал был недоступен — перерыв закончился."""
    state = _get_outage(conn)
    if state is None:
        return
    conn.execute("DELETE FROM app_settings WHERE key = 'portal_outage'")
    conn.commit()
    if state["notified"]:
        minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(state["since"])).total_seconds() // 60)
        notify.send(f"zakup: портал goszakup снова отвечает (перерыв около {minutes} мин), "
                    "сборщики догружают пропущенное.")
