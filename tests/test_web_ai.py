"""ИИ: подбор товаров, источники, выжимки, вопросы поставщику."""



from tests.web_helpers import ORIGIN, _announcement_with_lots, _login
from zakup import auth, db, details, research


def test_lot_page_research_section_and_start(client, conn, monkeypatch):
    from tests.test_research import _lot

    _lot(conn)
    _login(client)
    page = client.get("/lots/90").text
    assert "Поиск товаров с ИИ" in page and "Актюбинская область, г.Актобе" in page
    response = client.post("/lots/90/research", data={"region": "г.Актобе", "whole_kz": "1"},
                           headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert "В очереди" in client.get("/lots/90/research").text
    # Пока поиск идёт — кнопка неактивна и с надписью «Ищем…», а не «Найти заново».
    page = client.get("/lots/90").text
    form = page.split('class="research-form"')[1].split("</form>")[0]
    assert "disabled" in form and "Ищем…" in form
    conn.execute("""UPDATE lot_research SET status = 'done', finished_at = now(),
        result = '{"summary": "s", "region_searched": "Актобе", "items": [{"name": "Стол дуб",
        "seller": "Мебель", "city": "Актобе", "in_region": true, "price": 80000, "unit_price": 80000,
        "url": "https://m.kz/1", "image": null, "fit": "полностью"}]}'""")
    conn.commit()
    fragment = client.get("/lots/90/research").text
    assert "Стол дуб" in fragment and "20 000.00 ₸" in fragment and "20.0%" in fragment
    assert "80 000.00" in fragment  # маржа на лот: 20 000 × 4
    monkeypatch.setenv("ZAKUP_SECRET_KEY", "")
    bad = client.post("/settings/ai", data={"provider": "openai"}, headers=ORIGIN)
    assert bad.status_code == 422


def test_research_tab_hidden_without_spec(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("DELETE FROM lot_specs WHERE lot_id = 90")
    conn.commit()
    # Файл есть, но ещё не разобран — поиск есть (ориентируется на описание).
    detail = conn.execute("SELECT detail FROM announcements WHERE id = 9").fetchone()[0]
    detail["Lots"][0]["Files"] = [{"nameRu": "Техническая спецификация", "filePath": "https://ows.goszakup.gov.kz/f"}]
    assert research.lot_context(conn, 90)["has_spec"] is False
    details.save(conn, detail)
    conn.commit()
    assert research.lot_context(conn, 90)["has_spec"] is True
    # Файла нет вовсе — искать не по чему.
    details.save(conn, {**detail, "Lots": [{**detail["Lots"][0], "Files": []}]})
    conn.commit()
    _login(client)
    page = client.get("/lots/90").text
    # Без техспецификации искать не по чему: ни вкладки, ни запуска.
    assert 'data-tab="research"' not in page and 'id="research"' not in page
    response = client.post("/lots/90/research", data={"region": "г.Актобе", "whole_kz": "1"},
                           headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 409
    assert conn.execute("SELECT count(*) FROM lot_research").fetchone()[0] == 0
    # Уже сделанный поиск остаётся виден, но «Найти ещё» без спецификации нет.
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'anthropic', 'Актобе', 'done', now(), '{"summary": "s", "items": [{"name": "Стол дуб",
        "seller": "М", "price": 1, "unit_price": 1, "url": "https://m.kz/1", "fit": "полностью"}]}'
        FROM users LIMIT 1""")
    conn.commit()
    page = client.get("/lots/90").text
    assert 'data-tab="research"' in page and "искать товары не по чему" in page and "Найти ещё" not in page


def test_research_list_page(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'claude_code', 'г.Актобе', 'done', now(),
        '{"summary": "s", "items": [{"name": "Стол", "url": "https://m.kz/1", "in_region": true,
          "unit_price": 80000, "fit": "полностью"}]}' FROM users LIMIT 1""")
    conn.commit()
    client.post("/login", data={"email": "owner@mail.kz", "password": "long-enough-password"},
                headers=ORIGIN)
    page = client.get("/research").text
    assert "Подбор товаров" in page and "/lots/90#research" in page and "маржа до 20.0%" in page
    assert "/lots/90" not in client.get("/research?q=кровать").text


