"""Сборщик Самрук-Казыны (zakup.skk): платформа zakup.gov.kz → те же таблицы, system = 3."""

import json
from datetime import UTC, datetime, timedelta

from zakup import details, legal_entities, results, skk
from zakup.filters import Filters

NOW = datetime(2026, 11, 12, 9, tzinfo=UTC)
CUSTOMER = "161040005807"  # на платформе без названия — берём из реестра юрлиц
OTHER = "990340000123"


def _item(pid=41079505, status=6, published="2026-11-11T10:31:28.957000+00:00",
          end="2026-11-18T00:00:00+00:00"):
    return {"id": pid, "system_id": 3, "status_id": status, "status_name": "Опубликовано",
            "purchase_method_id": 12, "purchase_method_name": "Запрос ценовых предложений",
            "search_name": {"name_ru": "Прочие цеховые материалы", "name_kk": "Басқа цехтық материалдар"},
            "name": "Прочие цеховые материалы", "announcement_number": str(pid)[-7:],
            "organizer_name": None, "total_price": 223100.0, "lot_count": 1,
            "publish_date": published, "offer_start_date": "2026-11-12T00:00:00+00:00",
            "offer_end_date": end, "results_publication_date": None}


def _card(pid=41079505):
    org = {"id": 62000261, "iin_bin": CUSTOMER, "name": None}
    return {"id": pid, "name_kk": "Басқа цехтық материалдар", "timestamp": 1794429030,
            "status": {"id": 6, "name": "Опубликовано"}, "year": {"id": 12, "year": 2026},
            "purchase_method": {"id": 12, "name_kk": "Бағалы ұсыныстарға сұраным"},
            "organizer": org, "customer": org}


def _lots(lot_id=61854403, bin_=OTHER):
    return [{"id": lot_id, "lot_number": "4581093", "name": "Манометр технический", "description": "технический",
             "is_externally_deleted": False, "total_price": 223100.0,
             "status": {"id": 6, "name": "Опубликован"},
             "plan_items": [{"count": 10, "unit_price": 22310.0, "total_price": 223100.0, "prepayment": None,
                             "description_ru": "технический", "description_kk": "техникалық",
                             "extra_description_ru": "техникалық",
                             "organization": {"iin_bin": bin_, "name_ru": None},
                             "enstru": {"code": "265152.700.000019"},
                             "measure": {"name": "Штука"},
                             "purchase_subject": {"code": "G"},
                             "delivery_address": {"code": "615639300",
                                                  "full_name_ru": "Туркестанская область, с.Сарыжаз"}}]}]


class Platform:
    def __init__(self, items):
        self.items, self.calls = items, []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/announcements/":
            assert params["system_id"] == 3
            return {"count": len(self.items), "next": None, "results": self.items}
        pid = int(path.split("/")[2])
        if path.endswith("/lots/"):
            return _lots(lot_id=pid + 20_000_000)
        return _card(pid)


def _registry(monkeypatch):
    asked = []

    def fetch(http, bins):
        asked.append(sorted(bins))
        return {CUSTOMER: {"bin": CUSTOMER, "nameru": "ТОО «Казатомпром-SaUran»", "datereg": "2016-10-04"},
                OTHER: {"bin": OTHER, "nameru": "АО «Заказчик лота»", "datereg": "1999-03-01"}}
    monkeypatch.setattr(legal_entities, "fetch", fetch)
    return asked


def test_to_detail_maps_platform_to_ows_shape():
    detail = skk.to_detail(_item(), _card(), _lots(), {CUSTOMER: "ТОО «Казатомпром-SaUran»"})
    assert detail["id"] == skk.ID_OFFSET + 41079505 and detail["platformId"] == 41079505
    assert detail["refBuyStatusId"] == 210 and detail["refTradeMethodsId"] == 10012
    assert detail["customerNameRu"] == "ТОО «Казатомпром-SaUran»"
    assert detail["refSubjectTypeId"] == 1 and detail["finYear"] == [2026]
    lot = detail["Lots"][0]
    assert lot["id"] == skk.ID_OFFSET + 61854403 and lot["refLotStatusId"] == 240
    plan = lot["Plans"][0]
    # Русское описание — description_ru (в extra_description_ru у платформы бывает казахский).
    assert plan["extraDescRu"] == "технический" and plan["PlansKato"][0]["refKatoCode"] == "615639300"
    fields, lots = details.parse(detail)
    assert fields["kato"] == ["615639300"] and lots[0]["price"] == 22310.0


