"""Резерв госзакупок с платформы zakup.gov.kz (zakup.reserve): номера — номера OWS."""

from datetime import UTC, datetime, timedelta

import pytest

from zakup import db, details, reserve, spec
from zakup.skk import PlatformError

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


FAIL = object()  # general-files не отдаётся
SPEC_URL = "https://v3bl.goszakup.gov.kz/files/download_file/319178621/229284353/"


def _groups(lot_ext):
    """Ответ general-files: техспецификация лота и общий проект договора."""
    return [{"id": 1, "group": {"name": "Проект договора об электронных закупках", "egz_code": "101"},
             "files": [{"id": 193026109, "lot": None,
                        "file": {"file_url": "https://v3bl.goszakup.gov.kz/files/download_file/1/2/",
                                 "filename": "contract_project_1.pdf", "external_id": "1"}}]},
            {"id": 2, "group": {"name": "Техническая спецификация", "egz_code": "125"},
             "files": [{"id": 193026110, "lot": {"external_id": lot_ext, "lot_number": "88569522-ЗЦП1"},
                        "file": {"file_url": SPEC_URL, "filename": f"techspec_1_{lot_ext}.pdf"}},
                       {"id": 193026111, "lot": None, "file": {"file_url": "javascript:alert(1)"}}]},
            {"id": 3, "group": {"name": "Обеспечение заявки"}, "files": None}]


class Platform:
    def __init__(self, rows, cards=None, lots=None, files=None):
        self.rows, self.cards, self.lots, self.calls = rows, cards or {}, lots or {}, []
        self.files = files or {}

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/announcements/":
            if "announcement_number" in params:
                # Без limit платформа отдаёт голый список, без {"results": …}.
                return [r for r in self.rows if r["announcement_number"] == params["announcement_number"]
                        and r["system_id"] == params["system_id"]]
            rows = [r for r in self.rows if r["system_id"] == params["system_id"]]
            return {"results": rows[params["offset"]:params["offset"] + params["limit"]], "next": None}
        pid = int(path.split("/")[2])
        if path.endswith("/general-files/"):
            if self.files.get(pid) is FAIL:
                raise PlatformError("HTTP 502")
            return self.files.get(pid, [])
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
    row = conn.execute("SELECT system, status_id, trade_method_id, disable_person, lots_ready, details_fetched_at, "
                       "platform_id FROM announcements WHERE id = 17946359").fetchone()
    # подробности OWS — на ближайшем круге; номер платформы — для ссылки «На портале»
    assert row == (1, 240, 3, True, True, None, 41074043)
    lot = conn.execute("SELECT trd_buy_id, price, unit_name, kato, customer_bin FROM lots WHERE id = 43878609").fetchone()
    assert lot[0] == 17946359 and float(lot[1]) == 3000 and lot[2] == "Штука" and lot[3] == ["194020100"]
    assert lot[4] == ORG
    # Второй проход: уже с лотами — ни карточек, ни лотов (документов не нашлось — их спросим снова).
    platform.calls.clear()
    assert reserve.Reserve(platform).run(conn, NOW)["saved"] == 0
    assert all(path == "/announcements/" or path.endswith("/general-files/") for path, _ in platform.calls)

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
    assert res.run(conn, NOW) == {"listed": 1, "saved": 0, "backlog": 0, "plans": 0, "links": 0, "specs": 0, "failed": 0}
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


def test_links_backfill_open_announcements(conn):
    db.upsert_announcement(conn, {"id": 17950444, "number_anno": "17950444-1", "name_ru": "Товары",
                                  "end_date": "2099-01-01 09:00:00"})
    db.upsert_announcement(conn, {"id": 600, "number_anno": "600-1", "name_ru": "Нет на платформе",
                                  "end_date": "2099-01-01 09:00:00"})
    conn.commit()
    platform = Platform([_row(41083105, 17950444, published=NOW - timedelta(days=5))])
    res = reserve.Reserve(platform)
    res._fresh = lambda conn, now: []
    assert res.run(conn, NOW)["links"] == 1
    assert conn.execute("SELECT platform_id FROM announcements WHERE id = 17950444").fetchone()[0] == 41083105
    # Не нашли — до RETRY не ищем снова.
    platform.calls.clear()
    res.run(conn, NOW + timedelta(minutes=2))
    assert platform.calls == []


