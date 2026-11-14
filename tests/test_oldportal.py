"""Резерв лотов со страниц old.goszakup.gov.kz (zakup.oldportal) и замена временных лотов
лотами OWS (db.adopt_temp_lot)."""

from datetime import UTC, datetime, timedelta

from zakup import auth, db, details, marks, oldportal

NOW = datetime.now(UTC).replace(microsecond=0)
TEMP = db.TEMP_LOT_BASE + 88602578

# Как на портале 14.11.2026 (сокращено): строка вкладки «Лоты» и окно лота.
LIST = """<table class="table"><tr><th>№ п/п</th><th>Номер лота</th></tr>
<tr><td>1</td><td><a href='#' class='btn-select-lot'
   data-lot-id='88602578'> 88602578-ЗЦП2</a></td><td>АО</td></tr>
<tr><td>2</td><td><a href='#' class='btn-select-lot'
   data-lot-id='88602579'> 88602579-ЗЦП2</a></td><td>АО</td></tr></table>{pager}"""
PAGER = ('<ul class="pagination"><li class="active"><a>1</li></a><li><a href="https://old.goszakup.gov.kz/'
         'ru/announce/index/17962021?tab=lots&amp;page=2">2</a></li></ul>')


def _window(number="88602578-ЗЦП2", advance="30"):
    return f"""<div class="panel-body"><table class="table table-bordered table-hover">
<tr><th>Лот №</th><td>{number}</td></tr>
<tr><th>Статус лота</th><td>Опубликован</td></tr>
<tr><th>БИН заказчика</th><td>000540002083</td></tr>
<tr><th>Наименование заказчика</th><td>Акционерное общество &quot;Казтелерадио&quot;</td></tr>
<tr><th>Код ТРУ</th><td>711211.000.000001</td></tr>
<tr><th>Наименование ТРУ</th><td>Услуги консультационные технические</td></tr>
<tr><th>Краткая характеристика</th><td>Услуги консультационные технические</td></tr>
<tr><th>Дополнительная характеристика</th><td>Услуги по диагностированию</td></tr>
<tr><th>Цена за единицу</th><td>392241.12</td></tr>
<tr><th>Единица измерения</th><td>Одна услуга</td></tr>
<tr><th>Количество</th><td>2</td></tr>
<tr><th>Сумма 1 год</th><td>784 482.24</td></tr>
<tr>
    <th>Запланированная сумма</th>
    <td>784482.24</td>
</tr>
<tr><th>Размер авансового платежа %</th><td>{advance}</td></tr>
<tr><th>Место поставки товара, КАТО</th><td>
  <p>751410000, г.Алматы, Бостандыкский район,  согласно технической спецификации<br><b>Количество, объем: 1</b></p>
  <p>195020100, Алматинская область, г.Талгар<br><b>Количество, объем: 1</b></p></td></tr>
<tr><th>Срок поставки ТРУ</th><td>по 31.12.2026 г.</td></tr>
</table></div>"""


class Portal:
    """Подмена oldportal.Client: страницы по пути, вызовы — в calls."""

    def __init__(self, pages, windows):
        self.pages, self.windows, self.calls = pages, windows, []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "POST":
            if (page := self.windows.get(kwargs["data"]["id"])) is None:
                raise oldportal.PortalError("timeout")
            return page
        return self.pages[(path, (kwargs.get("params") or {}).get("page", 1))]


def _announcement(conn, aid=17962021, disabled=True):
    db.upsert_announcement(conn, {"id": aid, "number_anno": f"{aid}-1", "name_ru": "Услуги",
                                  "ref_buy_status_id": 220, "ref_trade_methods_id": 3,
                                  "publish_date": (NOW - timedelta(hours=5)).isoformat(),
                                  "end_date": (NOW + timedelta(days=2)).isoformat()})
    details.save(conn, {"id": aid, "nameKz": "x", "disablePersonId": 1 if disabled else 0, "refTypeTradeId": 1,
                        "kato": ["750000000"], "Files": [{"originalName": "Документация.pdf"}], "Lots": []})
    conn.commit()


def _portal(aid=17962021):
    path = f"/ru/announce/index/{aid}"
    return Portal({(path, 1): LIST.format(pager=PAGER), (path, 2): LIST.format(pager="")},
                  {88602578: _window(), 88602579: _window("88602579-ЗЦП2", "0")})


def test_parse_lot_window():
    fields = oldportal.parse_lot(_window())
    assert fields["Лот №"] == "88602578-ЗЦП2" and fields["Наименование заказчика"] == 'Акционерное общество "Казтелерадио"'
    assert fields["Запланированная сумма"] == "784482.24" and fields["Размер авансового платежа %"] == "30"
    assert fields["places"] == [("751410000", "г.Алматы, Бостандыкский район, согласно технической спецификации"),
                                ("195020100", "Алматинская область, г.Талгар")]
    assert oldportal.lot_links(LIST.format(pager="")) == [(88602578, "88602578-ЗЦП2"), (88602579, "88602579-ЗЦП2")]
    assert oldportal.has_next(PAGER, 1) and not oldportal.has_next(PAGER, 2)