def test_sources_sheet_on_research_page(client, conn):
    _login(client)
    response = client.post("/sources", data={"url": "https://zeta.kz", "name": "Zeta",
                                             "categories": "Мебель"},
                           headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/research?sources_open=1#sources"
    page = client.get("/research?sources_open=1").text
    assert "Источники для подбора" in page and "zeta.kz" in page and 'class="sheet open"' in page
    assert client.post("/sources", data={"url": "нет"}, headers=ORIGIN).status_code == 422


def test_spec_summary_shown_in_lot_card(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    _login(client)
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,Files}',
        '[{"id": 9, "nameRu": "Техническая спецификация", "filePath": "https://ows.goszakup.gov.kz/download/9"}]')""")
    conn.execute("UPDATE lot_specs SET file_url = 'https://ows.goszakup.gov.kz/download/9'")
    conn.commit()
    page = client.get("/lots/90").text
    assert "Кратко: главное в таблице" in page
    assert client.post("/lots/90/spec-summary", headers=ORIGIN).json() == {"status": "queued"}
    conn.execute("""UPDATE spec_summaries SET status = 'done', result =
        '{"item": "Стол", "params": [{"name": "Материал", "value": "дуб"}], "requirements": [],
          "kk": {"item": "Үстел", "params": [{"name": "Материалы", "value": "емен"}], "requirements": []}}'""")
    conn.commit()
    page = client.get("/lots/90").text
    assert "Материал" in page and "дуб" in page and "емен" not in page
    client.get("/lang/kk")  # на казахском — казахская версия выжимки
    page = client.get("/lots/90").text
    assert "Материалы" in page and "емен" in page


def test_research_list_margin_falls_back_to_partial_fit(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'claude_code', 'Актобе', 'done', now(),
        '{"items": [{"name": "Стол", "url": "https://m.kz/1", "unit_price": 50000, "fit": "частично"}]}'
        FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    page = client.get("/research").text
    assert "маржа до 50.0%" in page and "по частично подходящим" in page


def test_old_results_stay_visible_while_searching_more(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'claude_code', 'Актобе', 'done', now(),
        '{"items": [{"name": "Стол дубовый", "url": "https://m.kz/1", "unit_price": 50000, "fit": "полностью", "in_region": true}]}'
        FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    client.post("/lots/90/research", data={"more": "1", "target": "5"}, headers=ORIGIN)
    fragment = client.get("/lots/90/research").text
    assert "Ищу ещё предложения" in fragment and "Стол дубовый" in fragment
    assert "Найти ещё" not in fragment  # пока идёт поиск — кнопки нет
    page = client.get("/research").text
    assert "Сейчас ищем" in page and "ещё предложения" in page


def test_supplier_questions_on_demand_and_shared(client, conn, monkeypatch):
    from zakup import questions, research

    _announcement_with_lots(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 5001, id, 'claude_code', 'г.Алматы', 'done', now(),
        '{"summary": "s", "items": [{"name": "Бумага Svetocopy", "url": "https://m.kz/p1", "in_region": true,
          "matches": ["формат А4"], "mismatches": ["плотность не указана"]}]}' FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    page = client.get("/lots/5001").text
    assert "Что спросить у поставщика" in page and 'data-questions="https://m.kz/p1"' in page
    assert conn.execute("SELECT count(*) FROM supplier_questions").fetchone()[0] == 0  # сам не запускается
    assert client.post("/lots/5001/questions", data={"url": "https://evil.kz/x"},
                       headers=ORIGIN).status_code == 404
    assert client.post("/lots/5001/questions", data={"url": "https://m.kz/p1"},
                       headers=ORIGIN).json() == {"status": "queued"}
    assert 'data-q-status="queued"' in client.get("/lots/5001/questions?url=https://m.kz/p1").text
    prompts = []
    monkeypatch.setattr(research.providers, "_claude_code", lambda prompt, model, schema=None, web=True: (
        prompts.append(prompt) or {"confirm": ["Формат точно А4?"], "clarify": ["Плотность 80 г/м²?"],
                                   "terms": ["Цена на сайте актуальна?"]}))
    assert questions.work_one(conn, ["claude_code"]) is True
    assert "плотность не указана" in prompts[0] and "Бумага Svetocopy" in prompts[0]
    page = client.get("/lots/5001/questions?url=https://m.kz/p1").text
    assert "Плотность 80 г/м²?" in page and "Цена на сайте актуальна?" in page
    # Повторное нажатие (в т.ч. другим пользователем) не запускает ИИ заново.
    assert client.post("/lots/5001/questions", data={"url": "https://m.kz/p1"},
                       headers=ORIGIN).json() == {"status": "done"}
    assert questions.work_one(conn, ["claude_code"]) is False