def test_run_saves_with_names_and_refetches_only_changes(conn, monkeypatch):
    asked = _registry(monkeypatch)
    platform = Platform([_item()])
    assert skk.run(conn, platform, NOW)["saved"] == 1
    aid = skk.ID_OFFSET + 41079505
    row = conn.execute("SELECT system, customer_name_ru, lots_ready, trade_method_id, status_id "
                       "FROM announcements WHERE id = %s", (aid,)).fetchone()
    assert row == (3, "ТОО «Казатомпром-SaUran»", True, 10012, 210)
    lot = conn.execute("SELECT system, customer_bin, customer_name_ru, price, kato FROM lots "
                       "WHERE trd_buy_id = %s", (aid,)).fetchone()
    assert lot[:3] == (3, OTHER, "АО «Заказчик лота»") and float(lot[3]) == 22310.0 and lot[4] == ["615639300"]
    assert asked == [sorted([OTHER, CUSTOMER])]
    # Список не изменился — ни карточек, ни реестра.
    platform.calls.clear()
    assert skk.run(conn, platform, NOW)["changed"] == 0
    assert platform.calls == ["/announcements/"] and len(asked) == 1
    # Статус сменился — перечитываем; названия свежие — реестр не спрашиваем.
    platform.items = [_item(status=10)]
    assert skk.run(conn, platform, NOW)["saved"] == 1
    assert conn.execute("SELECT status_id FROM announcements WHERE id = %s", (aid,)).fetchone()[0] == 250
    assert len(asked) == 1


def test_filter_by_system_and_ows_services_skip_skk(conn, monkeypatch):
    _registry(monkeypatch)
    skk.run(conn, Platform([_item()]), NOW)
    conn.execute("INSERT INTO announcements (id, name_ru, end_date, lots_ready, raw) VALUES (7, 'Госзакупка', %s, true, '{}')",
                 (NOW + timedelta(days=3),))
    conn.commit()

    def ids(system):
        where, params = Filters(statuses=[], stages=[], disable_person="", regions=[], min_sum=None,
                                deadline="", end_from="", advance_min=0, system=system).where(NOW, "announcements")
        return sorted(r[0] for r in conn.execute(f"SELECT a.id FROM announcements a WHERE {where}", params))
    aid = skk.ID_OFFSET + 41079505
    assert ids("skk") == [aid] and ids("goszakup") == [7] and ids("") == [7, aid]
    form = Filters.from_form({"applied": ["1"], "system": ["skk"]})
    assert form.system == "skk" and "system=skk" in form.to_query() and "Самрук-Казына" in form.describe()
    assert Filters.from_json(json.loads(json.dumps(Filters().to_json()))).system == ""

    class NoOws:
        def graphql(self, *args):
            raise AssertionError("OWS must not be asked about SKK")
    assert details.fetch_one(conn, NoOws(), aid) is False
    results.refresh(conn, NoOws(), aid)


def test_backlog_is_seen_new_announcements_notify(conn, monkeypatch):
    _registry(monkeypatch)
    user = conn.execute("INSERT INTO users (email) VALUES ('s@mail.kz') RETURNING id").fetchone()[0]
    sub = conn.execute(
        "INSERT INTO subscriptions (user_id, name, query, kind, filters, scope) "
        "VALUES (%s, 'все', '', 'announcements', %s, 'lots') "
        "RETURNING id",
        (user, json.dumps(Filters.public().to_json()))).fetchone()[0]
    conn.commit()
    # Первый проход: опубликованное до запуска — сразу «увиденное».
    skk.run(conn, Platform([_item()]), NOW)
    later = _item(pid=41079600, published=(NOW + timedelta(hours=1)).isoformat())
    skk.run(conn, Platform([_item(), later]), NOW + timedelta(hours=2))
    from zakup import matcher
    matcher.run_filters(conn, NOW + timedelta(hours=2))
    rows = dict(conn.execute(
        "SELECT l.trd_buy_id - %s, m.notified_at IS NOT NULL FROM matches m JOIN lots l ON l.id = m.lot_id "
        "WHERE m.subscription_id = %s", (skk.ID_OFFSET, sub)).fetchall())
    assert rows == {41079505: True, 41079600: False}


