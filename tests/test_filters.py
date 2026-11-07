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
        (date(2026, 11, 4), date(2026, 11, 6)),   # ср -> пт
        (date(2026, 11, 5), date(2026, 11, 9)),   # чт -> пн
        (date(2026, 11, 6), date(2026, 11, 10)),  # пт -> вт
        (date(2026, 11, 7), date(2026, 11, 10)),  # сб -> вт
        (date(2026, 11, 8), date(2026, 11, 10)),  # вс -> вт
    ],
)
def test_deadline_rule(today, expected):
    assert deadline_by_rule(today) == expected


def test_defaults_match_the_owner_request():
    f = Filters()
    assert (f.statuses, f.disable_person, f.regions, f.min_sum, f.deadline) == (
        [240], "only", ["75", "19"], Decimal(300000), DEADLINE_RULE
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
    assert f.deadline == "2026-11-20" and f.advance_only and f.disable_person == ""
    assert Filters.from_form({"applied": ["1"], "deadline_mode": [""]}).deadline == ""
    assert Filters.from_form({"applied": ["1"], "disable_person": ["1"]}).disable_person == "only"
    # Подписки, сохранённые до трёх вариантов «организаций инвалидов».
    assert Filters.from_json({"disable_person": True}).disable_person == "only"
    assert Filters.from_json({"disable_person": False}).disable_person == ""


def test_details_parse_lot_fields():
    fields, lots = details.parse({
        "id": 5, "kato": ["191010000"], "disablePersonId": 1, "countLots": 2,
        "Lots": [
            {"id": 1, "plnPointKatoList": ["751410000"],
             "Plans": [{"prepayment": 30, "price": 10.5, "RefUnits": {"nameRu": "Штука"},
                        "extraDescRu": " дуб ", "supplyDateRu": "16 дней",
                        "PlansKato": [{"refKatoCode": "751110000"}]}]},
            {"id": 2, "plnPointKatoList": [""], "Plans": [{"prepayment": 0, "price": 1},
                                                          {"prepayment": 0, "price": 2}]},
            {"id": 3, "isDeleted": 1, "Plans": [{"prepayment": 90}]},
        ],
    })
    assert fields["disable_person"] and fields["prepayment_max"] == 30
    assert fields["kato"] == ["191010000", "751110000", "751410000"]
    assert lots[0] == {"id": 1, "prepayment": 30.0, "kato": ["751110000", "751410000"],
                       "price": 10.5, "unit_name": "Штука", "extra_desc": "дуб",
                       "supply_date": "16 дней", "enstru": []}
    assert lots[1]["price"] is None and lots[1]["kato"] == []
    assert len(lots) == 2


NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)  # суббота, 11:00 в Алматы