def test_files_mapped_like_ows():
    common, by_lot = reserve.files(_groups(43878609))
    assert [f["nameRu"] for f in common] == ["Проект договора об электронных закупках"]
    assert by_lot[43878609][0]["filePath"] == SPEC_URL and by_lot[43878609][0]["id"] == "p193026110"
    detail = reserve.to_detail(_card(41074043, 17946359), _lots(43878609), NOW, groups=_groups(43878609))
    assert spec.spec_file(detail["Lots"][0]) == SPEC_URL and detail["platformFiles"]
    # Не получили документы — без отметки: доберёт _files.
    assert "platformFiles" not in reserve.to_detail(_card(41074043, 17946359), _lots(43878609), NOW)


def test_spec_download_only_https_to_portal_hosts():
    for url in ("http://v3bl.goszakup.gov.kz/x", "https://evil.example/x", "https://v3bl.goszakup.gov.kz.evil/x"):
        with pytest.raises(spec.SpecError):
            spec.download(url)


def test_fresh_announcement_gets_spec_url(conn):
    platform = Platform([_row(41074043, 17946359)], cards={41074043: _card(41074043, 17946359)},
                        lots={41074043: _lots(43878609)}, files={41074043: _groups(43878609)})
    res = reserve.Reserve(platform)
    assert res.run(conn, NOW)["saved"] == 1
    assert conn.execute("SELECT spec_url FROM lots WHERE id = 43878609").fetchone()[0] == SPEC_URL
    # Документы уже есть — добор их не спрашивает.
    platform.calls.clear()
    res.run(conn, NOW + timedelta(minutes=2))
    assert not any(path.endswith("/general-files/") for path, _ in platform.calls)


def test_files_backfill_saved_without_documents(conn):
    platform = Platform([_row(41074043, 17946359)], cards={41074043: _card(41074043, 17946359)},
                        lots={41074043: _lots(43878609)}, files={41074043: FAIL})
    res = reserve.Reserve(platform)
    stats = res.run(conn, NOW)
    # Документы не отдались — объявление всё равно сохранили; добор сразу не повторяет.
    assert stats["saved"] == 1 and stats["specs"] == 0 and stats["failed"] == 0
    assert sum(path.endswith("/general-files/") for path, _ in platform.calls) == 1
    assert conn.execute("SELECT spec_url FROM lots WHERE id = 43878609").fetchone()[0] is None
    # До RETRY не повторяем.
    platform.calls.clear()
    res.run(conn, NOW + timedelta(minutes=2))
    assert not any(path.endswith("/general-files/") for path, _ in platform.calls)
    # Потом отдались: техспецификация на месте, очередь OWS не сдвинули.
    platform.files[41074043] = _groups(43878609)
    conn.execute("UPDATE announcements SET details_fetched_at = '2026-11-12 08:00+05', details_update_date = NULL "
                 "WHERE id = 17946359")
    conn.commit()
    assert res.run(conn, NOW + reserve.RETRY + timedelta(minutes=1))["specs"] == 1
    detail, same, update_date = conn.execute(
        "SELECT detail, details_fetched_at = '2026-11-12 08:00+05', details_update_date FROM announcements "
        "WHERE id = 17946359").fetchone()
    # Пустая дата обновления осталась пустой: OWS не затрёт пункты плана платформы.
    assert same and update_date is None and detail["platformFiles"] and detail["Files"][0]["originalName"] == "contract_project_1.pdf"
    assert conn.execute("SELECT spec_url FROM lots WHERE id = 43878609").fetchone()[0] == SPEC_URL


def test_fill_files_keeps_ows_detail(conn):
    db.upsert_announcement(conn, {"id": 17946359, "number_anno": "17946359-1", "name_ru": "Подушки",
                                  "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 17946359, "nameKz": "x", "Files": [{"originalName": "ТС.pdf"}],
                        "Lots": [{"id": 43878609, "trdBuyId": 17946359, "nameRu": "Подушка", "Plans": []}]})
    assert reserve.fill_files(conn, 17946359, _groups(43878609)) == 0
    detail = conn.execute("SELECT detail FROM announcements WHERE id = 17946359").fetchone()[0]
    assert detail["Files"] == [{"originalName": "ТС.pdf"}]


def test_files_odd_entries_and_single_lot_spec():
    groups = [{"group": {"name": "Техническая спецификация"},
               "files": [{"lot": None, "file": {"file_url": SPEC_URL}},
                         {"lot": {"external_id": "abc"}, "file": {"file_url": SPEC_URL + "2/"}},
                         "мусор", {"file": None}]},
              {"group": None, "files": [{"file": {"file_url": SPEC_URL}}]}]
    common, by_lot = reserve.files(groups)
    # Странные строки не роняют разбор; без номеров файлов — номер по ссылке, не «pNone».
    assert not by_lot and {f["id"] for f in common} == {f"p{SPEC_URL}", f"p{SPEC_URL}2/"}
    # Один лот, спецификация без привязки — она его.
    detail = reserve.to_detail(_card(41074043, 17946359), _lots(43878609), NOW, groups=groups)
    assert spec.spec_file(detail["Lots"][0]) == SPEC_URL and detail["Files"] == []
    # Пустой ответ (бывает и при сбое) — без отметки, добор спросит снова.
    assert "platformFiles" not in reserve.to_detail(_card(41074043, 17946359), _lots(43878609), NOW, groups=[])


