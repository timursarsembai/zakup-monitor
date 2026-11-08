import os

from cryptography.fernet import Fernet

from zakup import auth, db, details, research, secrets_box, spec


def _lot(conn):
    db.upsert_announcement(conn, {"id": 9, "name_ru": "Столы", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 9, "endDate": "2099-01-01 09:00:00", "Lots": [{
        "id": 90, "trdBuyId": 9, "lotNumber": "L-9", "nameRu": "Стол", "amount": 400000, "count": 4,
        "Plans": [{"price": 100000, "RefUnits": {"nameRu": "Штука"},
                   "PlansKato": [{"refKatoCode": "151010000",
                                  "fullDeliveryPlaceNameRu": "Актюбинская область, г.Актобе, ул. Абая 1"}]}],
    }]})
    conn.execute("INSERT INTO lot_specs (lot_id, file_url, text) VALUES (90, 'u', 'Стол дубовый 120х60')")
    conn.commit()
    return auth.set_password(conn, "owner@mail.kz", "long-enough-password")


def test_prompt_uses_lot_spec_and_region(conn):
    _lot(conn)
    context = research.lot_context(conn, 90)
    assert context["price"] == 100000 and context["unit"] == "штука"
    # Актобе — областной центр: город и область (потом весь Казахстан).
    assert research.default_region(context) == "г. Актобе, Актюбинская область"
    assert research.default_region({"kato": ["194430100"], "places": "Алматинская область, Кегенский район, с.Кеген"}) == "Алматинская область"
    assert research.default_region({"kato": ["751110000"], "places": "г.Алматы, Бостандыкский район"}) == "г. Алматы"
    prompt = research.build_prompt(context, "г.Актобе", True)
    assert "Стол дубовый 120х60" in prompt and "г.Актобе" in prompt and "по всему Казахстану" in prompt


def test_clean_drops_unsafe_links_and_sorts_region_first():
    result = research.clean({"summary": "s", "region_searched": "Актобе", "items": [
        {"name": "A", "url": "https://a.kz", "in_region": False, "unit_price": 50, "image": "javascript:x"},
        {"name": "B", "url": "javascript:alert(1)", "in_region": True, "unit_price": 10},
        {"name": "C", "url": "https://c.kz", "in_region": True, "unit_price": "90", "image": "https://c.kz/i.jpg"},
    ]})
    assert [i["name"] for i in result["items"]] == ["C", "A"]
    assert result["items"][0]["unit_price"] == 90 and result["items"][1]["image"] is None


def test_queue_runs_once_and_stores_result(conn, monkeypatch):
    user = _lot(conn)
    job = research.enqueue(conn, user, 90, "г.Актобе", True)
    assert research.enqueue(conn, user, 90, "г.Актобе", True) == job  # одна активная на лот
    monkeypatch.setattr(research, "_claude_code", lambda prompt, model: {
        "summary": "ok", "region_searched": "Актобе",
        "items": [{"name": "Стол", "url": "https://m.kz/1", "in_region": True, "unit_price": 80000}]})
    research.work(os.environ["TEST_DATABASE_URL"], ["claude_code"], once=True)
    latest = research.latest(conn, 90)
    assert latest["status"] == "done" and latest["result"]["items"][0]["unit_price"] == 80000


def test_api_keys_are_encrypted(conn, monkeypatch):
    monkeypatch.setenv("ZAKUP_SECRET_KEY", Fernet.generate_key().decode())
    user = _lot(conn)
    secrets_box.save_key(conn, user, "openai", " sk-test ")
    stored = conn.execute("SELECT key_enc FROM user_ai_keys").fetchone()[0]
    assert b"sk-test" not in bytes(stored)
    assert secrets_box.user_key(conn, user, "openai") == "sk-test"
    assert secrets_box.saved_providers(conn, user) == {"openai"}


