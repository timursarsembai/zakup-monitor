from datetime import datetime

from zakup import db, sync
from zakup.goszakup import PORTAL_TZ


def _announcements(n, start_id=1, step=7):
    return [
        {"id": start_id + i * step, "publish_date": f"2026-{1 + i * 12 // n:02d}-01 09:00:00",
         "name_ru": f"Закупка {i}", "total_sum": "1000.50"}
        for i in range(n)
    ]


def test_find_start_returns_cursor_just_before_first_matching_record(fake_client):
    items = _announcements(500)
    client = fake_client({"announcements": items})
    since = datetime(2026, 6, 1, tzinfo=PORTAL_TZ)
    after = sync.find_start(client, "announcements", sync.published_since(since))
    first = client.page("announcements", after=after, limit=1).items[0]
    expected = next(i for i in items if i["publish_date"] >= "2026-06")
    assert first["id"] == expected["id"]
    assert client.requests < 60


def test_find_start_handles_everything_new_and_nothing_new(fake_client):
    client = fake_client({"announcements": _announcements(10)})
    assert sync.find_start(client, "announcements", lambda item: True) == 0
    assert sync.find_start(client, "announcements", lambda item: False) is None
    assert sync.find_start(fake_client({}), "announcements", lambda item: True) is None


def test_sync_saves_pages_and_resumes_from_cursor(conn, fake_client):
    client = fake_client({"announcements": _announcements(450)})
    first = sync.sync(conn, client, "announcements", max_pages=2)
    assert (first.saved, first.caught_up) == (400, False)
    second = sync.sync(conn, client, "announcements", max_pages=5)
    assert (second.saved, second.caught_up) == (50, True)
    assert conn.execute("SELECT count(*) FROM announcements").fetchone()[0] == 450
    assert db.get_cursor(conn, "announcements") == 1 + 449 * 7
    assert sync.sync(conn, client, "announcements", max_pages=5).saved == 0


def test_find_start_when_second_record_is_the_first_new_one(fake_client):
    client = fake_client({"announcements": [{"id": 1, "new": False}, {"id": 2, "new": True}]})
    assert sync.find_start(client, "announcements", lambda item: item["new"]) == 1
