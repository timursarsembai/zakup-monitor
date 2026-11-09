from dataclasses import replace
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
    assert Filters.from_json(f.to_json()) == f
    # Первый заход на поиск — свежая сводка; клиентам сначала новые, сотрудникам — как привыкли.
    fresh = Filters.from_form({}, staff=False)
    assert (fresh.published_from, fresh.end_from, fresh.regions, fresh.disable_person, fresh.sort) == (
        "today", "today", [], "", "published")
    assert Filters.from_form({}, staff=True).sort == "advance"
    from zakup.filters import last_workday
    assert last_workday(date(2026, 11, 7)) == last_workday(date(2026, 11, 8)) == date(2026, 11, 6)
    assert last_workday(date(2026, 11, 9)) == date(2026, 11, 7)  # пн — с субботы, выходные тоже новые
    assert last_workday(date(2026, 11, 10)) == date(2026, 11, 10)


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
                       "supply_date": "16 дней", "enstru": [], "spec_url": None,
                       "unit_name_kz": None, "extra_desc_kz": None}
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
    _announcement(conn, 12, ends_in_days=2)          # понедельник: в пределах 2 рабочих дней
    _announcement(conn, 8, ends_in_days=-1)          # уже закончилось
    _announcement(conn, 9, prepayment=0)
    # Объявление на 1 млн, но лоты мелкие или в другом регионе — подходит только один.
    _announcement(conn, 11, total=1_000_000, lots=[
        {"id": 111, "amount": 100_000, "prepayment": 30, "kato": ["751410000"], "name": "мелкий"},
        {"id": 112, "amount": 400_000, "prepayment": 30, "kato": ["711410000"], "name": "Астана"},
        {"id": 113, "amount": 500_000, "prepayment": 0, "kato": ["191010000"], "name": "годится"},
    ])
    assert _lot_ids(conn, Filters()) == [10, 50, 120]  # по умолчанию аванс от 30%
    assert _lot_ids(conn, Filters(advance_min=0)) == [10, 50, 90, 113, 120]
    assert _lot_ids(conn, Filters(advance_min=0, advance_only=True)) == [10, 50, 120]
    assert _lot_ids(conn, Filters(advance_min=35)) == []
    assert _lot_ids(conn, Filters(advance_min=0, advance_max=0)) == [90, 113]
    assert _lot_ids(conn, Filters(advance_min=0, q="Лот или годится")) == [10, 50, 90, 113, 120]
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
    # Лот один раз на каждом языке (казахского названия нет — русское).
    assert text.count("Стол") == 2 and "Стул" in text and "· amir" in text
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
    matcher.seed(conn, sub, Filters(), "lots", NOW)
    assert matcher.run_filters(conn, NOW) == 0  # уже увиденные при создании
    _announcement(conn, 2)
    assert matcher.run_filters(conn, NOW) == 1

    def reject(chat, text, buttons=None):
        raise matcher.telegram.TelegramError(400, "Bad Request: message is too long")
    monkeypatch.setattr(matcher.telegram, "send_message", reject)
    assert matcher.deliver(conn, pause=0) == 0
    assert conn.execute("SELECT count(*) FROM matches WHERE notified_at IS NULL").fetchone()[0] == 0


def test_search_finds_kazakh_names(conn):
    _announcement(conn, 1, lots=[{"id": 10, "amount": 500_000, "prepayment": 30,
                                  "kato": ["751410000"], "name": "Стол"}])
    conn.execute("UPDATE lots SET name_kz = 'Жазу үстелі' WHERE id = 10")
    assert _lot_ids(conn, Filters(q="үстелі")) == [10]
    assert _lot_ids(conn, Filters(q="кітап немесе үстелі")) == [10]
    assert _lot_ids(conn, Filters(exclude="үстелі")) == []