def _announcement(conn, ident, *, status=240, disable=True, kato=("751410000",), total=500000,
                  ends_in_days=3, prepayment=30, lots=None):
    end = (NOW + timedelta(days=ends_in_days)).astimezone(PORTAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    db.upsert_announcement(conn, {"id": ident, "name_ru": f"Закупка {ident}", "total_sum": total,
                                  "ref_buy_status_id": status, "end_date": end})
    lots = lots or [{"id": ident * 10, "amount": total, "prepayment": prepayment,
                     "kato": list(kato), "name": f"Лот {ident}"}]
    item = {"id": ident, "disablePersonId": 1 if disable else 0, "kato": [], "Lots": [
        {"id": lot["id"], "trdBuyId": ident, "nameRu": lot["name"], "amount": lot["amount"],
         "refLotStatusId": status,
         "plnPointKatoList": lot["kato"], "Plans": [{"prepayment": lot["prepayment"]}]}
        for lot in lots
    ]}
    details.save(conn, item)


def _lot_ids(conn, f):
    where, params = f.where(NOW)
    return sorted(r[0] for r in conn.execute(
        f"SELECT l.id FROM lots l, announcements a WHERE {where}", params))


def test_where_applies_every_default_per_lot(conn):
    _announcement(conn, 1)
    _announcement(conn, 2, status=220)
    _announcement(conn, 3, disable=False)
    _announcement(conn, 4, kato=("711410000",))
    _announcement(conn, 5, kato=("196855100",))      # Алматинская область
    _announcement(conn, 6, total=299999)
    _announcement(conn, 7, ends_in_days=4)           # после вторника
    _announcement(conn, 12, ends_in_days=2)          # понедельник: раньше дня по правилу
    _announcement(conn, 8, ends_in_days=-1)          # уже закончилось
    _announcement(conn, 9, prepayment=0)
    # Объявление на 1 млн, но лоты мелкие или в другом регионе — подходит только один.
    _announcement(conn, 11, total=1_000_000, lots=[
        {"id": 111, "amount": 100_000, "prepayment": 30, "kato": ["751410000"], "name": "мелкий"},
        {"id": 112, "amount": 400_000, "prepayment": 30, "kato": ["711410000"], "name": "Астана"},
        {"id": 113, "amount": 500_000, "prepayment": 0, "kato": ["191010000"], "name": "годится"},
    ])
    assert _lot_ids(conn, Filters()) == [10, 50]  # по умолчанию аванс от 30%
    assert _lot_ids(conn, Filters(advance_min=0)) == [10, 50, 90, 113]
    assert _lot_ids(conn, Filters(advance_min=0, advance_only=True)) == [10, 50]
    assert _lot_ids(conn, Filters(advance_min=35)) == []
    assert _lot_ids(conn, Filters(advance_min=0, advance_max=0)) == [90, 113]
    assert _lot_ids(conn, Filters(advance_min=0, q="Лот или годится")) == [10, 50, 90, 113]
    assert Filters.from_json({"statuses": [240]}).advance_min == 0
    # «Окончание с» по умолчанию — сегодня, и на подписку это не влияет.
    assert Filters().end_from == "today" and Filters().relative()
    assert not Filters(end_from="2026-11-01").relative()
    assert Filters.from_form({"applied": ["1"], "advance_min": ["30"], "advance_max": ["500"]}
                             ).describe().endswith("аванс 30–100%")
    assert _lot_ids(conn, Filters(advance_min=0, deadline="")) == [10, 50, 70, 90, 113, 120]
    assert _lot_ids(conn, Filters(advance_min=0, deadline="2026-11-10")) == [10, 50, 90, 113, 120]


def test_filter_subscription_sends_lots_grouped_by_announcement(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(matcher.telegram, "send_message", lambda chat, text: sent.append(text))
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES ('a@b.kz', '42') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Алматы', '', 'announcements', %s)""",
        (user, db.Jsonb(Filters(advance_min=0).to_json())),
    )
    _announcement(conn, 1, lots=[
        {"id": 11, "amount": 400_000, "prepayment": 30, "kato": ["751410000"], "name": "Стол"},
        {"id": 12, "amount": 400_000, "prepayment": 0, "kato": ["751410000"], "name": "Стул"},
    ])
    assert matcher.run_filters(conn, NOW) == 2
    assert matcher.run_filters(conn, NOW) == 0
    assert matcher.deliver(conn, pause=0) == 2
    assert len(sent) == 1
    text = sent[0]
    assert text.index("Стол") < text.index("Стул")  # с авансом — первым
    assert "аванс 30%" in text and "/a/1" in text


def test_group_gets_each_lot_once_and_announcement_scope_sends_all_lots(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(matcher.telegram, "send_message", lambda chat, text: sent.append((chat, text)))
    conn.execute("INSERT INTO telegram_groups (chat_id, title) VALUES ('-100', 'Партнёры')")
    users = [conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES (%s, %s) RETURNING id", (email, chat)
    ).fetchone()[0] for email, chat in (("amir", "1"), ("serik", "2"))]
    lot_filter = Filters(min_sum=Decimal(300_000), advance_min=0)
    for user, name, scope in ((users[0], "Мебель", "lots"), (users[1], "Всё", "announcements")):
        conn.execute(
            """INSERT INTO subscriptions (user_id, name, query, kind, filters, scope, chat_id)
               VALUES (%s, %s, '', 'announcements', %s, %s, '-100')""",
            (user, name, db.Jsonb(lot_filter.to_json()), scope))
    _announcement(conn, 1, total=500_000, lots=[
        {"id": 11, "amount": 400_000, "prepayment": 30, "kato": ["751410000"], "name": "Стол"},
        {"id": 12, "amount": 100_000, "prepayment": 0, "kato": ["751410000"], "name": "Стул"},
    ])
    # «Мебель» (лоты): только стол; «Всё» (объявления): объявление целиком — стол и стул.
    assert matcher.run_filters(conn, NOW) == 3
    assert matcher.deliver(conn, pause=0) == 2
    assert [chat for chat, _ in sent] == ["-100"]
    text = sent[0][1]
    assert text.count("Стол") == 1 and "Стул" in text and "· amir" in text
    assert conn.execute("SELECT count(*) FROM matches WHERE notified_at IS NULL").fetchone()[0] == 0


def test_review_fixes_status_scope_seed_and_rejected_message(conn, monkeypatch):
    _announcement(conn, 1)
    # Статус «Завершено» есть только у объявлений: на странице лотов — пусто, а не всё подряд.
    assert _lot_ids(conn, Filters(statuses=[350], advance_min=0)) == []
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES ('a', '42') RETURNING id").fetchone()[0]
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'x', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters().to_json()))).fetchone()[0]
    monkeypatch.setattr(matcher, "FIRST_BATCH_LIMIT", 0)
    matcher.seed(conn, sub, Filters(), "lots", NOW)
    assert matcher.run_filters(conn, NOW) == 0  # уже увиденные при создании
    _announcement(conn, 2)
    assert matcher.run_filters(conn, NOW) == 1

    def reject(chat, text):
        raise matcher.telegram.TelegramError(400, "Bad Request: message is too long")
    monkeypatch.setattr(matcher.telegram, "send_message", reject)
    assert matcher.deliver(conn, pause=0) == 0
    assert conn.execute("SELECT count(*) FROM matches WHERE notified_at IS NULL").fetchone()[0] == 0
