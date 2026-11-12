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



def test_history_run_from_queue_to_done(conn, fake_client):
    from datetime import date

    from zakup import history

    items = [{"id": i, "publish_date": f"2026-10-{i:02d} 09:00:00", "name_ru": f"З {i}"} for i in range(1, 21)]
    lots = [{"id": 100 + i, "trd_buy_id": i, "name_ru": f"Лот {i}"} for i in range(1, 21)]
    client = fake_client({"announcements": items, "lots": lots})
    for item in items[15:]:
        db.upsert_announcement(conn, item)
    for lot in lots[15:]:
        db.upsert_lot(conn, lot)
    conn.commit()
    run = history.start(conn, date(2026, 10, 5))
    assert history.start(conn, date(2026, 10, 1)) is None  # один запуск за раз
    assert history.step(conn, client, max_pages=10).startswith("announcements")
    row = conn.execute("SELECT status, upto, before FROM backfill_runs WHERE id = %s", (run,)).fetchone()
    assert row[0] == "running" and row[1] == date(2026, 10, 16) and row[2]["announcements"] == 5
    while history.step(conn, client, max_pages=10) != "done":
        pass
    status, after = conn.execute("SELECT status, after FROM backfill_runs WHERE id = %s", (run,)).fetchone()
    assert status == "done" and after["announcements"] == 16 and after["lots"] == 16
    assert history.step(conn, client, max_pages=10) == "idle"
    # Остановка: следующий шаг ничего не качает.
    history.start(conn, date(2026, 10, 1))
    assert history.stop(conn)
    assert history.step(conn, client, max_pages=10) == "idle"
    assert conn.execute("SELECT count(*) FROM announcements").fetchone()[0] == 16


def test_open_announcement_without_lots_is_refetched_soon(conn):
    from zakup import details

    asked = []

    class Portal:
        def graphql(self, query):
            asked.append(query)
            return {"TrdBuy": []}

    db.upsert_announcement(conn, {"id": 5, "name_ru": "Реагенты", "end_date": "2099-01-01 09:00:00"})
    conn.execute("""UPDATE announcements SET detail = '{"nameKz": "x"}',
                    details_fetched_at = now() - interval '20 minutes' WHERE id = 5""")
    details.refresh(conn, Portal(), 10)
    assert len(asked) == 1  # лотов нет — перечитали, не дожидаясь суток
    conn.execute("""UPDATE announcements SET lots_ready = true,
                    details_fetched_at = now() - interval '20 minutes' WHERE id = 5""")
    details.refresh(conn, Portal(), 10)
    assert len(asked) == 1  # лоты в подробностях есть — как обычно, раз в сутки


def test_only_one_active_history_run(conn):
    from datetime import date

    from zakup import history

    first = history.start(conn, date(2026, 10, 1))
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):  # вторая копия сайта в тот же момент: индекс 0020
        conn.execute("INSERT INTO backfill_runs (since) VALUES ('2026-09-01')")
    conn.rollback()
    assert first and history.start(conn, date(2026, 9, 1)) is None


def test_details_retry_announcement_after_deadlock(conn, monkeypatch):
    import psycopg

    from zakup import details

    class Portal:
        def graphql(self, query):
            return {"TrdBuy": [{"id": 5, "nameKz": "x", "Lots": []}, {"id": 6, "nameKz": "y", "Lots": []}]}

    for i in (5, 6):
        db.upsert_announcement(conn, {"id": i, "name_ru": "Реагенты", "end_date": "2099-01-01 09:00:00"})
    conn.commit()
    real, calls = details.save, []

    def save(conn, item):
        calls.append(item["id"])
        real(conn, item)
        if calls == [5, 6]:  # встречная служба держит лоты: база отменила вторую запись
            raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(details, "save", save)
    assert details.refresh(conn, Portal(), 10) == 2
    assert calls == [5, 6, 6]
    # Первое объявление сохранено своей транзакцией, откат второго его не задел.
    assert conn.execute("SELECT count(*) FROM announcements WHERE detail ? 'nameKz'").fetchone()[0] == 2


def test_late_plans_refetched_and_not_wiped(conn):
    """OWS дописывает пункты плана позже публикации, а у завершённых отдаёт их снова пустыми."""
    from zakup import details

    def item(plans):
        return {"id": 5, "nameKz": "x", "Lots": [
            {"id": 51, "nameRu": "Подушка", "count": 500, "amount": 1500000, "trdBuyId": 5, "Plans": plans}]}
    plan = [{"price": 3000, "RefUnits": {"nameRu": "Штука", "nameKz": "Дана"}, "refEnstruCode": "139224.992.000000"}]

    class Portal:
        answer = item([])

        def graphql(self, query):
            self.asked = getattr(self, "asked", 0) + 1
            return {"TrdBuy": [self.answer]}

    portal = Portal()
    db.upsert_announcement(conn, {"id": 5, "name_ru": "Подушки", "end_date": "2099-01-01 09:00:00",
                                  "publish_date": "2026-11-12 09:00:00"})
    conn.execute("UPDATE announcements SET publish_date = now() - interval '1 hour' WHERE id = 5")
    details.refresh(conn, portal, 10)

    def state():
        return conn.execute("SELECT a.plans_missing, l.price, l.unit_name, a.detail->'Lots'->0->'Plans' "
                            "FROM announcements a JOIN lots l ON l.trd_buy_id = a.id WHERE a.id = 5").fetchone()
    assert state()[:3] == (True, None, None)
    # Свежее, пунктов нет — перечитываем через 10 минут, а не через сутки.
    conn.execute("UPDATE announcements SET details_fetched_at = now() - interval '11 minutes' WHERE id = 5")
    portal.answer = item(plan)
    details.refresh(conn, portal, 10)
    assert portal.asked == 2 and state()[:3] == (False, 3000, "Штука")
    # Без пропусков — как обычно, раз в сутки.
    conn.execute("UPDATE announcements SET details_fetched_at = now() - interval '3 hours' WHERE id = 5")
    details.refresh(conn, portal, 10)
    assert portal.asked == 2
    # Снова пусто (завершённое) — прочитанное раньше остаётся.
    details.save(conn, item([]))
    missing, price, unit, plans = state()
    assert (missing, price, unit) == (False, 3000, "Штука") and plans == plan
    # Объявление изменили (lastUpdateDate новее) — старые пункты не возвращаем, ждём новые.
    conn.execute("UPDATE announcements SET details_update_date = '2026-11-12 09:00+05' WHERE id = 5")
    details.save(conn, {**item([]), "lastUpdateDate": "2026-11-12 10:00:00"})
    assert state()[0] is True and state()[3] == []
    # Самрук-Казына (system 3) — флаг не ставим: её подробности не из OWS.
    conn.execute("UPDATE announcements SET system = 3 WHERE id = 5")
    details.save(conn, item([]))
    assert state()[0] is False
