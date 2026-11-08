"""Очередь поисков: постановка (со списанием поиска), выдача результатов, исполнитель и возвраты."""


import logging
import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zakup import ai_config, contacts, credits, sources
from zakup.research import providers as prov
from zakup.research.context import (
    clean_region,
    lot_context,
)
from zakup.research.kaspi_query import (
    _kaspi_candidates,
)
from zakup.research.prompts import (
    build_prompt,
)
from zakup.research.providers import (
    DEFAULT_MODELS,
    ResearchError,
    api_key,
)
from zakup.research.results import (
    _base_result,
    clean,
    merge,
    without_loss,
)
from zakup.research.summaries import (
    _work_summaries,
)

logger = logging.getLogger(__name__)


STALE_AFTER = "30 minutes"


# Пользователь (не администратор) за сутки запускает не больше стольких поисков:
# каждый расходует подписку или ключ администратора.
USER_DAILY_LIMIT = 20


class QuotaExceeded(Exception):
    pass


TARGETS = (5, 10, 15, 20)


def enqueue(conn: psycopg.Connection, user_id: int, lot_id: int, region: str,
            whole_kz: bool, is_admin: bool = False, target: int = 10, more: bool = False,
            charge: bool = False) -> int:
    """Ставит поиск в очередь. more=True — «найти ещё»: дополнить последний результат.
    charge=True — списать один поиск с баланса клиента (credits.NoCredits, если пусто)."""
    # Кто ищет — решает администратор в своих настройках, для всех пользователей.
    provider, model, _ = ai_config.models(conn)
    # Свои поиски у каждого клиента: ждём только свой незаконченный (сотрудник — любой).
    active = conn.execute(
        """SELECT id FROM lot_research WHERE lot_id = %s AND status IN ('queued', 'running')
           AND (%s OR user_id = %s)""",
        (lot_id, is_admin, user_id),
    ).fetchone()
    if active:
        return active[0]
    if not is_admin:
        used = conn.execute(
            """SELECT count(*) FROM lot_research
               WHERE user_id = %s AND created_at > now() - interval '1 day'""",
            (user_id,),
        ).fetchone()[0]
        if used >= USER_DAILY_LIMIT:
            raise QuotaExceeded(f"не больше {USER_DAILY_LIMIT} поисков в сутки")
    base_id = None
    if more:
        base = conn.execute(
            """SELECT id, region, whole_kz FROM lot_research
               WHERE lot_id = %s AND status = 'done' AND (%s OR user_id = %s)
               ORDER BY id DESC LIMIT 1""",
            (lot_id, is_admin, user_id),
        ).fetchone()
        if base:
            base_id, region, whole_kz = base
    job = conn.execute(
        """INSERT INTO lot_research (lot_id, user_id, provider, model, region, whole_kz,
                                     target, base_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (lot_id, user_id, provider, model or DEFAULT_MODELS[provider], clean_region(region),
         whole_kz, target if target in TARGETS else 10, base_id),
    ).fetchone()[0]
    if charge:
        try:
            credits.charge(conn, user_id, lot_id, job)
        except credits.NoCredits:
            conn.rollback()
            raise
    conn.commit()
    return job


def latest_done(conn: psycopg.Connection, lot_id: int, owner: int | None = None) -> dict[str, Any] | None:
    """Последний готовый результат — его показываем, пока идёт новый поиск."""
    row = conn.execute(
        """SELECT id FROM lot_research WHERE lot_id = %s AND status = 'done'
           AND (%s::bigint IS NULL OR user_id = %s) ORDER BY id DESC LIMIT 1""",
        (lot_id, owner, owner),
    ).fetchone()
    return _job(conn, row[0]) if row else None


def latest(conn: psycopg.Connection, lot_id: int, owner: int | None = None) -> dict[str, Any] | None:
    """Последний запуск по лоту — любой: в очереди, в работе, готовый или с ошибкой."""
    row = conn.execute(
        "SELECT max(id) FROM lot_research WHERE lot_id = %s AND (%s::bigint IS NULL OR user_id = %s)",
        (lot_id, owner, owner),
    ).fetchone()
    return _job(conn, row[0]) if row and row[0] else None


def _url_key(url: str) -> str:
    return (url or "").strip().lower().rstrip("/").split("#")[0]


def merged_done(conn: psycopg.Connection, lot_id: int) -> dict[str, Any] | None:
    """Все готовые поиски по лоту одной таблицей (для сотрудников): одно предложение
    (по адресу) — один раз, из самого свежего поиска."""
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM lot_research WHERE lot_id = %s AND status = 'done' ORDER BY id DESC",
        (lot_id,),
    ).fetchall()]
    if not ids:
        return None
    newest = _job(conn, ids[0])
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for job_id in ids:
        result = _job(conn, job_id)["result"] if job_id != ids[0] else newest["result"]
        for item in (result or {}).get("items") or []:
            key = _url_key(item.get("url"))
            if key and key not in seen:
                seen.add(key)
                items.append(item)
    newest["result"] = {**(newest["result"] or {}), "items": items}
    newest["merged"] = len(ids)
    return newest


def _job(conn: psycopg.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT r.id, r.status, r.provider, r.model, r.region, r.whole_kz, r.result, r.error,
                  r.created_at, r.finished_at, u.email, r.target, r.base_id,
                  (SELECT count(*) FROM lot_research q
                   WHERE q.status = 'queued' AND q.provider = r.provider AND q.id < r.id)
           FROM lot_research r JOIN users u ON u.id = r.user_id WHERE r.id = %s""",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "status", "provider", "model", "region", "whole_kz", "result", "error",
            "created_at", "finished_at", "email", "target", "base_id", "ahead")
    return dict(zip(keys, row, strict=True))


