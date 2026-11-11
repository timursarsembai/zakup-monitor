"""Оценки лота (lot_scores) и фильтры поиска по ним."""

from datetime import UTC, datetime
from decimal import Decimal

from zakup import db, scores
from zakup.filters import Filters


def _lot(conn, lot_id, announcement, customer="111", name="Бумага А4", amount=1000000, method=2):
    db.upsert_announcement(conn, {"id": announcement, "name_ru": "Закупка", "end_date": "2099-01-01 09:00:00"})
    conn.execute("""UPDATE announcements SET publish_date = now() - interval '1 day', lots_ready = true,
                           trade_method_id = %s, subject_type_id = 1, status_id = 240 WHERE id = %s""",
                 (method, announcement))
    db.upsert_lot(conn, {"id": lot_id, "trd_buy_id": announcement, "name_ru": name, "amount": amount})
    conn.execute("UPDATE lots SET count = 10, unit_name = 'шт', customer_bin = %s, prepayment = 0, status_id = 240 WHERE id = %s",
                 (customer, lot_id))


def test_refresh_and_filters(conn):
    _lot(conn, 1, 501)
    _lot(conn, 2, 502, customer="222")
    conn.commit()
    assert scores.refresh(conn) == 2
    row = conn.execute("SELECT bid_deposit, contract_deposit, freeze_total, dumping_floor FROM lot_scores WHERE lot_id = 1").fetchone()
    # Конкурс, товары: 1% заявки, 3% договора, порог демпинга −10%.
    assert [float(v) for v in row] == [10000, 30000, 40000, 900000]
    assert scores.refresh(conn) == 0  # свежие — не пересчитываем
    conn.execute("UPDATE lot_scores SET market_diff = 0.25, same_winner_bin = '999', repeats = 1, repeats_failed = 1 "
                 "WHERE lot_id = 1")
    conn.commit()
    now = datetime.now(UTC)

    def ids(**kw):
        where, params = Filters(**{**Filters.public().__dict__, **kw}).where(now, "lots")
        return sorted(r[0] for r in conn.execute(f"SELECT l.id FROM lots l, announcements a WHERE {where}", params))

    assert ids() == [1, 2]
    assert ids(market_above=20) == [1] and ids(market_above=30) == []
    assert ids(own_supplier="hide") == [2] and ids(own_supplier="only") == [1]
    assert ids(repeat="failed") == [1]
    assert ids(freeze_max=Decimal(39999)) == [] and ids(freeze_max=Decimal(40000)) == [1, 2]
    where, params = Filters(**{**Filters.public().__dict__, "own_supplier": "hide"}).where(now, "announcements")
    assert [r[0] for r in conn.execute(f"SELECT a.id FROM announcements a WHERE {where}", params)] == [502]


def test_filters_form_round_trip():
    f = Filters.from_form({"applied": ["1"], "market_above": ["20"], "repeat": ["failed"], "own_supplier": ["only"],
                           "customer_failed_max": ["30"], "freeze_max": ["100 000"]})
    assert (f.market_above, f.repeat, f.own_supplier, f.customer_failed_max, f.freeze_max) == (
        20, "failed", "only", 30, Decimal(100000))
    assert Filters.from_json(f.to_json()) == f
    assert len(f.score_labels()) == 5 and "заморозить не больше 100 000 ₸" in f.describe()
    back = Filters.from_form({k: [str(v)] for k, v in __import__("urllib.parse").parse.parse_qsl(f.to_query().split("?")[1])})
    assert back.own_supplier == "only" and back.freeze_max == Decimal(100000)
