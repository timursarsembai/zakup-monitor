"""Резерв госзакупок с платформы zakup.gov.kz (zakup.reserve): номера — номера OWS."""

from datetime import UTC, datetime, timedelta

from zakup import db, details, reserve

NOW = datetime.now(UTC).replace(microsecond=0)
ORG = "100240023650"


def _ts(delta: timedelta) -> int:
    return int((NOW + delta).timestamp())


def _row(pid, ext, system=4, published=None):
    return {"id": pid, "external_id": ext, "system_id": system, "announcement_number": f"{ext}-1",
            "publish_date": (published or NOW - timedelta(minutes=5)).isoformat()}


def _card(pid, ext, status="240", disabled=True):
    org = {"iin_bin": ORG, "name": "ГКП «Енбекшиказахская ЦРБ»"}
    return {"id": pid, "external_id": ext, "announcement_number": f"{ext}-1",
            "name": "приобретение хозяйственных товаров", "name_ru": "приобретение хозяйственных товаров",
            "name_kk": "шаруашылық заттар сатып алу", "publish_date": _ts(-timedelta(minutes=5)),
            "offer_start_date": _ts(timedelta(minutes=5)), "offer_end_date": _ts(timedelta(days=4)),
            "total_price": 1500000.0, "lot_count": 1, "is_among_disabled": disabled,
            "status": {"id": 9, "name": "Опубликовано (прием ценовых предложений)", "egz_code": status},
            "purchase_type": {"id": 10, "egz_code": "1"},
            "purchase_method": {"id": 12, "name_ru": "Запрос ценовых предложений", "egz_code": "3"},
            "purchase_subject": {"code": "G", "egz_code": "G"},
            "organizer": org, "customer": None,
            "organizer_address_ru": "194020100, none, КАЗАХСТАН, г. ЕСИК, ул. АБАЙ, д. 336"}


def _lots(lot_ext, place=None):
    return [{"id": 61846479, "external_id": lot_ext, "lot_number": "88569522-ЗЦП1", "name": "Подушка",
             "description": "спальная, с наполнителем", "total_price": 1500000.0, "is_externally_deleted": False,
             "status": {"name": "Опубликован (прием ценовых предложений)", "egz_code": "240"},
             "plan_items": [{"count": 500, "unit_price": 3000.0, "total_price": 1500000.0, "prepayment": None,
                             "name_ru": "Подушка", "name_kk": "Жастық",
                             "description_ru": "спальная, с наполнителем", "description_kk": "жатын",
                             "extra_description_ru": "Подушка спальная, с наполнителем",
                             "delivery_deadline_ru": "до 31 декабря 2026 года",
                             "organization": {"iin_bin": ORG, "name_ru": "ГКП «Енбекшиказахская ЦРБ»"},
                             "enstru": {"code": "139224.992.000000"}, "measure": {"name": "Штука"},
                             "year": {"egz_code": "2026"},
                             "delivery_address": {"code": place, "full_name_ru": "Алматинская обл."} if place else None}]}]


class Platform:
    def __init__(self, rows, cards=None, lots=None):
        self.rows, self.cards, self.lots, self.calls = rows, cards or {}, lots or {}, []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/announcements/":
            if "announcement_number" in params:
                return {"results": [r for r in self.rows if r["announcement_number"] == params["announcement_number"]
                                    and r["system_id"] == params["system_id"]]}
            rows = [r for r in self.rows if r["system_id"] == params["system_id"]]
            return {"results": rows[params["offset"]:params["offset"] + params["limit"]], "next": None}
        pid = int(path.split("/")[2])
        return self.lots[pid] if path.endswith("/lots/") else self.cards[pid]


def test_to_detail_uses_ows_codes_and_numbers():
    detail = reserve.to_detail(_card(41074043, 17946359), _lots(43878609), NOW)
    assert detail["id"] == 17946359 and detail["refBuyStatusId"] == 240 and detail["refTradeMethodsId"] == 3
    assert detail["disablePersonId"] == 1 and detail["refSubjectTypeId"] == 1 and detail["finYear"] == [2026]
    lot = detail["Lots"][0]
    assert lot["id"] == 43878609 and lot["refLotStatusId"] == 240 and lot["nameKz"] == "Жастық"
    fields, lots = details.parse(detail)
    # Места поставки в пунктах нет — регион организатора из его адреса.
    assert fields["kato"] == ["194020100"] and fields["customer_bin"] == ORG
    assert lots[0]["price"] == 3000.0 and lots[0]["unit_name"] == "Штука"
    assert lots[0]["enstru"] == ["139224.992.000000"]
    assert reserve._kato("КАЗАХСТАН, 395420100, Костанайская область") == "395420100"
    assert reserve._kato("КАЗАХСТАН, г. Астана") is None
    # Статус без кода OWS — по сроку.
    assert reserve.to_detail(_card(1, 2, status=None), [], NOW)["refBuyStatusId"] == 210