def test_source_contacts_dialog(client, conn):
    from zakup import contacts

    conn.execute("INSERT INTO sources (domain, name, auto) VALUES ('satu.kz', 'Satu', true)")
    contacts._store(conn, "satu.kz", "тоо мебель", "ТОО «Мебель»",
                    [{"number": "+7 (707) 303-70-63", "label": "страница товара", "whatsapp": False}])
    conn.commit()
    _login(client)
    page = client.get("/research").text
    assert 'data-contacts="/sources/' in page and 'class="count">1<' in page
    source = conn.execute("SELECT id FROM sources WHERE domain = 'satu.kz'").fetchone()[0]
    fragment = client.get(f"/sources/{source}/contacts").text
    assert "ТОО «Мебель»" in fragment and 'href="tel:+77073037063"' in fragment
    assert 'href="https://wa.me/77073037063"' in fragment
    assert client.get("/sources/999999/contacts").status_code == 404


def test_research_hides_offers_without_margin(client, conn):
    _announcement_with_lots(conn)  # лот 5001: 10 000 ₸ за единицу
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 5001, id, 'claude_code', 'г.Алматы', 'done', now(),
        '{"summary": "s", "items": [
          {"name": "Дешёвая бумага", "url": "https://m.kz/1", "in_region": true, "unit_price": 8000},
          {"name": "Дорогая бумага", "url": "https://m.kz/2", "in_region": true, "unit_price": 12000}]}'
        FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    page = client.get("/lots/5001").text
    assert "Дешёвая бумага" in page and "Дорогая бумага" not in page


def test_each_client_sees_own_research_staff_sees_all_merged(client, conn, monkeypatch):
    import json as _json

    from tests.test_research import _lot

    _lot(conn)
    owner = auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    a = auth.set_password(conn, "client-a", "client-password-1")
    b = auth.set_password(conn, "client-b", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', paid_until = now() + interval '30 days' WHERE id IN (%s, %s)", (a, b))
    conn.execute("UPDATE users SET role = 'admin' WHERE id = %s", (owner,))
    for uid, urls in ((a, ["https://shop.kz/a", "https://shop.kz/same"]), (b, ["https://shop.kz/b", "https://shop.kz/same/"])):
        items = [{"name": u, "url": u, "unit_price": 50000, "in_region": True, "fit": "полностью"} for u in urls]
        conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
                        VALUES (90, %s, 'claude_code', 'Актобе', 'done', now(), %s)""", (uid, _json.dumps({"items": items})))
    conn.commit()
    client.post("/login", data={"email": "client-a", "password": "client-password-1"}, headers=ORIGIN)
    page = client.get("/lots/90/research").text
    assert "shop.kz/a" in page and "shop.kz/b" not in page
    research_list = client.get("/research").text
    assert "/lots/90#research" in research_list
    client.post("/logout", headers=ORIGIN)
    _login(client)
    page = client.get("/lots/90/research").text
    assert "shop.kz/a" in page and "shop.kz/b" in page and page.count('href="https://shop.kz/same') == 2  # фото и название — один товар
    assert "Все поиски по лоту вместе: 2" in page


def test_research_fragment_shows_offers_found_so_far(client, conn):
    from psycopg.types.json import Jsonb

    from zakup import details

    db.upsert_announcement(conn, {"id": 9, "name_ru": "Столы", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 9, "endDate": "2099-01-01 09:00:00", "Lots": [{"id": 90, "trdBuyId": 9, "nameRu": "Стол", "Plans": []}]})
    owner = conn.execute("SELECT id FROM users WHERE email = 'owner@mail.kz'").fetchone()[0]
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, started_at, result)
                    VALUES (90, %s, 'claude_code', 'Актобе', 'running', now(), %s)""",
                 (owner, Jsonb({"partial": True, "step": "Открываю mebel.kz", "items": [
                     {"name": "Стол дубовый", "url": "https://mebel.kz/1", "seller": "Мебель", "price": 90000}]})))
    conn.commit()
    _login(client)
    page = client.get("/lots/90/research").text
    assert "Уже нашёл:" in page and "Стол дубовый" in page and "Открываю mebel.kz…" in page


