"""Веб-интерфейс Tenderim: сборка приложения из разделов (см. модули пакета).

Запуск: `uvicorn zakup.web:app`. Разделы — отдельные модули со своим router;
общее (база, шаблоны, доступ) — в core, обработчики каждого запроса — в middleware.
"""

import sys
from contextlib import asynccontextmanager
from importlib.resources import files

import psycopg
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from zakup.web import accounts, ai, billing, cabinet, catalog, core, middleware, public, settings, shop, staff


@asynccontextmanager
async def lifespan(_: FastAPI):
    core.pool.open()
    # Схема до первого запроса, если таблицы свободны. Заняты сборщиком — не ждём:
    # сайт важнее, миграцию сделает сборщик при своём запуске.
    from zakup import db

    try:
        with core.pool.connection() as conn:
            db.migrate(conn, attempts=1)
    except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected):
        core.logger.warning("migration skipped at startup: tables are busy")
    # Аналитику рынка считаем сразу (≈10 с), а не на первом посетителе. В тестах — нет:
    # фоновый расчёт мешал бы пересозданию тестовой базы.
    if "pytest" not in sys.modules:
        core._spawn(public._recount_market)
        core._spawn(public._recount_catalog)
    yield
    core.pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(files("zakup").joinpath("static"))), name="static")
middleware.install(app)
# Порядок разделов — как порядок адресов в прежнем общем файле (важен при совпадении путей).
for section in (public, accounts, billing, staff, catalog, shop, cabinet, ai, settings):
    app.include_router(section.router)
