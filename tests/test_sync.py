from datetime import datetime

import pytest

from zakup import db, sync
from zakup.goszakup import PORTAL_TZ


def _announcements(n, start_id=1, step=7):
    return [
        {"id": start_id + i * step, "publish_date": f"2026-{1 + i * 12 // n:02d}-01 09:00:00",
         "name_ru": f"Закупка {i}", "total_sum": "1000.50"}
        for i in range(n)
    ]


def test_find_start_returns_oldest_matching_id(fake_client):
    items = _announcements(500)
    client = fake_client({"announcements": items})
    since = datetime(2026, 6, 1, tzinfo=PORTAL_TZ)
    expected = next(i for i in items if i["publish_date"] >= "2026-06")
    assert sync.find_start(client, "announcements", sync.published_since(since)) == expected["id"]
    assert client.requests < 40


def test_find_start_edges(fake_client):
    client = fake_client({"announcements": _announcements(10)})
    assert sync.find_start(client, "announcements", lambda item: True) == 1
    assert sync.find_start(client, "announcements", lambda item: False) is None
    assert sync.find_start(fake_client({}), "announcements", lambda item: True) is None
    two = fake_client({"announcements": [{"id": 5, "new": False}, {"id": 9, "new": True}]})
    assert sync.find_start(two, "announcements", lambda item: item["new"]) == 9


def test_sync_needs_a_start_point(conn, fake_client):
    with pytest.raises(RuntimeError):
        sync.sync(conn, fake_client({}), "announcements", max_pages=1)


def test_sync_walks_down_resumes_and_then_picks_up_new_records(conn, fake_client):
    items = _announcements(1300)  # ids 1..9094
    client = fake_client({"announcements": items})
    db.save_state(conn, "announcements", db.SyncState(items[99]["id"], None, None))

    first = sync.sync(conn, client, "announcements", max_pages=1)
    assert (first.saved, first.caught_up) == (500, False)
    # Пока проход не закончен, «загружено без дыр» не сдвигается.
    assert db.get_state(conn, "announcements").last_id == items[99]["id"]

    second = sync.sync(conn, client, "announcements", max_pages=5)
    assert (second.saved, second.caught_up) == (700, True)
    assert db.get_state(conn, "announcements").last_id == items[-1]["id"]
    assert conn.execute("SELECT count(*) FROM announcements").fetchone()[0] == 1200

    client.data["announcements"].insert(0, {"id": 10_000, "publish_date": "2026-12-01 09:00:00"})
    third = sync.sync(conn, client, "announcements", max_pages=5)
    assert (third.saved, third.last_id) == (1, 10_000)
    assert sync.sync(conn, client, "announcements", max_pages=5).saved == 0


def test_rescan_picks_up_records_published_below_the_loaded_id(conn, fake_client):
    items = _announcements(20)  # ids 1..134
    client = fake_client({"announcements": items})
    db.save_state(conn, "announcements", db.SyncState(0, None, None))
    sync.sync(conn, client, "announcements", max_pages=5)
    # Черновик с id 50 опубликовали только сейчас: sync до него уже не дойдёт.
    client.data["announcements"].append({"id": 50, "publish_date": "2026-12-01 09:00:00"})
    client.data["announcements"].sort(key=lambda i: i["id"], reverse=True)
    assert sync.sync(conn, client, "announcements", max_pages=5).saved == 0
    client.page = lambda entity, after=None, limit=3: type(client).page(client, entity, after, limit)
    assert sync.rescan(conn, client, "announcements", 30, max_pages=2) == 6
    assert db.get_state(conn, "rescan:announcements").resume_after is not None
    sync.rescan(conn, client, "announcements", 30, max_pages=10)
    assert db.get_state(conn, "rescan:announcements").resume_after is None
    assert conn.execute("SELECT 1 FROM announcements WHERE id = 50").fetchone()


def test_list_upsert_keeps_customer_from_the_card(conn):
    db.upsert_announcement(conn, {"id": 1, "customer_bin": "123", "customer_name_ru": "Акимат"})
    db.upsert_announcement(conn, {"id": 1, "name_ru": "из списка"})
    assert conn.execute(
        "SELECT customer_bin, customer_name_ru, name_ru FROM announcements"
    ).fetchone() == ("123", "Акимат", "из списка")


def test_backfill_walks_down_to_the_floor(conn, fake_client):
    items = _announcements(20)  # ids 1..134
    client = fake_client({"announcements": items})
    for item in items[10:]:  # в базе — только новые, с id 71
        db.upsert_announcement(conn, item)
    db.save_state(conn, "backfill:announcements", db.SyncState(30, None, items[10]["id"]))
    client.page = lambda entity, after=None, limit=3: type(client).page(client, entity, after, limit)
    assert sync.backfill(conn, client, "announcements", max_pages=1) == 3
    seen = []
    sync.backfill(conn, client, "announcements", max_pages=10, on_page=seen.extend)
    assert sorted(seen) == [36, 43]  # 64, 57, 50 — в первом вызове
    assert db.get_state(conn, "backfill:announcements").resume_after is None
    assert conn.execute("SELECT min(id) FROM announcements").fetchone()[0] == 36
    assert sync.backfill(conn, client, "announcements", max_pages=1) is None