def test_research_output_in_kazakh_when_interface_is_kazakh(client, conn):
    from psycopg.types.json import Jsonb

    from zakup import details

    db.upsert_announcement(conn, {"id": 9, "name_ru": "Столы", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 9, "endDate": "2099-01-01 09:00:00", "Lots": [{"id": 90, "trdBuyId": 9, "nameRu": "Стол", "Plans": []}]})
    owner = conn.execute("SELECT id FROM users WHERE email = 'owner@mail.kz'").fetchone()[0]
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
                    VALUES (90, %s, 'claude_code', 'Актобе', 'done', now(), %s)""",
                 (owner, Jsonb({"summary": "Столы есть в Актобе", "summary_kk": "Үстелдер Ақтөбеде бар", "items": [
                     {"name": "Стол", "url": "https://mebel.kz/1", "in_region": True, "unit_price": 1000,
                      "note": "В наличии", "note_kk": "Қоймада бар", "matches": ["дуб"], "matches_kk": ["емен"]}]})))
    conn.commit()
    _login(client)
    page = client.get("/lots/90/research").text
    assert "Столы есть в Актобе" in page and "В наличии" in page and "✓ дуб" in page
    client.get("/lang/kk")
    page = client.get("/lots/90/research").text
    assert "Үстелдер Ақтөбеде бар" in page and "Қоймада бар" in page and "✓ емен" in page


def test_tech_proposal_fragment_and_word_download(client, conn):
    from psycopg.types.json import Jsonb

    from zakup import details

    db.upsert_announcement(conn, {"id": 9, "name_ru": "Столы", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 9, "endDate": "2099-01-01 09:00:00", "Lots": [{"id": 90, "trdBuyId": 9, "nameRu": "Стол", "Plans": []}]})
    owner = conn.execute("SELECT id FROM users WHERE email = 'owner@mail.kz'").fetchone()[0]
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
                    VALUES (90, %s, 'claude_code', 'Актобе', 'done', now(), %s)""",
                 (owner, Jsonb({"items": [{"name": "Стол", "url": "https://mebel.kz/1", "in_region": True}]})))
    conn.execute("""INSERT INTO tech_proposals (lot_id, url, provider, status, result) VALUES (90, 'https://mebel.kz/1',
                    'claude_code', 'done', %s)""",
                 (Jsonb({"title": "Стол Дуб", "rows": [{"requirement": "Дуб", "offer": "Массив дуба", "ok": True}],
                         "confirm": ["Сертификат"]}),))
    conn.commit()
    assert client.get("/lots/90/proposal?url=https://mebel.kz/1", follow_redirects=False).status_code != 200
    _login(client)
    page = client.get("/lots/90/proposal?url=https://mebel.kz/1").text
    assert "Массив дуба" in page and "соответствует" in page and "proposal.doc" in page
    doc = client.get("/lots/90/proposal.doc?url=https://mebel.kz/1")
    assert doc.status_code == 200 and doc.headers["content-type"].startswith("application/msword")
    assert "Массив дуба" in doc.text and "Техническое предложение" in doc.text
