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
    assert research.default_region(context) == "Актюбинская область, г.Актобе"
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