def run_job(conn: psycopg.Connection, job: dict[str, Any]) -> dict[str, Any]:
    context = lot_context(conn, job["lot_id"])
    if context is None:
        raise ResearchError("нет данных лота — откройте страницу объявления, чтобы их загрузить")
    base = _base_result(conn, job)
    prompt = build_prompt(context, job["region"], job["whole_kz"],
                          sources.for_prompt(conn), sources.known_categories(conn),
                          target=job.get("target") or 10,
                          exclude=[i["url"] for i in (base or {}).get("items", [])],
                          known_sellers=contacts.known_for_prompt(conn),
                          kaspi_text=_kaspi_candidates(conn, context, base))
    key = api_key(conn, job["provider"])
    # Поиск идёт минутами: не держим транзакцию открытой.
    conn.commit()
    if job["provider"] == "claude_code":
        found = clean(prov._claude_code(prompt, job["model"]))
    else:
        runner = prov._anthropic if job["provider"] == "anthropic" else prov._openai
        found = clean(runner(prompt, job["model"], key))
    found = without_loss(found, context.get("price"))
    # Телефоны, которые ИИ не увидел (лежат в коде страницы), — достаём сами.
    contacts.enrich(found, conn)
    conn.commit()
    if base:
        # Новое — в справочник источников; в результат — прошлое плюс новое.
        sources.remember(conn, found)
        return merge(base, found)
    sources.remember(conn, found)
    return found


def work(database_url: str, providers: list[str], once: bool = False) -> None:
    """Берёт задачи по очереди. Одна за раз: подписку и ключи не нагружаем."""
    from zakup import db, questions

    first = True
    while True:
        with db.connect(database_url) as conn:
            # На старте «running» своих провайдеров — точно мёртвые: исполнитель один.
            reaped = conn.execute(
                f"""UPDATE lot_research SET status = 'error', finished_at = now(),
                       error = 'прервано: перезапуск службы'
                    WHERE status = 'running' AND provider = ANY(%s)
                      AND (%s OR started_at < now() - interval '{STALE_AFTER}')
                    RETURNING id""",
                (providers, first),
            ).fetchall()
            for (dead,) in reaped:
                credits.refund(conn, dead)
            first = False
            for table in ("spec_summaries", "supplier_questions"):
                conn.execute(
                    f"""UPDATE {table} SET status = 'error', error = 'прервано'
                        WHERE status = 'running' AND started_at < now() - interval '10 minutes'"""
                )
            conn.commit()
            # Выжимки короткие (секунды) — их вперёд длинных поисков товаров.
            while _work_summaries(conn, providers) or questions.work_one(conn, providers):
                pass
            row = conn.execute(
                """UPDATE lot_research SET status = 'running', started_at = now()
                   WHERE id = (SELECT id FROM lot_research
                               WHERE status = 'queued' AND provider = ANY(%s)
                               ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING id, lot_id, user_id, provider, model, region, whole_kz,
                             target, base_id""",
                (providers,),
            ).fetchone()
            conn.commit()
            if row:
                job = dict(zip(("id", "lot_id", "user_id", "provider", "model", "region",
                                "whole_kz", "target", "base_id"), row, strict=True))
                logger.info("research %s: lot %s via %s", job["id"], job["lot_id"], job["provider"])
                prov._last_usage.clear()
                try:
                    result, error = run_job(conn, job), None
                except Exception as exc:  # noqa: BLE001 — задача не должна зависнуть в running
                    conn.rollback()
                    logger.warning("research %s failed: %s", job["id"], exc)
                    result, error = None, str(exc)[:500]
                if result is None:
                    # Не удалось — поиск возвращается на баланс клиента.
                    credits.refund(conn, job["id"])
                conn.execute(
                    """UPDATE lot_research SET status = %s, result = %s, error = %s,
                           finished_at = now(), usage = %s WHERE id = %s AND status = 'running'""",
                    ("done" if result is not None else "error",
                     Jsonb(result) if result is not None else None, error,
                     Jsonb(dict(prov._last_usage)) if prov._last_usage else None, job["id"]),
                )
                conn.commit()
        if once:
            return
        if not row:
            time.sleep(5)