def test_fresh_announcement_saved_before_ows_and_ows_completes_it(conn):
    platform = Platform([_row(41074043, 17946359), _row(41000001, 500, system=1)],
                        cards={41074043: _card(41074043, 17946359)}, lots={41074043: _lots(43878609, "194020100")})
    # 500 у нас уже есть с лотами (OWS успел) — не трогаем.
    db.upsert_announcement(conn, {"id": 500, "name_ru": "Есть", "end_date": "2099-01-01 09:00:00"})
    conn.execute("UPDATE announcements SET lots_ready = true WHERE id = 500")
    conn.commit()
    stats = reserve.Reserve(platform).run(conn, NOW)
    assert stats["saved"] == 1 and ("/announcements/41000001/", {}) not in platform.calls
    row = conn.execute("SELECT system, status_id, trade_method_id, disable_person, lots_ready, details_fetched_at "
                       "FROM announcements WHERE id = 17946359").fetchone()
    assert row == (1, 240, 3, True, True, None)  # подробности OWS — на ближайшем круге
    lot = conn.execute("SELECT trd_buy_id, price, unit_name, kato, customer_bin FROM lots WHERE id = 43878609").fetchone()
    assert lot[0] == 17946359 and float(lot[1]) == 3000 and lot[2] == "Штука" and lot[3] == ["194020100"]
    assert lot[4] == ORG
    # Второй проход: уже с лотами — ни карточек, ни лотов.
    platform.calls.clear()
    assert reserve.Reserve(platform).run(conn, NOW)["saved"] == 0
    assert all(path == "/announcements/" for path, _ in platform.calls)

    # OWS запаздывает: объявление уже есть, лотов ещё нет — лоты платформы не затираем.
    class Early:
        def graphql(self, query):
            return {"TrdBuy": [{"id": 17946359, "nameKz": "x", "Lots": []}]}
    details.refresh(conn, Early(), 10)
    detail, ready = conn.execute("SELECT detail, lots_ready FROM announcements WHERE id = 17946359").fetchone()
    assert ready and detail["Lots"] and detail["source"] == "platform"
    # …и спросим OWS снова на следующем круге, а не через сутки.
    conn.execute("UPDATE announcements SET details_fetched_at = now() - interval '2 minutes' WHERE id = 17946359")
    asked = []

    class Counting(Early):
        def graphql(self, query):
            asked.append(query)
            return super().graphql(query)
    details.refresh(conn, Counting(), 10)
    assert asked
    # OWS прислал подробности: файлы есть, пунктов плана ещё нет — пункты платформы остаются.
    class Ows:
        def graphql(self, query):
            return {"TrdBuy": [{"id": 17946359, "nameKz": "x", "lastUpdateDate": "2026-11-12 09:16:39",
                                "Files": [{"originalName": "ТС.pdf"}],
                                "Lots": [{"id": 43878609, "count": 500, "amount": 1500000, "trdBuyId": 17946359,
                                          "nameRu": "Подушка", "Plans": []}]}]}
    details.refresh(conn, Ows(), 10)
    detail, missing = conn.execute("SELECT detail, plans_missing FROM announcements WHERE id = 17946359").fetchone()
    assert detail["Files"] and detail["Lots"][0]["Plans"][0]["price"] == 3000.0 and missing is False
    assert "source" not in detail


def test_old_and_lotless_skipped(conn):
    old = _row(41000002, 600, published=NOW - timedelta(days=2))
    platform = Platform([_row(41000003, 700), old], cards={41000003: _card(41000003, 700)}, lots={41000003: []})
    res = reserve.Reserve(platform)
    assert res.run(conn, NOW) == {"listed": 1, "saved": 0, "plans": 0, "failed": 0}
    assert conn.execute("SELECT count(*) FROM announcements").fetchone()[0] == 0
    # Лотов на платформе нет — не спрашиваем снова до RETRY.
    platform.calls.clear()
    res.run(conn, NOW + timedelta(minutes=2))
    assert ("/announcements/41000003/", {}) not in platform.calls


def test_fills_missing_plans_into_ows_detail(conn):
    db.upsert_announcement(conn, {"id": 17946359, "number_anno": "17946359-1", "name_ru": "Подушки",
                                  "end_date": "2099-01-01 09:00:00", "publish_date": NOW.isoformat()})
    details.save(conn, {"id": 17946359, "nameKz": "x", "Files": [{"originalName": "ТС.pdf"}],
                        "Lots": [{"id": 43878609, "trdBuyId": 17946359, "nameRu": "Подушка", "count": 500,
                                  "amount": 1500000, "Plans": []}]})
    conn.commit()
    platform = Platform([_row(41074043, 17946359)], lots={41074043: _lots(43878609)})
    res = reserve.Reserve(platform)
    res._fresh = lambda conn, now: []  # только пункты плана
    conn.execute("UPDATE announcements SET details_fetched_at = '2026-11-12 08:00+05' WHERE id = 17946359")
    conn.commit()
    assert res.run(conn, NOW)["plans"] == 1
    detail, missing, price = conn.execute(
        "SELECT a.detail, a.plans_missing, l.price FROM announcements a JOIN lots l ON l.trd_buy_id = a.id "
        "WHERE a.id = 17946359").fetchone()
    assert detail["Files"] and missing is False and float(price) == 3000
    # Это не чтение OWS — очередь подробностей не сдвинули.
    assert conn.execute("SELECT details_fetched_at = '2026-11-12 08:00+05' FROM announcements "
                        "WHERE id = 17946359").fetchone()[0]
    # Не нашли на платформе — следующие проходы берут другие, а не те же.
    conn.execute("UPDATE announcements SET plans_missing = true WHERE id = 17946359")
    conn.commit()
    platform.rows = []
    platform.calls.clear()
    res.run(conn, NOW + timedelta(minutes=2))
    assert platform.calls == []