def test_skk_announcement_page(client, conn, monkeypatch):
    _registry(monkeypatch)
    skk.run(conn, Platform([_item()]), NOW)
    aid = skk.ID_OFFSET + 41079505
    conn.execute("UPDATE announcements SET end_date = now() + interval '3 days'")
    conn.commit()
    page = client.get(f"/a/{aid}")
    assert page.status_code == 200
    assert "Самрук-Казына" in page.text and "https://zakup.gov.kz/announcement/41079505" in page.text
    assert "old.goszakup.gov.kz" not in page.text
    assert conn.execute("SELECT platform_id FROM announcements WHERE id = %s", (aid,)).fetchone()[0] == 41079505
    lot = conn.execute("SELECT id FROM lots WHERE trd_buy_id = %s", (aid,)).fetchone()[0]
    assert "Самрук-Казына" in client.get(f"/lots/{lot}").text
    listing = client.get("/announcements?applied=1&system=skk")
    assert "Прочие цеховые материалы" in listing.text
    # Подробностей ещё нет — не идём в OWS, просто «загружаются».
    conn.execute("UPDATE announcements SET detail = NULL")
    conn.commit()
    assert client.get(f"/a/{aid}").status_code == 404


def test_unknown_status_bad_item_and_old_reviews(conn, monkeypatch):
    _registry(monkeypatch)
    # Незнакомый статус платформы — по сроку, а не пустой (иначе выпадет из поиска и подписок).
    detail = skk.to_detail(_item(status=999, end="2099-01-01T00:00:00+00:00"), _card(), _lots())
    assert detail["refBuyStatusId"] == 210 and detail["Lots"][0]["refLotStatusId"] == 240

    class Broken(Platform):
        def get(self, path, params=None):
            data = super().get(path, params)
            if path == "/announcements/41079600/lots/":
                return [{"no": "id"}]  # битый лот — KeyError при разборе
            return data
    bad = _item(pid=41079600)
    stats = skk.run(conn, Broken([bad, _item()]), NOW)
    assert stats["saved"] == 1 and stats["failed"] == 1
    # Давнее «на рассмотрении» (конец приёма раньше окна) дочитываем по номеру объявления.
    aid = skk.ID_OFFSET + 41079505
    conn.execute("UPDATE announcements SET status_id = 250, end_date = %s WHERE id = %s",
                 (NOW - timedelta(days=60), aid))
    conn.commit()

    class Old(Platform):
        def get(self, path, params=None):
            if path == "/announcements/" and "announcement_number" in params:
                self.calls.append(("number", params["announcement_number"]))
                return [_item(status=16, end="2026-09-01T00:00:00+00:00")]  # без limit — список
            if path == "/announcements/":
                self.calls.append(path)
                return {"results": [], "next": None}
            return super().get(path, params)
    old = Old([])
    skk.run(conn, old, NOW)
    assert ("number", _item()["announcement_number"]) in old.calls
    assert conn.execute("SELECT status_id FROM announcements WHERE id = %s", (aid,)).fetchone()[0] == 350


def test_skk_badge_in_lists(client, conn, monkeypatch):
    _registry(monkeypatch)
    skk.run(conn, Platform([_item()]), NOW)
    conn.execute("UPDATE announcements SET end_date = now() + interval '3 days'")
    conn.commit()
    page = client.get("/lots?applied=1&system=skk")
    assert "Манометр технический" in page.text and 'class="badge">Самрук-Казына' in page.text