def test_migrations_apply_once_and_old_databases_are_recognised(conn):
    from zakup import db

    assert db.migrate(conn) is False and db.pending(conn) == []  # фикстура уже всё применила
    names = [name for name, _ in db.migrations()]
    assert names[0] == "0001_initial" and names == sorted(names)
    # Последняя миграция «забыта» — применяется заново, и только она.
    conn.execute("DELETE FROM schema_migrations WHERE name = %s", (names[-1],))
    conn.commit()
    assert db.pending(conn) == [names[-1]]
    assert db.migrate(conn) is True and db.migrate(conn) is False
    # База прежнего образца (schema_state есть, schema_migrations пуст): первая миграция
    # считается применённой, остальные — применяются.
    conn.execute("DELETE FROM schema_migrations")
    conn.execute("CREATE TABLE schema_state (id int PRIMARY KEY, digest text NOT NULL)")
    conn.commit()
    assert db.pending(conn) == names[1:]
    assert db.migrate(conn) is True and db.pending(conn) == []


def test_huge_or_infinite_sums_are_ignored():
    # «1e200000000» в строку — сотни мегабайт: копия сайта падала от нехватки памяти.
    from zakup.filters import _decimal

    for value in ("1e200000000", "Infinity", "NaN", "1e16"):
        assert _decimal(value) is None
    assert _decimal("5 000 000") == 5_000_000 and _decimal("-1,5") == Decimal("-1.5")


def _paid_user(conn, chat="42", **columns):
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id, paid_until) VALUES ('p', %s, now() + interval '30 days') "
        "RETURNING id", (chat,)).fetchone()[0]
    for column, value in columns.items():
        conn.execute(f"UPDATE users SET {column} = %s WHERE id = %s", (value, user))
    return user


def test_first_batch_comes_as_one_message_with_pages(conn, monkeypatch):
    for ident in range(1, 26):
        _announcement(conn, ident)
    user = _paid_user(conn)
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Мебель', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters(deadline="").to_json()))).fetchone()[0]
    matcher.seed(conn, sub, Filters(deadline=""), "lots", NOW, send_open=True)
    sent, calls = [], []
    monkeypatch.setattr(matcher.telegram, "send_message",
                        lambda chat, text, buttons=None: sent.append((chat, text, buttons)) or {"message_id": 7})
    monkeypatch.setattr(matcher.telegram, "call", lambda method, **kw: calls.append((method, kw)))
    assert matcher.deliver(conn, pause=0) == 0  # отдельных сообщений нет — всё в одном
    assert len(sent) == 1
    chat, text, buttons = sent[0]
    assert "стр. 1 из 3" in text and text.count("Закупка ") == 10
    assert [b["callback_data"] for b in buttons[0]] == ["mp:1:1"]
    assert conn.execute("SELECT count(*) FROM matches WHERE notified_at IS NULL").fetchone()[0] == 0
    # «Дальше» из этого чата — следующая страница; из чужого — нет.
    query = {"id": "q", "data": "mp:1:2", "message": {"chat": {"id": 42}, "message_id": 7}}
    matcher.turn_page(conn, query)
    edited = [kw for method, kw in calls if method == "editMessageText"][0]
    assert "стр. 3 из 3" in edited["text"] and edited["text"].count("Закупка ") == 5
    assert [b["text"] for b in edited["reply_markup"]["inline_keyboard"][0]] == ["◀ Назад"]
    calls.clear()
    matcher.turn_page(conn, {**query, "message": {"chat": {"id": 99}, "message_id": 7}})
    assert [method for method, _ in calls] == ["answerCallbackQuery"]
    # Испорченная кнопка — ответ «Список устарел», без падения бота.
    for data in ("mp:1", "mp:x:1", "mp:99999999999999999999:0", "mp:1:2:3"):
        calls.clear()
        matcher.turn_page(conn, {**query, "data": data})
        assert [method for method, _ in calls] == ["answerCallbackQuery"], data
    # Повторно в тот же чат лоты не придут ни выборкой, ни по второй подписке.
    assert matcher.send_pages(conn) == 0