def test_fills_lots_with_delivery_place_and_advance(conn):
    _announcement(conn)
    portal = _portal()
    stats = oldportal.run(conn, portal, NOW)
    assert stats == {"todo": 1, "saved": 1, "lots": 2, "empty": 0, "failed": 0}
    # Вторая страница повторила те же лоты — листать дальше незачем, окна лотов — по разу.
    assert sum(method == "POST" for method, _, _ in portal.calls) == 2
    row = conn.execute("SELECT lot_number, amount::float, count::float, prepayment::float, kato, price::float, "
                       "unit_name, enstru, customer_bin, status_id FROM lots WHERE id = %s", (TEMP,)).fetchone()
    assert row == ("88602578-ЗЦП2", 784482.24, 2, 30, ["195020100", "751410000"], 392241.12, "Одна услуга",
                   ["711211.000.000001"], "000540002083", 220)
    ready, prepayment, kato, disabled, trade, detail = conn.execute(
        "SELECT lots_ready, prepayment_max, kato, disable_person, type_trade_id, detail FROM announcements "
        "WHERE id = 17962021").fetchone()
    # Отметки OWS (среди инвалидов, тип торгов, документы) — прежние, лоты и аванс — с портала.
    assert ready and float(prepayment) == 30 and disabled and trade == 1 and "751410000" in kato
    assert detail["source"] == "oldportal" and detail["Files"][0]["originalName"] == "Документация.pdf"
    # Готово — больше не берём.
    assert oldportal.run(conn, portal, NOW)["todo"] == 0


def test_skips_when_ows_lots_exist_and_marks_failures(conn):
    _announcement(conn)
    _announcement(conn, 17962022)
    db.upsert_lot(conn, {"id": 43900001, "trd_buy_id": 17962022, "lot_number": "1-ЗЦП1", "name_ru": "Есть"})
    conn.commit()
    portal = _portal()
    portal.windows.pop(88602579)  # окно второго лота не открылось — объявление не сохраняем
    stats = oldportal.run(conn, portal, NOW)
    assert stats["todo"] == 1 and stats["failed"] == 1 and stats["saved"] == 0
    assert conn.execute("SELECT count(*) FROM lots WHERE id >= %s", (db.TEMP_LOT_BASE,)).fetchone()[0] == 0
    # Неудача — снова не раньше чем через час.
    assert oldportal.run(conn, _portal(), NOW)["todo"] == 0
    assert oldportal.run(conn, _portal(), NOW + timedelta(minutes=61))["saved"] == 1


def test_ows_lot_replaces_temporary_one(conn, client):
    _announcement(conn)
    oldportal.run(conn, _portal(), NOW)
    user = auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    marks.set_mark(conn, user, TEMP, "interested")
    sub = conn.execute("INSERT INTO subscriptions (user_id, name, query) VALUES (%s, 'Услуги', 'услуги') RETURNING id",
                       (user,)).fetchone()[0]
    conn.execute("INSERT INTO matches (subscription_id, lot_id, notified_at) VALUES (%s, %s, now())", (sub, TEMP))
    conn.commit()
    # Список лотов OWS ожил: лот пришёл с настоящим номером.
    db.upsert_lot(conn, {"id": 43900500, "trd_buy_id": 17962021, "lot_number": "88602578-ЗЦП2",
                         "name_ru": "Услуги консультационные технические"})
    conn.commit()
    assert conn.execute("SELECT 1 FROM lots WHERE id = %s", (TEMP,)).fetchone() is None
    # Место поставки, аванс и цена (из окна лота) — у настоящего, пока OWS не дал своих.
    assert conn.execute("SELECT prepayment::float, kato, price::float FROM lots WHERE id = 43900500").fetchone() == (
        30, ["195020100", "751410000"], 392241.12)
    assert marks.of_user(conn, user, [43900500]) == {43900500: ("interested", "")}
    # «Уже присылали» — перешло: второго уведомления о том же лоте не будет.
    assert conn.execute("SELECT notified_at IS NOT NULL FROM matches WHERE lot_id = 43900500").fetchone() == (True,)
    lots = conn.execute("SELECT detail->'Lots' FROM announcements WHERE id = 17962021").fetchone()[0]
    assert [lot["id"] for lot in lots] == [43900500, db.TEMP_LOT_BASE + 88602579]
    # Старая ссылка (из уведомления) ведёт на настоящий лот.
    response = client.get(f"/lots/{TEMP}?sup=1", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/lots/43900500?sup=1"
    # Подробности OWS с лотами: временный без пары (лот убрали) удаляется, остаются лоты OWS.
    details.save(conn, {"id": 17962021, "nameKz": "x", "disablePersonId": 1, "Files": [],
                        "Lots": [{"id": 43900500, "lotNumber": "88602578-ЗЦП2", "trdBuyId": 17962021,
                                  "nameRu": "Услуги", "isDeleted": 0, "Plans": [], "Files": []}]})
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM lots WHERE trd_buy_id = 17962021 ORDER BY id")]
    assert ids == [43900500]
    # Ссылка на убранный лот — на объявление.
    response = client.get(f"/lots/{db.TEMP_LOT_BASE + 88602579}", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/a/17962021"


def test_incomplete_lot_list_is_not_saved(conn, monkeypatch):
    """Страниц больше предела — неполный список не сохраняем как полный."""
    _announcement(conn)
    monkeypatch.setattr(oldportal, "MAX_PAGES", 1)
    stats = oldportal.run(conn, _portal(), NOW)
    assert stats["failed"] == 1 and stats["saved"] == 0
    assert not conn.execute("SELECT lots_ready FROM announcements WHERE id = 17962021").fetchone()[0]
