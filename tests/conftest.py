import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from zakup import auth, catalog, db, orgs, tariff, web
from zakup.goszakup import Page


@pytest.fixture(scope="session")
def _schema():
    """Схема создаётся один раз на весь прогон (это секунды), между тестами — только
    очистка таблиц."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    if not urlsplit(url).path.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a *_test database: the suite wipes it")
    with db.connect(url) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.commit()
        db.migrate(connection)
        tables = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
        sequences = [row[0] for row in connection.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")]
    # TRUNCATE на сорока таблицах — секунды (новые файлы на диске); таблицы в тестах
    # крошечные, DELETE без проверки ссылок и сброс счётчиков id — миллисекунды.
    reset = ("SET session_replication_role = replica;"
             + "".join(f"DELETE FROM {t};" for t in tables)
             + "".join(f"SELECT setval('{s}', 1, false);" for s in sequences)
             + "SET session_replication_role = DEFAULT;")
    return url, reset


@pytest.fixture
def conn(_schema):
    url, reset = _schema
    with db.connect(url) as connection:
        connection.execute("SET synchronous_commit = off")
        connection.execute(reset)
        connection.commit()
        yield connection
        connection.rollback()


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch):
    """Тесты не ходят в интернет: настоящий запрос — ошибка соединения (как офлайн).
    Тестовый клиент сайта работает через свой транспорт и не задет."""
    import httpx

    def offline(self, request):
        raise httpx.ConnectError(f"tests are offline: {request.url.host}", request=request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline)
    # Проверка «адрес публичный» делает DNS-запрос — без сети это секунды на каждый сайт.
    # Адреса-цифры и localhost проверяем по-настоящему (тесты защиты), имена — «не публичный».
    import ipaddress

    from zakup import contacts
    real = contacts._public

    def public(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return real(host) if host == "localhost" else False
        return real(host)
    monkeypatch.setattr(contacts, "_public", public)


class FakeClient:
    """Отдаёт записи как настоящий API: по убыванию id, search_after=X — id меньше X."""

    def __init__(self, data: dict[str, list[dict]]):
        self.data = {
            entity: sorted(items, key=lambda i: i["id"], reverse=True)
            for entity, items in data.items()
        }
        self.requests = 0

    def page(self, entity, after=None, limit=500):
        self.requests += 1
        rest = [i for i in self.data.get(entity, []) if not after or i["id"] < after]
        items = rest[:limit]
        return Page(items, items[-1]["id"] if items else None, len(self.data.get(entity, [])))


@pytest.fixture
def fake_client():
    return FakeClient


@pytest.fixture
def client(conn, monkeypatch):
    # Не conn.info.dsn: в нём нет пароля.
    test_pool = ConnectionPool(
        os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2, open=False, timeout=5
    )
    monkeypatch.setattr(web.core, "pool", test_pool)
    # Карточки организаций догружаются с портала фоном — в тестах без сети.
    monkeypatch.setattr(web.public, "_subjects_in_background", lambda bins: None)
    # Статистика посещений пишется в фоне — в тестах сразу: иначе фоновые потоки
    # занимают соединения тестового пула и тормозят следующие тесты.
    monkeypatch.setattr(web.core, "_spawn", lambda job: job())
    # Тариф кэшируется в памяти процесса — между тестами сбрасываем.
    monkeypatch.setattr(tariff, "_cache", None)
    # Список лотов для sitemap кэшируется на час — в каждом тесте свой.
    monkeypatch.setattr(web.public, "_sitemap_lots", {"at": 0.0, "rows": []})
    monkeypatch.setattr(catalog, "_counts", {"at": 0.0, "rows": {}})
    monkeypatch.setattr(catalog, "_pages", {})
    monkeypatch.setattr(orgs, "_sitemap", {"at": 0.0, "bins": []})
    auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    conn.execute("UPDATE users SET role = 'admin' WHERE email = 'owner@mail.kz'")
    conn.commit()
    with TestClient(web.app, base_url="https://testserver") as test_client:
        yield test_client
