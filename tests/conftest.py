import os
from urllib.parse import urlsplit

import pytest

from zakup import db
from zakup.goszakup import Page


@pytest.fixture
def conn():
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
        yield connection
        connection.rollback()


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