def test_first_batch_keeps_nearest_lots_up_to_limit(conn, monkeypatch):
    monkeypatch.setattr(matcher, "PAGE_LOTS_LIMIT", 4)
    for ident in range(1, 8):
        _announcement(conn, ident, ends_in_days=10 - ident)  # у последних срок ближе
    user = _paid_user(conn)
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Мебель', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters(deadline="").to_json()))).fetchone()[0]
    matcher.seed(conn, sub, Filters(deadline=""), "lots", NOW, send_open=True)
    lot_ids, total = conn.execute("SELECT lot_ids, total FROM match_pages").fetchone()
    assert total == 7 and len(lot_ids) == 4
    nearest = [r[0] for r in conn.execute(
        "SELECT l.id FROM lots l JOIN announcements a ON a.id = l.trd_buy_id "
        "ORDER BY a.end_date, l.id LIMIT 4").fetchall()]
    assert lot_ids == nearest == [70, 60, 50, 40]
    # Все 7 — «увиденные»: остальные 3 не придут потом по одному.
    assert conn.execute("SELECT count(*) FROM matches WHERE notified_at IS NULL").fetchone()[0] == 0
    text = matcher.render_page(conn, 1, 0)[0]
    assert "Показаны 4 лотов с ближайшим сроком из 7" in text


def test_notification_language_follows_settings(conn):
    _announcement(conn, 1)
    user = _paid_user(conn, ui_lang="kk")
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'x', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters(deadline="").to_json()))).fetchone()[0]
    conn.execute("INSERT INTO matches (subscription_id, lot_id) VALUES (%s, 10)", (sub,))

    def text():
        return matcher.format_block(matcher.pending(conn))
    # По умолчанию — язык сайта (казахский): только казахская часть.
    assert "🇷🇺" not in text() and "Мүгедектер" in text() and "Среди организаций" not in text()
    conn.execute("UPDATE users SET notify_lang = 'both'")
    assert "🇰🇿" in text() and "🇷🇺" in text()
    conn.execute("UPDATE users SET notify_lang = 'ru'")
    assert "Среди организаций" in text() and "Мүгедектер" not in text()
    # В группе — язык группы.
    conn.execute("INSERT INTO telegram_groups (chat_id, title, notify_lang) VALUES ('-1', 'g', 'kk')")
    conn.execute("UPDATE subscriptions SET chat_id = '-1'")
    assert "Мүгедектер" in text() and "Среди организаций" not in text()


def test_first_batch_limit_keeps_whole_announcement(conn, monkeypatch):
    monkeypatch.setattr(matcher, "PAGE_LOTS_LIMIT", 1)
    _announcement(conn, 1, lots=[{"id": 11, "amount": 500000, "prepayment": 30, "kato": ["751410000"], "name": "А"},
                                 {"id": 12, "amount": 500000, "prepayment": 30, "kato": ["751410000"], "name": "Б"}])
    _announcement(conn, 2, ends_in_days=5)
    user = _paid_user(conn)
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Все', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters(deadline="").to_json()))).fetchone()[0]
    matcher.seed(conn, sub, Filters(deadline=""), "lots", NOW, send_open=True)
    lot_ids, total = conn.execute("SELECT lot_ids, total FROM match_pages").fetchone()
    assert sorted(lot_ids) == [11, 12] and total == 3


def test_open_now_checkbox():
    today = date.today().isoformat()
    assert Filters.from_form({"applied": ["1"], "open_now": ["1"]}).end_from == "today"
    assert Filters.from_form({"applied": ["1"]}).end_from == ""
    assert Filters.from_form({"applied": ["1"], "open_now": ["1"], "end_from": ["2026-12-01"]}).end_from == "2026-12-01"
    query = Filters().to_query("lots")
    assert "open_now=1" in query and "end_from" not in query
    from urllib.parse import parse_qs, urlsplit
    assert Filters.from_form(parse_qs(urlsplit(query).query)).end_from == "today"
    assert Filters.from_form({"applied": ["1"], "end_from": ["today"]}).end_from == "today"  # старые ссылки
    assert today