def test_backlog_fills_lots_of_ows_announcements_without_them(conn):
    """Список лотов OWS застыл (12–13.11.2026): объявления есть, лотов нет — добираем с
    платформы по номеру, даже когда верх списка платформы уже весь у нас."""
    published = (NOW - timedelta(hours=5)).isoformat()
    for aid in (17950444, 601):
        db.upsert_announcement(conn, {"id": aid, "number_anno": f"{aid}-1", "name_ru": "Товары",
                                      "publish_date": published, "end_date": "2099-01-01 09:00:00"})
    conn.commit()
    platform = Platform([_row(41083105, 17950444, system=1, published=NOW - timedelta(hours=5))],
                        cards={41083105: _card(41083105, 17950444)}, lots={41083105: _lots(43900001)})
    res = reserve.Reserve(platform)
    res._fresh = lambda conn, now: []
    stats = res.run(conn, NOW)
    assert stats["backlog"] == 1 and stats["failed"] == 0
    row = conn.execute("SELECT lots_ready, platform_id, name_ru FROM announcements WHERE id = 17950444").fetchone()
    assert row == (True, 41083105, "Товары")  # строку OWS не переписали, лоты — с платформы
    assert conn.execute("SELECT trd_buy_id FROM lots WHERE id = 43900001").fetchone() == (17950444,)
    # 601 на платформе нет — отметка в базе, сутки не ищем (и после перезапуска); с лотами — не берём.
    assert conn.execute("SELECT platform_checked_at IS NOT NULL FROM announcements WHERE id = 601").fetchone()[0]
    platform.calls.clear()
    assert reserve.Reserve(platform).run(conn, NOW + timedelta(minutes=2))["backlog"] == 0
    assert not any(params.get("announcement_number") for _, params in platform.calls)
    # Через час ищем снова: платформа могла ожить.
    conn.execute("UPDATE announcements SET platform_checked_at = now() - interval '61 minutes' WHERE id = 601")
    conn.commit()
    platform.calls.clear()
    reserve.Reserve(platform).run(conn, NOW + timedelta(minutes=2))
    assert any(params.get("announcement_number") == "601-1" for _, params in platform.calls)


def test_backlog_keeps_ows_detail_and_adds_platform_lots(conn):
    """Подробности OWS уже есть (документы, тип торгов), лотов в них нет — дописываем только лоты."""
    published = (NOW - timedelta(hours=5)).isoformat()
    db.upsert_announcement(conn, {"id": 17950444, "number_anno": "17950444-1", "name_ru": "Товары",
                                  "publish_date": published, "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 17950444, "nameKz": "x", "refTypeTradeId": 1, "lastUpdateDate": "2026-11-13 10:00:00",
                        "Files": [{"originalName": "Документация.pdf", "filePath": "https://ows.goszakup.gov.kz/f/1"}],
                        "Lots": []})
    conn.commit()
    platform = Platform([_row(41083105, 17950444, system=1, published=NOW - timedelta(hours=5))],
                        cards={41083105: _card(41083105, 17950444)}, lots={41083105: _lots(43900001)})
    res = reserve.Reserve(platform)
    res._fresh = lambda conn, now: []
    assert res.run(conn, NOW)["backlog"] == 1
    detail, ready, trade = conn.execute(
        "SELECT detail, lots_ready, type_trade_id FROM announcements WHERE id = 17950444").fetchone()
    assert ready and detail["Files"][0]["originalName"] == "Документация.pdf" and trade == 1
    assert detail["Lots"][0]["id"] == 43900001 and detail["source"] == "platform"
    assert detail["finYear"] == [2026]  # у OWS без лотов годов нет — из пунктов плана платформы
    # OWS всё ещё без лотов, но с новым документом (протокол): документ берём, лоты платформы остаются.
    details.save(conn, {"id": 17950444, "nameKz": "x", "refTypeTradeId": 1,
                        "Files": [{"originalName": "Протокол.pdf", "filePath": "https://ows.goszakup.gov.kz/f/2"}],
                        "Lots": []})
    detail, ready = conn.execute("SELECT detail, lots_ready FROM announcements WHERE id = 17950444").fetchone()
    assert ready and detail["Lots"][0]["id"] == 43900001 and detail["Files"][0]["originalName"] == "Протокол.pdf"