def test_prefetch_stores_both_languages(conn, monkeypatch):
    _lot(conn)
    conn.execute("DELETE FROM lot_specs")
    conn.execute("UPDATE lots SET spec_url = 'https://ows.goszakup.gov.kz/download/1'")
    monkeypatch.setattr(spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(spec, "extract_both", lambda pdf: ("Стол дубовый", "Емен үстел"))
    assert spec.prefetch(conn, 10) == 1
    assert conn.execute("SELECT text, text_kk FROM lot_specs").fetchone() == ("Стол дубовый", "Емен үстел")
    assert spec.prefetch(conn, 10) == 0


def test_clean_keeps_only_phone_like_values():
    result = research.clean({"items": [
        {"name": "A", "url": "https://a.kz", "phone": "+7 (727) 123-45-67, 8 701 000 0000"},
        {"name": "B", "url": "https://b.kz", "phone": "звоните менеджеру"},
    ]})
    assert result["items"][0]["phone"] == "+7 (727) 123-45-67, 8 701 000 0000"
    assert result["items"][1]["phone"] is None


def test_sources_from_results_and_manual(conn):
    from zakup import sources
    user = _lot(conn)
    assert sources.domain_of("https://www.Zeta.kz/catalog?x=1") == "zeta.kz"
    assert sources.domain_of("Zeta.kz") == "zeta.kz" and sources.domain_of("zeta") is None
    sources.add(conn, user, "Zeta.kz", "Zeta", "мебель, оргтехника", "Алматы")
    sources.remember(conn, {"items": [{"url": "https://x.kz/1"}]})  # без категории
    sources.remember(conn, {"items": [{"url": "https://x.kz/2"}]})
    sources.remember(conn, {"category": "мебель", "items": [
        {"url": "https://www.mebel.kz/1", "seller": "Мебель.kz", "city": "Алматы"},
        {"url": "https://zeta.kz/2", "seller": "Zeta"},
    ]})
    rows = {r[1]: r for r in sources.listing(conn)}
    assert rows["zeta.kz"][6] is False and rows["zeta.kz"][8] == 1  # своя, нашлась раз
    assert rows["mebel.kz"][6] is True and rows["mebel.kz"][3] == ["Мебель"]
    prompt = sources.for_prompt(conn)
    assert "zeta.kz — Мебель, Оргтехника, Алматы (добавлен вручную)" in prompt
    assert sources.known_categories(conn) == ["Мебель", "Оргтехника"]


def test_find_more_extends_saved_result(conn, monkeypatch):
    user = _lot(conn)
    first = {"summary": "s", "region_searched": "Актобе", "category": "Мебель",
             "items": [{"name": "Стол 1", "url": "https://a.kz/1", "in_region": True, "unit_price": 90000}]}
    calls = []

    def fake(prompt, model):
        calls.append(prompt)
        return first if len(calls) == 1 else (
            {"items": [{"name": "Стол 1", "url": "https://a.kz/1", "unit_price": 90000},
                       {"name": "Стол 2", "url": "https://b.kz/2", "unit_price": 80000}]}
            if len(calls) == 2 else {"items": []})
    monkeypatch.setattr(research, "_claude_code", fake)
    url = os.environ["TEST_DATABASE_URL"]
    research.enqueue(conn, user, 90, "Актобе", True, target=15)
    research.work(url, ["claude_code"], once=True)
    assert "Найди 15 разных предложений" in calls[0]
    research.enqueue(conn, user, 90, "", True, target=5, more=True)
    research.work(url, ["claude_code"], once=True)
    assert "https://a.kz/1" in calls[1] and "НЕ возвращай" in calls[1]
    result = research.latest(conn, 90)["result"]
    # Прежнее — первым, новое — в конце и с пометкой.
    assert [i["url"] for i in result["items"]] == ["https://a.kz/1", "https://b.kz/2"]
    assert result["items"][1]["new"] and "new" not in result["items"][0]
    assert result["added"] == 1 and not result["exhausted"]
    research.enqueue(conn, user, 90, "", True, more=True)
    research.work(url, ["claude_code"], once=True)
    result = research.latest(conn, 90)["result"]
    assert result["exhausted"] and len(result["items"]) == 2


def test_spec_summary_queue_and_cache(conn, monkeypatch):
    _lot(conn)
    calls = []

    def fake(prompt, model, schema=None, web=True):
        calls.append((model, web, "Стол дубовый 120х60" in prompt))
        return {"item": "Стол дубовый", "params": [{"name": "Размер", "value": "120×60"}],
                "requirements": ["Гарантия 12 мес."]}
    monkeypatch.setattr(research, "_claude_code", fake)
    assert research.request_summary(conn, 90) == "queued"
    research.work(os.environ["TEST_DATABASE_URL"], ["claude_code"], once=True)
    assert calls == [("haiku", False, True)]
    summary = research.summary_of(conn, 90)
    assert summary["status"] == "done" and summary["result"]["params"][0]["value"] == "120×60"
    assert research.request_summary(conn, 90) == "done"  # готовая — повторно не ставится


def test_manual_search_links():
    from zakup import research

    links = dict(research.manual_links({"name": "Кровать", "extra": "двухъярусная"}, "г. Алматы"))
    assert links["Google — в регионе"].endswith("q=%D0%9A%D1%80%D0%BE%D0%B2%D0%B0%D1%82%D1%8C+"
                                               "%D0%B4%D0%B2%D1%83%D1%85%D1%8A%D1%8F%D1%80%D1%83%D1%81%D0%BD%D0%B0%D1%8F+"
                                               "%D0%BA%D1%83%D0%BF%D0%B8%D1%82%D1%8C+%D0%90%D0%BB%D0%BC%D0%B0%D1%82%D1%8B+"
                                               "%D1%86%D0%B5%D0%BD%D0%B0")
    assert links["Kaspi"].startswith("https://kaspi.kz/shop/search/?text=")
    assert research.manual_links(None, "") == []


def test_kaspi_search_parses_cards_and_reports_blocks():
    import httpx

    from zakup import kaspi

    def handler(request):
        assert request.url.params["text"] == "кровать двухъярусная"
        return httpx.Response(200, json={"data": [
            {"title": "Двухъярусная кровать Гранада", "unitPrice": 179500,
             "shopLink": "/p/dvuh-jarusnaja-krovat-113069628/?c=750000000",
             "previewImages": [{"medium": "https://resources.cdn-kaspi.kz/img/m.jpg"}]},
            {"title": "Без ссылки"}]})

    found = kaspi.search(" кровать   двухъярусная ", "http://u:p@h:1", transport=httpx.MockTransport(handler))
    assert found == [{"name": "Двухъярусная кровать Гранада", "price": 179500,
                      "url": "https://kaspi.kz/shop/p/dvuh-jarusnaja-krovat-113069628/",
                      "image": "https://resources.cdn-kaspi.kz/img/m.jpg"}]
    assert "Гранада — 179500 ₸" in kaspi.candidates_for_prompt(found)
    blocked = httpx.MockTransport(lambda request: httpx.Response(429))
    try:
        kaspi.search("кровать", "http://u:secret@h:1", transport=blocked)
    except kaspi.Blocked as error:
        assert "429" in str(error) and "secret" not in str(error)
    else:
        raise AssertionError("ожидали Blocked")
    assert kaspi.proxy_host("http://user:secret@81.1.2.3:12323") == "81.1.2.3:12323"
    assert kaspi.valid_proxy("http://u:p@h:1") and not kaspi.valid_proxy("h:1")
    assert kaspi.city_of(["711410000"]) == "710000000" and kaspi.city_of(["351010000"]) == kaspi.ALMATY


def test_kaspi_candidates_use_buyer_query(monkeypatch):
    from zakup import kaspi, research

    class Conn:
        def commit(self):
            pass

    asked, searched = [], []
    monkeypatch.setattr(kaspi, "proxy_url", lambda conn: "http://u:p@h:1")
    monkeypatch.setattr(research, "_light_json", lambda conn, prompt, schema: (
        asked.append(prompt) or {"queries": ["утюг паровой", "утюг"]}))
    monkeypatch.setattr(kaspi, "search", lambda query, proxy, city: searched.append(query) or [
        {"name": f"Утюг {query}", "price": 9990, "url": f"https://kaspi.kz/shop/p/{len(query)}/", "image": None}])
    context = {"name": "Электроутюг", "extra": "", "spec": "Мощность 2000 Вт", "kato": ["751110000"]}
    text = research._kaspi_candidates(Conn(), context, {"items": [{"url": "https://kaspi.kz/shop/p/5/"}]})
    assert "Мощность 2000 Вт" in asked[0] and searched == ["утюг паровой", "утюг"]
    assert "Утюг утюг паровой" in text and "/shop/p/5/" not in text  # уже найденное — не повторяем
    monkeypatch.setattr(kaspi, "proxy_url", lambda conn: None)
    assert research._kaspi_candidates(Conn(), context, None) == ""