def test_new_subscription_sends_only_new_lots_by_default(conn, monkeypatch):
    _announcement(conn, 1)
    user = _paid_user(conn)
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Только новые', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters(deadline="").to_json()))).fetchone()[0]
    matcher.seed(conn, sub, Filters(deadline=""), "lots", NOW)
    sent = []
    monkeypatch.setattr(matcher.telegram, "send_message", lambda chat, text, buttons=None: sent.append(text) or {"message_id": 1})
    assert matcher.deliver(conn, pause=0) == 0 and sent == []
    assert conn.execute("SELECT count(*) FROM match_pages").fetchone()[0] == 0
    # Уже открытое — «увиденное»: потом не придёт.
    assert conn.execute("SELECT count(*) FROM matches WHERE notified_at IS NULL").fetchone()[0] == 0


def test_today_only_checkbox_published_today(conn):
    current = Filters.from_form({"applied": ["1"], "today_only": ["1"], "open_now": ["1"]})
    assert current.published_from == "today" and current.relative()
    assert "today_only=1" in current.to_query("lots")
    for ident, published in ((1, NOW - timedelta(days=5)), (2, NOW)):
        _announcement(conn, ident)
        conn.execute("UPDATE announcements SET publish_date = %s WHERE id = %s", (published, ident))
    where, params = replace(current, statuses=[], disable_person="", regions=[], min_sum=None,
                            deadline="", advance_min=0).where(NOW, "announcements")
    assert [r[0] for r in conn.execute(f"SELECT a.id FROM announcements a WHERE {where}", params)] == [2]



def test_unchanged_portal_rows_are_not_rewritten(conn):
    item = {"id": 77, "name_ru": "Стулья", "end_date": "2099-01-01 09:00:00"}
    db.upsert_announcement(conn, item)
    first = conn.execute("SELECT xmin::text FROM announcements WHERE id = 77").fetchone()[0]
    conn.commit()
    db.upsert_announcement(conn, item)
    assert conn.execute("SELECT xmin::text FROM announcements WHERE id = 77").fetchone()[0] == first
    db.upsert_announcement(conn, {**item, "name_ru": "Стулья офисные"})
    assert conn.execute("SELECT name_ru FROM announcements WHERE id = 77").fetchone()[0] == "Стулья офисные"
    # Карточка объявления не затирает полный ответ REST по лоту.
    db.upsert_lot(conn, {"id": 770, "trd_buy_id": 77, "name_ru": "Стул", "description_ru": "полный"})
    db.upsert_lot(conn, {"id": 770, "trd_buy_id": 77, "name_ru": "Стул"}, overwrite=False)
    assert conn.execute("SELECT description_ru FROM lots WHERE id = 770").fetchone()[0] == "полный"


def test_backfilled_lots_are_marked_seen_not_sent(conn):
    # Старый, но ещё открытый лот, догруженный из прошлого, подходит под фильтр — не рассылаем.
    user = conn.execute("INSERT INTO users (email) VALUES ('a@b.kz') RETURNING id").fetchone()[0]
    conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Все', '', 'announcements', %s)""",
        (user, db.Jsonb(Filters(advance_min=0).to_json())),
    )
    _announcement(conn, 1)
    _announcement(conn, 2)
    matcher.mark_seen(conn, [10], NOW)
    assert matcher.run_filters(conn, NOW) == 1  # только 20: 10 уже «увиден»
    assert conn.execute(
        "SELECT lot_id FROM matches WHERE notified_at IS NULL").fetchall() == [(20,)]


def test_announcements_without_lots_are_hidden(conn):
    _announcement(conn, 1)
    db.upsert_announcement(conn, {"id": 2, "name_ru": "Без лотов", "total_sum": 500000,
                                  "ref_buy_status_id": 240, "end_date": "2099-01-01 09:00:00"})
    where, params = Filters(advance_min=0, disable_person="").where(NOW, "announcements")
    ids = [r[0] for r in conn.execute(f"SELECT a.id FROM announcements a WHERE {where}", params)]
    assert 1 in ids and 2 not in ids


