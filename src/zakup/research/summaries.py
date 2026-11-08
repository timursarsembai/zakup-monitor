"""Выжимки техспецификаций: очередь и выполнение лёгкой моделью."""


import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup.research import providers as prov
from zakup.research.context import (
    lot_context,
)
from zakup.research.prompts import (
    SUMMARY_PROMPT,
    SUMMARY_SCHEMA,
)
from zakup.research.providers import (
    ResearchError,
    api_key,
    light_model,
)
from zakup.research.results import (
    _strings,
)

logger = logging.getLogger(__name__)


def request_summary(conn: psycopg.Connection, lot_id: int) -> str:
    """Ставит выжимку в очередь (если её ещё нет или прошлая не удалась). Статус."""
    provider, model = light_model(conn)
    row = conn.execute(
        """INSERT INTO spec_summaries (lot_id, provider, model) VALUES (%s, %s, %s)
           ON CONFLICT (lot_id) DO UPDATE SET status = 'queued', error = NULL,
               provider = excluded.provider, model = excluded.model, created_at = now()
           WHERE spec_summaries.status = 'error'
           RETURNING status""",
        (lot_id, provider, model),
    ).fetchone()
    conn.commit()
    if row:
        return row[0]
    return conn.execute("SELECT status FROM spec_summaries WHERE lot_id = %s", (lot_id,)).fetchone()[0]


def summary_of(conn: psycopg.Connection, lot_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT status, result, error FROM spec_summaries WHERE lot_id = %s", (lot_id,)
    ).fetchone()
    return dict(zip(("status", "result", "error"), row, strict=True)) if row else None


def _summarize(conn: psycopg.Connection, lot_id: int, provider: str, model: str) -> dict[str, Any]:
    context = lot_context(conn, lot_id)
    if context is None or context["spec"].startswith("нет —"):
        raise ResearchError("нет текста техспецификации")
    prompt = SUMMARY_PROMPT.format(**{k: v or "—" for k, v in context.items() if k != "kato"})
    key = api_key(conn, provider)
    conn.commit()
    if provider == "claude_code":
        raw = prov._claude_code(prompt, model, SUMMARY_SCHEMA, web=False)
    elif provider == "anthropic":
        raw = prov._anthropic(prompt, model, key, schema=SUMMARY_SCHEMA, web=False)
    else:
        raw = prov._openai(prompt, model, key, schema=SUMMARY_SCHEMA, web=False)
    return {
        "item": str(raw.get("item") or "")[:300],
        "params": [{"name": str(p.get("name") or "")[:80], "value": str(p.get("value") or "")[:200]}
                   for p in (raw.get("params") or []) if isinstance(p, dict)][:20],
        "requirements": _strings(raw.get("requirements"))[:10],
    }


def _work_summaries(conn: psycopg.Connection, providers: list[str]) -> bool:
    row = conn.execute(
        """UPDATE spec_summaries SET status = 'running', started_at = now()
           WHERE lot_id = (SELECT lot_id FROM spec_summaries
                           WHERE status = 'queued' AND provider = ANY(%s)
                           ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
           RETURNING lot_id, provider, model""",
        (providers,),
    ).fetchone()
    conn.commit()
    if not row:
        return False
    try:
        result, error = _summarize(conn, *row), None
    except Exception as exc:  # noqa: BLE001 — задача не должна зависнуть в running
        conn.rollback()
        logger.warning("summary of %s failed: %s", row[0], exc)
        result, error = None, str(exc)[:300]
    conn.execute(
        """UPDATE spec_summaries SET status = %s, result = %s, error = %s, finished_at = now()
           WHERE lot_id = %s""",
        ("done" if result else "error", Jsonb(result) if result else None, error, row[0]),
    )
    conn.commit()
    return True
