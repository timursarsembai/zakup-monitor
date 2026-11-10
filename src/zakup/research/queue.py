"""Очередь поисков: постановка (со списанием поиска), выдача результатов, исполнитель и возвраты."""


import logging
import threading
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
# Сотрудники ищут бесплатно, но тоже за счёт подписки: свой потолок.
STAFF_DAILY_LIMIT = 200
# Неудачный поиск возвращается на баланс, а работа ИИ уже оплачена: после стольких
# неудач за сутки новые поиски — завтра (иначе неудачи можно вызывать нарочно).
USER_DAILY_FAILURES = 5


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
    # Строка пользователя — под блокировку до конца транзакции: параллельные нажатия
    # считают лимит по очереди, а не все по старому числу.
    conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
    used, failed = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE status = 'error') FROM lot_research
           WHERE user_id = %s AND created_at > now() - interval '1 day'""",
        (user_id,),
    ).fetchone()
    limit = STAFF_DAILY_LIMIT if is_admin else USER_DAILY_LIMIT
    if used >= limit:
        conn.rollback()
        raise QuotaExceeded(f"не больше {limit} поисков в сутки")
    if not is_admin and failed >= USER_DAILY_FAILURES:
        conn.rollback()
        raise QuotaExceeded("сегодня слишком много неудачных поисков — попробуйте завтра "
                            "или напишите в поддержку")
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


# Найденного по ходу показываем не больше стольких: зациклившаяся модель или чужая страница,
# подталкивающая её вызывать инструмент без конца, не раздуют запись.
PARTIAL_MAX = 40


def _partial_saver(conn: psycopg.Connection, job: dict[str, Any], base: dict[str, Any] | None = None):
    """Предложения, найденные по ходу поиска, — сразу в result задачи «в работе» (с пометкой
    partial): страница лота показывает их, пока поиск идёт. Итог по схеме потом заменит."""
    items: list[dict[str, Any]] = []
    # «Найти ещё»: уже найденное прежде показано ниже — повторно сюда не берём.
    seen: set[str] = {i.get("url") for i in (base or {}).get("items", [])}
    state = {"step": ""}

    def write() -> None:
        try:
            conn.execute("UPDATE lot_research SET result = %s WHERE id = %s AND status = 'running'",
                         (Jsonb({"items": items, "partial": True, "step": state["step"]}), job["id"]))
            conn.commit()
        except psycopg.Error:
            # Сбой записи не должен оставить соединение в оборванной транзакции: итог поиска
            # потом пишется через него же.
            conn.rollback()
            raise

    def save(item: dict[str, Any]) -> None:
        for clean_item in clean({"items": [item]}).get("items", []):
            if clean_item["url"] in seen or len(items) >= PARTIAL_MAX:
                continue
            seen.add(clean_item["url"])
            items.append(clean_item)
            write()

    def step(text: str) -> None:
        state["step"] = text
        write()

    save.step = step  # type: ignore[attr-defined]
    return save


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
        saver = _partial_saver(conn, job, base)
        found = clean(prov._claude_code(prompt, job["model"], on_item=saver, on_step=saver.step))
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




# Поиски товаров одновременно (каждый — отдельный Claude Code). Больше — упрёмся в лимиты
# подписки; выжимки и вопросы поставщику — своей дорожкой, они не ждут длинных поисков.
RESEARCH_WORKERS = 2


def work(database_url: str, providers: list[str], once: bool = False) -> None:
    """Очередь ИИ: дорожка коротких задач (выжимки, вопросы поставщику; проверка раз в
    секунду) и RESEARCH_WORKERS дорожек поиска товаров."""
    from zakup import db

    with db.connect(database_url) as conn:
        _reap(conn, providers, True)  # «в работе» на старте — мёртвые: прошлый запуск службы
    if once:
        with db.connect(database_url) as conn:
            _light_once(conn, providers)
            if job := _take(conn, providers):
                _run(conn, job)
        return
    lanes = [threading.Thread(target=_light_lane, args=(database_url, providers), name="ai-light", daemon=True)]
    lanes += [threading.Thread(target=_research_lane, args=(database_url, providers), name=f"ai-research-{n}",
                               daemon=True) for n in range(RESEARCH_WORKERS)]
    for lane in lanes:
        lane.start()
    for lane in lanes:
        lane.join()


def _light_once(conn: psycopg.Connection, providers: list[str]) -> bool:
    from zakup import questions

    return bool(_work_summaries(conn, providers) or questions.work_one(conn, providers))


def _light_lane(database_url: str, providers: list[str]) -> None:
    """Одно соединение на дорожку (не новое каждую секунду); оборвалось — новое."""
    from zakup import db

    while True:
        try:
            with db.connect(database_url) as conn:
                while True:
                    while _light_once(conn, providers):
                        pass
                    time.sleep(1)
        except Exception as exc:  # noqa: BLE001 — дорожка не должна умирать от одной ошибки
            logger.warning("light lane: %s", exc)
            time.sleep(5)


def _research_lane(database_url: str, providers: list[str]) -> None:
    from zakup import db

    while True:
        try:
            with db.connect(database_url) as conn:
                while True:
                    _reap(conn, providers, False)
                    if job := _take(conn, providers):
                        _run(conn, job)
                    else:
                        time.sleep(2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("research lane: %s", exc)
            time.sleep(5)


def _reap(conn: psycopg.Connection, providers: list[str], first: bool) -> None:
    """Зависшие задачи — в ошибку (поиск возвращается на баланс). На старте «running»
    своих провайдеров — точно мёртвые: служба одна (дорожки запускаются после этой проверки)."""
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
    for table in ("spec_summaries", "supplier_questions"):
        conn.execute(
            f"""UPDATE {table} SET status = 'error', error = 'прервано'
                WHERE status = 'running' AND started_at < now() - interval '20 minutes'"""
        )
    conn.commit()


def _take(conn: psycopg.Connection, providers: list[str]) -> dict[str, Any] | None:
    """Следующая задача из очереди (сразу помечается «в работе») или None."""
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
    if row is None:
        return None
    return dict(zip(("id", "lot_id", "user_id", "provider", "model", "region",
                     "whole_kz", "target", "base_id"), row, strict=True))


def _run(conn: psycopg.Connection, job: dict[str, Any]) -> None:
    """Выполняет поиск и сохраняет итог; не удалось — поиск возвращается на баланс."""
    logger.info("research %s: lot %s via %s", job["id"], job["lot_id"], job["provider"])
    prov.reset_usage()
    try:
        result, error = run_job(conn, job), None
    except Exception as exc:  # noqa: BLE001 — задача не должна зависнуть в running
        conn.rollback()
        logger.warning("research %s failed: %s", job["id"], exc)
        result, error = None, str(exc)[:500]
    if result is None:
        credits.refund(conn, job["id"])
    conn.execute(
        """UPDATE lot_research SET status = %s, result = %s, error = %s,
               finished_at = now(), usage = %s WHERE id = %s AND status = 'running'""",
        ("done" if result is not None else "error",
         Jsonb(result) if result is not None else None, error,
         Jsonb(dict(prov.last_usage())) if prov.last_usage() else None, job["id"]),
    )
    conn.commit()