def test_details_fill_region_and_division_codes(conn):
    from zakup import details

    _announcement(conn, 1, kato=("751410000", "711110000"))
    conn.execute("UPDATE lots SET enstru = '{\"311011\", \"170000\"}' WHERE id = 10")
    details.codes(conn, [1])
    assert conn.execute("SELECT regions, divisions FROM lots WHERE id = 10").fetchone() == (["71", "75"], ["17", "31"])
    assert conn.execute("SELECT divisions FROM announcements WHERE id = 1").fetchone() == (["17", "31"],)
    conn.execute("UPDATE lots SET regions = NULL")
    assert details.fill_codes(conn) >= 1
    assert conn.execute("SELECT count(*) FROM lots WHERE regions IS NULL").fetchone() == (0,)


def test_lot_customer_filter_by_name_and_bin(conn):
    _announcement(conn, 1)
    conn.execute("UPDATE lots SET customer_name_ru = 'КГУ Школа №5', customer_bin = '123456789012' WHERE id = 10")
    _announcement(conn, 2)
    for value, expected in (("школа №5", [10]), ("123456789012", [10]), ("больница", [])):
        assert _lot_ids(conn, Filters(advance_min=0, customer=value)) == expected


def test_active_count_includes_open_and_today_checkboxes():
    from zakup.filters import TODAY

    base = Filters(advance_min=0, disable_person="", end_from="", published_from="")
    both = Filters(advance_min=0, disable_person="", end_from=TODAY, published_from=TODAY)
    assert both.active_count() - base.active_count() == 2  # две галочки — два условия


def test_closed_lots_are_never_notified_as_new(conn):
    user = conn.execute("INSERT INTO users (email) VALUES ('c@b.kz') RETURNING id").fetchone()[0]
    sub = conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, filters)
           VALUES (%s, 'Все статусы', '', 'announcements', %s) RETURNING id""",
        (user, db.Jsonb(Filters(advance_min=0, statuses=[], stages=[], end_from="").to_json())),
    ).fetchone()[0]
    _announcement(conn, 1)
    _announcement(conn, 2, ends_in_days=-3)  # завершённое: история или карточка старого объявления
    matcher.run_filters(conn, NOW)
    lots = [r[0] for r in conn.execute("SELECT lot_id FROM matches WHERE subscription_id = %s", (sub,))]
    assert lots == [10]


def test_lot_customer_bin_includes_organizer(conn):
    _announcement(conn, 1)
    conn.execute("UPDATE announcements SET org_bin = '999999999999' WHERE id = 1")
    assert _lot_ids(conn, Filters(advance_min=0, customer="999999999999")) == [10]


def test_result_date_follows_announcement_itogi_date(conn):
    from zakup import details

    _announcement(conn, 1)
    conn.execute("INSERT INTO lot_results (lot_id, outcome) VALUES (10, 'failed')")
    details.save(conn, {"id": 1, "itogiDatePublic": "2026-10-15 10:00:00", "Lots": []})
    date = conn.execute("SELECT result_date FROM lot_results WHERE lot_id = 10").fetchone()[0]
    assert date.astimezone(PORTAL_TZ).strftime("%Y-%m-%d %H:%M") == "2026-10-15 10:00"


def test_lots_ready_at_set_once_when_lots_arrive(conn):
    from zakup import details, history

    db.upsert_announcement(conn, {"id": 7, "name_ru": "Без лотов", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 7, "Lots": []})
    assert conn.execute("SELECT lots_ready_at FROM announcements WHERE id = 7").fetchone() == (None,)
    _announcement(conn, 7)  # подробности пришли уже с лотом
    first = conn.execute("SELECT lots_ready_at FROM announcements WHERE id = 7").fetchone()[0]
    assert first is not None
    _announcement(conn, 7)
    assert conn.execute("SELECT lots_ready_at FROM announcements WHERE id = 7").fetchone()[0] == first
    assert history.lots_delay(conn)["count"] >= 0
