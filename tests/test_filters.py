from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from zakup import db, details, matcher
from zakup.filters import DEADLINE_RULE, Filters, deadline_by_rule
from zakup.goszakup import PORTAL_TZ


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 11, 2), date(2026, 11, 4)),   # пн -> ср
        (date(2026, 11, 5), date(2026, 11, 7)),   # чт -> сб
        (date(2026, 11, 6), date(2026, 11, 8)),   # пт -> вс
        (date(2026, 11, 7), date(2026, 11, 10)),  # сб -> вт
        (date(2026, 11, 8), date(2026, 11, 10)),  # вс -> вт
    ],
)
def test_deadline_rule(today, expected):
    assert deadline_by_rule(today) == expected


def test_defaults_match_the_owner_request():
    f = Filters()
    assert (f.statuses, f.disable_person, f.regions, f.min_sum, f.deadline) == (
        [240], True, ["75", "19"], Decimal(300000), DEADLINE_RULE
    )
    assert Filters.from_form({}) == f
    assert Filters.from_json(f.to_json()) == f


def test_form_parsing():
    f = Filters.from_form({
        "applied": ["1"], "status": ["240", "999", "x"], "region": ["75", "00"],
        "min_sum": ["1 000,50"], "deadline_mode": ["date"], "deadline_date": ["2026-11-20"],
        "advance_only": ["1"],
    })
    assert f.statuses == [240] and f.regions == ["75"] and f.min_sum == Decimal("1000.50")
    assert f.deadline == "2026-11-20" and f.advance_only and not f.disable_person
    assert Filters.from_form({"applied": ["1"], "deadline_mode": [""]}).deadline == ""


def test_details_parse_collects_kato_and_prepayment():
    fields, lots = details.parse({
        "id": 5, "kato": ["191010000"], "disablePersonId": 1, "countLots": 2,
        "Lots": [
            {"id": 1, "plnPointKatoList": ["751410000"],
             "Plans": [{"prepayment": 30, "PlansKato": [{"refKatoCode": "751110000"}]}]},
            {"id": 2, "plnPointKatoList": [""], "Plans": [{"prepayment": 0, "PlansKato": []}]},
        ],
    })
    assert fields["disable_person"] and fields["prepayment_max"] == 30
    assert fields["kato"] == ["191010000", "751110000", "751410000"]
    assert lots == [(1, 30.0, ["751110000", "751410000"]), (2, 0.0, [])]


NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)  # суббота, 11:00 в Алматы


def _announcement(conn, ident, *, status=240, disable=True, kato=("751410000",), total=500000,
                  ends_in_days=2, prepayment=30):
    db.upsert_announcement(conn, {"id": ident, "name_ru": f"Закупка {ident}", "total_sum": total,
                                  "ref_buy_status_id": status,
                                  "end_date": (NOW + timedelta(days=ends_in_days)).astimezone(PORTAL_TZ)
                                  .strftime("%Y-%m-%d %H:%M:%S")})
    details.save(conn, {"id": ident, "disable_person": disable, "kato": list(kato),
                        "prepayment_max": prepayment, "type_trade_id": None,
                        "subject_type_id": None, "count_lots": None, "customer_bin": None,
                        "customer_name_ru": "Школа", "last_update_date": None}, [])


def _ids(conn, f):
    where, params = f.where(NOW)
    return sorted(r[0] for r in conn.execute(f"SELECT a.id FROM announcements a WHERE {where}", params))


def test_where_applies_every_default(conn):
    _announcement(conn, 1)
    _announcement(conn, 2, status=220)
    _announcement(conn, 3, disable=False)
    _announcement(conn, 4, kato=("711410000",))
    _announcement(conn, 5, kato=("196855100",))      # Алматинская область
    _announcement(conn, 6, total=299999)
    _announcement(conn, 7, ends_in_days=4)           # после вторника
    _announcement(conn, 8, ends_in_days=-1)          # уже закончилось
    _announcement(conn, 9, prepayment=0)
    assert _ids(conn, Filters()) == [1, 5, 9]
    assert _ids(conn, Filters(advance_only=True)) == [1, 5]
    assert _ids(conn, Filters(deadline="")) == [1, 5, 7, 9]


def test_filter_subscription_notifies_each_announcement_once(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(matcher.telegram, "send_message", lambda chat, text: sent.append(text))
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES ('a@b.kz', '42') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Алматы', '', 'announcements', %s)""",
        (user, db.Jsonb(Filters().to_json())),
    )
    _announcement(conn, 1)
    _announcement(conn, 2, prepayment=0)
    assert matcher.run_filters(conn, NOW) == 2
    assert matcher.run_filters(conn, NOW) == 0
    assert matcher.deliver(conn, pause=0) == 2
    text = sent[0]
    # С авансом — первым.
    assert text.index("Закупка 1") < text.index("Закупка 2")
    assert "аванс 30%" in text and "г. Алматы" in text and "/announce/index/1" in text
