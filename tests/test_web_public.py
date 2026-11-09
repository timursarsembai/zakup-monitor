"""Открытые страницы: поиск, закупки, анализ рынка, документы."""



from tests.web_helpers import ORIGIN, _announcement_with_lots, _login
from zakup import db, details, results, spec, web


def test_announcement_page_shows_lots_and_loads_spec(client, conn, monkeypatch):
    db.upsert_announcement(conn, {"id": 77, "name_ru": "Столик", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {
        "id": 77, "numberAnno": "77-1", "nameRu": "Приобретение столика", "totalSum": 689655.16,
        "endDate": "2099-01-01 09:00:00", "disablePersonId": 1,
        "RefBuyStatus": {"nameRu": "Опубликовано (прием ценовых предложений)"},
        "Organizer": {"bin": "870240000026", "nameRu": "Школа №5",
                      "Address": [{"address": "г. Конаев, мкр 5"}]},
        "Files": [{"id": 1, "nameRu": "Проект договора", "filePath": "https://ows.goszakup.gov.kz/download/1"}],
        "Lots": [{"id": 5, "trdBuyId": 77, "lotNumber": "88448423-ЗЦП1", "nameRu": "Стол",
                  "amount": 689655.16, "count": 4,
                  "Files": [{"id": 2, "nameRu": "Техническая спецификация",
                             "filePath": "https://ows.goszakup.gov.kz/download/2"}],
                  "Plans": [{"prepayment": 30, "price": 172413.79, "RefUnits": {"nameRu": "Штука"},
                             "PlansKato": [{"refKatoCode": "191010000",
                                            "fullDeliveryPlaceNameRu": "г.Қонаев, мкр.5"}]}]}],
    })
    conn.commit()
    _login(client)
    page = client.get("/a/77")
    assert page.status_code == 200
    for text in ("Приобретение столика", "88448423-ЗЦП1", "аванс 30%", "г.Қонаев, мкр.5",
                 "Проект договора", "Техническая спецификация", "data-end="):
        assert text in page.text
    monkeypatch.setattr(spec, "get", lambda conn, lot_id, url: (None, "портал недоступен"))
    assert "Обновить" in client.get("/a/77/lots/5/spec").text
    monkeypatch.setattr(spec, "get", lambda conn, lot_id, url: ("Столик, дерево", None))
    fragment = client.get("/a/77/lots/5/spec?refresh=1")
    assert "Столик, дерево" in fragment.text
    assert client.get("/a/404404").status_code == 404


def test_finished_announcement_shows_winners_and_contracts(client, conn, monkeypatch):
    from tests.test_results import PROTOCOL, FakeClient, _announcement

    _announcement(conn)
    conn.commit()
    monkeypatch.setattr(results.spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(results, "protocol_text", lambda pdf: PROTOCOL)
    monkeypatch.setattr(web.core, "_goszakup", lambda: FakeClient())
    # Подробности свежие: перечитывать объявление не нужно, только итоги.
    conn.execute("UPDATE announcements SET details_fetched_at = now()")
    conn.commit()
    _login(client)
    # Итоги перечитываются в фоне после ответа: видны со следующего открытия.
    client.get("/a/7")
    page = client.get("/a/7").text
    for text in ("Победители (2)", "Договоры (1)", 'объединение &#34;ОКА&#34;', "37 950.00",
                 "C-1", "отсутствием представленных ценовых предложений"):
        assert text in page, text


def test_search_finds_finished_lots_with_winner(client, conn, monkeypatch):
    from tests.test_results import PROTOCOL, _announcement

    _announcement(conn)
    conn.execute("UPDATE announcements SET status_id = 350, disable_person = false")
    conn.execute("UPDATE lots SET status_id = 360 WHERE id = 70")
    monkeypatch.setattr(results.spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(results, "protocol_text", lambda pdf: PROTOCOL)
    detail = conn.execute("SELECT detail FROM announcements WHERE id = 7").fetchone()[0]
    results.parse_announcement(conn, 7, detail)
    conn.commit()
    _login(client)
    base = "/lots?applied=1&deadline_mode="
    assert "Весы" not in client.get(base + "&status=240").text
    page = client.get(base + "&stage=done").text
    assert "Весы" in page and "🏁" in page and "37 950.00" in page and "−15.7%" in page
    # Разобранный протокол второй раз не качается.
    monkeypatch.setattr(results.spec, "download", lambda url: 1 / 0)
    assert results.parse_pending(conn, 10) == 0


def test_announcements_page_lot_page_and_portal_fields(client, conn):
    _announcement_with_lots(conn)
    _login(client)
    base = "/announcements?applied=1&deadline_mode="
    page = client.get(base + "&q=канцтовары&number=500-1").text
    assert "Канцтовары для школы" in page and "Лотов: 2" in page and "Организатор: Школа" in page
    assert "Канцтовары для школы" not in client.get(base + "&q=мебель").text
    assert "Канцтовары для школы" not in client.get(base + "&min_sum=2000000").text
    lots = client.get("/lots?applied=1&deadline_mode=&exclude=ручка&year=2026").text
    assert "Бумага А4" in lots and "Ручка шариковая" not in lots
    assert "Бумага А4" not in client.get("/lots?applied=1&deadline_mode=&year=2025").text
    assert "Бумага А4" in client.get("/lots?applied=1&deadline_mode=&enstru=1712").text
    lot = client.get("/lots/5001").text
    assert "Бумага А4" in lot and "/a/500" in lot and "Другие лоты этого объявления (1)" in lot
    assert "/lots/5002" in lot
    workbook = client.get("/export.xlsx?applied=1&deadline_mode=")
    assert workbook.status_code == 200 and workbook.content[:2] == b"PK"
    assert client.get("/lots/999999").status_code == 404


def test_links_keep_default_filter(client):
    _login(client)
    page = client.get("/lots").text
    # Ссылка на поиск объявлений несёт значения по умолчанию, а не пустой фильтр.
    link = page.split('href="/announcements?', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    assert "status=240" in link and "advance_min=30" in link and "deadline_mode=rule" in link
    announcements = client.get("/announcements?" + link).text
    assert 'value="240" checked' in announcements and 'value="75" checked' in announcements


def test_logo_and_share_buttons(client, conn):
    assert client.get("/static/logo-mark.png").status_code == 200
    db.upsert_announcement(conn, {"id": 501, "name_ru": "Стулья", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 501, "nameRu": "Стулья", "endDate": "2099-01-01 09:00:00",
                        "Lots": [{"id": 5011, "trdBuyId": 501, "nameRu": "Стул", "amount": 1}]})
    conn.commit()
    _login(client)
    page = client.get("/a/501").text
    assert 'data-share="https://zakup.sarsembai.com/a/501"' in page
    assert 'data-share="https://goszakup.gov.kz/ru/announce/index/501"' in page
    assert 'data-share="https://zakup.sarsembai.com/lots/5011"' in client.get("/lots/5011").text


def test_announcements_advance_column_groups_values(client, conn):
    _announcement_with_lots(conn)
    conn.execute("UPDATE lots SET prepayment = 30")
    db.upsert_lot(conn, {"id": 5003, "trd_buy_id": 500, "name_ru": "Скрепки", "amount": 1})
    conn.execute("UPDATE lots SET prepayment = 100 WHERE id = 5003")
    conn.commit()
    _login(client)
    page = client.get("/announcements?applied=1&deadline_mode=").text
    cell = page.split('class="advances c-adv"', 1)[1].split("</td>", 1)[0]
    assert cell.count("30%") == 1 and cell.count("100%") == 1


def test_spec_shown_from_database_without_portal(client, conn, monkeypatch):
    _announcement_with_lots(conn)
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,Files}',
        '[{"id": 9, "nameRu": "Техническая спецификация", "filePath": "https://ows.goszakup.gov.kz/download/9"}]')""")
    conn.execute("INSERT INTO lot_specs (lot_id, file_url, text) VALUES (5001, 'https://ows.goszakup.gov.kz/download/9', 'Бумага офисная А4, 80 г/м2')")
    conn.commit()
    monkeypatch.setattr(details, "fetch_one", lambda *a: (_ for _ in ()).throw(AssertionError("portal")))
    _login(client)
    page = client.get("/lots/5001").text
    assert "Бумага офисная А4, 80 г/м2" in page and 'data-loaded="1"' in page


def test_pagination_and_per_page(client, conn):
    db.upsert_announcement(conn, {"id": 600, "name_ru": "Много лотов", "end_date": "2099-01-01 09:00:00"})
    for i in range(25):
        db.upsert_lot(conn, {"id": 6000 + i, "trd_buy_id": 600, "name_ru": f"Лот {i}", "amount": 1})
    conn.commit()
    _login(client)
    page = client.get("/lots?applied=1&deadline_mode=&per_page=20").text
    assert 'aria-current="page">1<' in page and "page=2" in page and "per_page=20" in page
    second = client.get("/lots?applied=1&deadline_mode=&per_page=20&page=2").text
    assert second.count('href="/lots/60') == 5
    assert 'value="100"' in page and "Показывать по" in page


def test_market_analytics_page(client, conn):
    _announcement_with_lots(conn)
    page = client.get("/analytics")  # без входа: аналитика открыта
    assert page.status_code == 200
    for text in ("Обзор рынка", "Новые закупки", "По дням недели", "Регионы", "Способы закупки",
                 "Самые дорогие открытые лоты", "Чаще всех побеждают", "Самые частые закупки"):
        assert text in page.text, text
    # Только общая аналитика, без личных блоков.
    for text in ("лотов под ваш фильтр", "Мои подписки", "Сроки по вашему фильтру"):
        assert text not in page.text, text
    # Главная — поиск объявлений; старые ссылки /?applied=1&… работают там же.
    assert client.get("/?applied=1", follow_redirects=False).status_code == 200


def test_kazakh_interface_and_data(client, conn):
    db.upsert_announcement(conn, {"id": 700, "name_ru": "Столы", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 700, "nameRu": "Приобретение столов", "nameKz": "Үстелдер сатып алу",
                        "endDate": "2099-01-01 09:00:00",
                        "RefBuyStatus": {"nameRu": "Опубликовано", "nameKz": "Жарияланды"},
                        "Lots": [{"id": 7001, "trdBuyId": 700, "nameRu": "Стол", "nameKz": "Үстел",
                                  "amount": 1, "count": 1,
                                  "Plans": [{"price": 1, "RefUnits": {"nameRu": "Штука", "nameKz": "Дана"}}]}]})
    conn.commit()
    _login(client)
    assert "Хабарландыру" not in client.get("/a/700").text
    switched = client.get("/lang/kk", headers={"referer": "https://testserver/a/700"}, follow_redirects=False)
    assert switched.headers["location"] == "/a/700" and "zakup_lang=kk" in switched.headers["set-cookie"]
    page = client.get("/a/700").text
    assert "Үстелдер сатып алу" in page and "Үстел" in page and "Жарияланды" in page
    assert "Лоттар (" in page and 'lang="kk"' in page and "дана" in page
    search = client.get("/lots").text
    assert "Лоттарды іздеу" in search and "Мәртебе" in search
    client.get("/lang/ru")
    assert "Поиск лотов" in client.get("/lots").text


def test_customer_contacts_on_lot_page(client, conn):
    from zakup import subjects

    _announcement_with_lots(conn)
    conn.execute("""UPDATE announcements SET detail = detail || '{"orgBin": "123456789012", "customerBin": "123456789012"}'
                    WHERE id = 500""")
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,customerBin}', '"123456789012"')
                    WHERE id = 500""")
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,descriptionRu}',
                    '"Вопросы по тел. 8 (701) 111-22-33 (снабжение)"') WHERE id = 500""")
    conn.commit()
    _login(client)
    page = client.get("/lots/5001").text
    assert "Контакты загружаются" in page and 'href="tel:+77011112233"' in page and "снабжение" in page
    subjects.save(conn, "123456789012", {
        "nameRu": "Школа №1", "phone": "+77273382995", "email": "school@mail.kz", "website": "school.kz",
        "Address": [{"address": "г.Алматы, ул. Абая, 1", "phone": "87017573388"},
                    {"address": "г.Алматы,  ул. Абая, 1", "phone": ""}]})
    page = client.get("/lots/5001").text
    assert 'href="tel:+77273382995"' in page and 'href="https://wa.me/77017573388"' in page
    assert "wa.me/77273382995" not in page  # городской номер — без WhatsApp
    assert "mailto:school@mail.kz" in page and 'href="https://school.kz"' in page
    assert page.count("ул. Абая, 1") == 1
    announcement = client.get("/a/500").text
    assert "mailto:school@mail.kz" in announcement and "Найти официальный сайт" not in announcement


def test_guest_sees_public_pages_and_paywall(client, conn):
    _announcement_with_lots(conn)
    conn.execute("INSERT INTO spec_summaries (lot_id, provider, model, status, result) VALUES "
                 "(5001, 'claude_code', 'haiku', 'done', '{\"item\": \"Секретная выжимка\", \"params\": [], \"requirements\": []}')")
    conn.commit()
    web.app.dependency_overrides.clear()
    page = client.get("/lots/5001")
    assert page.status_code == 200 and "Бумага А4" in page.text and 'id="paywall"' in page.text
    assert "Секретная выжимка" not in page.text  # платное содержимое гостю не показываем
    assert 'data-paid' in page.text and "Войти" in page.text
    assert client.get("/announcements").status_code == 200
    assert client.post("/lots/5001/mark", data={"status": "interested"}, headers=ORIGIN,
                       follow_redirects=False).headers["location"].startswith("/login")
    assert "9 900 ₸" in client.get("/pricing").text
    assert "Sitemap:" in client.get("/robots.txt").text
    assert "/sitemap-a-0.xml" in client.get("/sitemap.xml").text
    assert "/a/500" in client.get("/sitemap-a-0.xml").text and "/lots/5001" in client.get("/sitemap-l-0.xml").text


def test_legal_documents_use_requisites(client, conn):
    from zakup import legal

    legal._cache = None  # кэш реквизитов — между тестами сбрасываем
    page = client.get("/offer").text
    assert "Публичная оферта" in page and "статьёй 395" in page and "не заполнено" in page
    assert "/privacy" in client.get("/").text  # ссылки внизу каждой страницы
    _login(client)
    client.post("/settings/requisites", data={"name": "ИП Тестов", "bin": "900101300123",
                                              "email": "info@tenderim.kz"}, headers=ORIGIN)
    page = client.get("/offer").text
    assert "ИП Тестов" in page and "info@tenderim.kz" in page
    assert "ИП Тестов" in client.get("/privacy").text and "900101300123" in client.get("/contacts").text
    client.get("/lang/kk", headers={"referer": "https://testserver/offer"})
    assert "Жария оферта" in client.get("/offer").text


def test_spec_tables_rendered_as_table():
    blocks = web.core._spec_blocks("Заголовок\nНаименование | Кол-во\nСтол | 5\nИтого")
    assert blocks == [("text", "Заголовок"), ("table", [["Наименование", "Кол-во"], ["Стол", "5"]]),
                      ("text", "Итого")]


def test_sitemap_skips_lots_removed_on_portal(client, conn):
    _announcement_with_lots(conn)
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,1,isDeleted}', '1')
                    WHERE id = 500""")
    conn.commit()
    lots = client.get("/sitemap-l-0.xml").text
    assert "/lots/5001" in lots and "/lots/5002" not in lots
    assert client.get("/lots/5002").status_code == 404


def test_spec_loaded_later_shows_ready_summary(client, conn, monkeypatch):
    from zakup import results

    _announcement_with_lots(conn)
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,Files}',
                    '[{"nameRu": "Техническая спецификация", "filePath": "https://ows.goszakup.gov.kz/x"}]')""")
    conn.execute("INSERT INTO lot_specs (lot_id, file_url, text) VALUES (5001, 'https://ows.goszakup.gov.kz/x', 'Бумага А4')")
    conn.execute("""INSERT INTO spec_summaries (lot_id, provider, model, status, result)
                    VALUES (5001, 'claude_code', 'm', 'done', '{"item": "Бумага офисная"}')""")
    conn.commit()
    monkeypatch.setattr(results.spec, "download", lambda url: 1 / 0)
    _login(client)
    page = client.get("/a/500/lots/5001/spec").text
    assert "Бумага офисная" in page and "data-summary-start" not in page


def test_seo_tags_kazakh_url_and_structured_data(client, conn):
    import json
    _announcement_with_lots(conn)
    page = client.get("/lots/5001").text
    assert "<title>Бумага А4 — лот" in page and "· Tenderim</title>" in page
    assert '<link rel="canonical" href="https://zakup.sarsembai.com/lots/5001">' in page
    assert 'hreflang="kk" href="https://zakup.sarsembai.com/lots/5001?lang=kk"' in page
    assert 'property="og:image" content="https://zakup.sarsembai.com/og/lot/5001.png"' in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    assert "Госзакупка: Бумага А4" in page.split('name="description"', 1)[1].split(">", 1)[0]
    blocks = [json.loads(chunk.split("</script>", 1)[0])
              for chunk in page.split('<script type="application/ld+json">')[1:]]
    demand = next(item for block in blocks for item in block if item["@type"] == "Demand")
    assert demand["url"].endswith("/lots/5001") and demand["priceSpecification"]["priceCurrency"] == "KZT"
    assert any(item["@type"] == "BreadcrumbList" for block in blocks for item in block)
    # Казахская версия — по своему адресу, без cookie (как у поисковика), и запоминается.
    kk = client.get("/lots/5001?lang=kk")
    assert 'lang="kk"' in kk.text and '<link rel="canonical" href="https://zakup.sarsembai.com/lots/5001?lang=kk">' in kk.text
    assert "zakup_lang=kk" in kk.headers["set-cookie"]
    assert 'content="https://zakup.sarsembai.com/og/lot/5001.png?lang=kk"' in kk.text
    robots = client.get("/robots.txt").text
    assert "Allow: /*?lang=kk$" in robots and "Disallow: /*?" in robots
    home = client.get("/").text
    assert '"@type": "WebSite"' in home or '"@type":"WebSite"' in home


def test_preview_images_and_llms_txt(client, conn):
    _announcement_with_lots(conn)
    for path in ("/og/site.png", "/og/lot/5001.png", "/og/a/500.png", "/og/lot/5001.png?lang=kk"):
        image = client.get(path)
        assert image.status_code == 200 and image.content[:8] == b"\x89PNG\r\n\x1a\n", path
        assert image.headers["content-type"] == "image/png"
    assert client.get("/og/lot/999999.png").status_code == 404
    llms = client.get("/llms.txt")
    assert llms.status_code == 200 and "# Tenderim" in llms.text and "https://zakup.sarsembai.com/lots" in llms.text


def _org_data(conn):
    customer, supplier, person = "990740001111", "050540002222", "651114400549"
    db.upsert_announcement(conn, {"id": 900, "number_anno": "900-1", "name_ru": "Ремонт крыши",
                                  "total_sum": 2000000, "customer_bin": customer,
                                  "customer_name_ru": "ГУ Школа №9", "org_bin": customer,
                                  "org_name_ru": "ГУ Школа №9", "end_date": "2026-11-01 09:00:00"})
    details.save(conn, {"id": 900, "nameRu": "Ремонт крыши", "endDate": "2026-11-01 09:00:00",
                        "customerBin": customer, "customerNameRu": "ГУ Школа №9",
                        "Lots": [{"id": 9001, "trdBuyId": 900, "lotNumber": "R-1", "nameRu": "Ремонт кровли",
                                  "amount": 2000000, "count": 1, "customerBin": customer,
                                  "customerNameRu": "ГУ Школа №9"}]})
    conn.execute("UPDATE lots SET customer_bin = %s WHERE id = 9001", (customer,))
    conn.execute(
        """INSERT INTO lot_results (lot_id, outcome, winner_bin, winner_name, winner_total, offers)
           VALUES (9001, 'winner', %s, 'ТОО Кровля', 1800000, %s)""",
        (supplier, db.Jsonb([{"bin": supplier, "name": "ТОО Кровля", "total": "1800000"},
                             {"bin": person, "name": "ИП Иванов", "total": "1900000"}])))
    conn.execute("INSERT INTO subjects (bin, name_ru, phone, email, website, addresses, fetched_at) "
                 "VALUES (%s, 'ГУ Школа №9', '+7 727 111 22 33', 'school9@mail.kz', '', '[]', now())", (customer,))
    conn.commit()
    return customer, supplier, person


def test_organization_pages_for_customer_and_supplier(client, conn):
    customer, supplier, person = _org_data(conn)
    page = client.get(f"/org/{customer}").text
    assert "<h1>ГУ Школа №9</h1>" in page and "Объявил закупок" in page and "school9@mail.kz" in page
    assert f'href="/org/{supplier}">ТОО Кровля</a>' in page and "10.0%" in page
    assert '"taxID": "990740001111"' in page and "/a/900" in page
    seller = client.get(f"/org/{supplier}").text
    assert "Побед в закупках" in seller and "/lots/9001" in seller and f'href="/org/{customer}"' in seller
    # У ИП (по ИИН) — тоже страница: участие в закупках; неизвестный БИН — 404.
    assert "ИП Иванов" in client.get(f"/org/{person}").text
    assert client.get("/org/990740009999").status_code == 404
    assert client.get("/org/abc").status_code == 404
    lot = client.get("/lots/9001").text
    assert f'href="/org/{customer}"' in lot and f'href="/org/{person}">ИП Иванов</a>' in lot
    from zakup import orgs
    orgs.refresh_directory(conn)
    index = client.get("/sitemap.xml").text
    assert "sitemap-o-0.xml" in index
    part = client.get("/sitemap-o-0.xml").text
    assert f"/org/{customer}" in part and f"/org/{supplier}" in part


def test_organization_directory_filters(client, conn):
    customer, supplier, person = _org_data(conn)
    from zakup import orgs
    orgs.refresh_directory(conn)
    page = client.get("/orgs").text
    assert "ГУ Школа №9" in page and "ТОО Кровля" in page and "ИП Иванов" in page and "Найдено: 3" in page
    sellers = client.get("/orgs?role=seller").text
    assert "ТОО Кровля" in sellers and "ИП Иванов" in sellers and "ГУ Школа №9" not in sellers
    assert "ГУ Школа №9" in client.get("/orgs?role=buyer").text
    only_ip = client.get("/orgs?form=ip").text
    assert "ИП Иванов" in only_ip and "ТОО Кровля" not in only_ip
    assert "ТОО Кровля" in client.get("/orgs?form=too").text
    assert "ГУ Школа №9" in client.get("/orgs?form=gu").text
    assert "ТОО Кровля" in client.get("/orgs?q=кровля").text and "Найдено: 1" in client.get("/orgs?q=0505").text
    assert "Найдено: 0" in client.get("/orgs?q=%25_").text
    assert client.get("/orgs?sort=evil;drop&region=zz&form=x&page=-5").status_code == 200
    assert 'href="/orgs"' in client.get("/").text  # ссылка в меню


def test_category_and_region_pages(client, conn):
    _announcement_with_lots(conn)  # «Бумага А4» — ЕНС ТРУ 171211…, регион не указан
    conn.execute("UPDATE lots SET kato = '{751410000}' WHERE id = 5001")
    conn.commit()
    index = client.get("/categories").text
    assert "Бумага и изделия из бумаги" in index and 'href="/category/17"' in index
    category = client.get("/category/17").text
    assert "Госзакупки: Бумага и изделия из бумаги" in category and "/lots/5001" in category
    assert 'href="/category/17/75"' in category  # Алматы
    both = client.get("/category/17/75").text
    assert "Бумага и изделия из бумаги — г. Алматы" in both and "/lots/5001" in both
    region = client.get("/region/75").text
    assert "Госзакупки: г. Алматы" in region and "/lots/5001" in region
    assert client.get("/category/99").status_code == 404 and client.get("/region/00").status_code == 404
    assert "Өңірлер бойынша" in client.get("/regions?lang=kk").text
    static = client.get("/sitemap-static.xml").text
    assert "/category/17/75" in static and "/orgs" in static


def _tender(conn, ident, *, code, kato, end, winner=None, offers=()):
    db.upsert_announcement(conn, {"id": ident, "number_anno": f"{ident}-1", "name_ru": f"Мебель {ident}",
                                  "total_sum": 1000000, "customer_bin": "990740001111",
                                  "customer_name_ru": "ГУ Школа №9", "end_date": end})
    details.save(conn, {"id": ident, "nameRu": f"Мебель {ident}", "endDate": end, "Lots": [
        {"id": ident * 10, "trdBuyId": ident, "lotNumber": f"M-{ident}", "nameRu": "Диван", "amount": 1000000,
         "count": 1, "plnPointKatoList": [kato], "Plans": [{"refEnstruCode": code}]}]})
    if winner:
        conn.execute(
            """INSERT INTO lot_results (lot_id, outcome, winner_bin, winner_name, winner_total, offers)
               VALUES (%s, 'winner', %s, %s, 900000, %s)""",
            (ident * 10, winner[0], winner[1],
             db.Jsonb([{"bin": b, "name": n, "total": "900000"} for b, n in [winner, *offers]])))


def test_lot_page_shows_competitors_and_suppliers(client, conn):
    from zakup import orgs
    sofa, chair = "310912.100.000001", "310912.300.000002"
    _tender(conn, 801, code=sofa, kato="551010000", end="2099-01-01 09:00:00")  # открытый лот, Павлодар
    _tender(conn, 802, code=sofa, kato="551010000", end="2026-10-30 09:00:00",
            winner=("050540002222", "ТОО Кровля"), offers=[("651114400549", "ИП Иванов")])
    _tender(conn, 803, code=chair, kato="751410000", end="2026-10-29 09:00:00",
            winner=("060640003333", "ТОО Мебель-Юг"))
    conn.commit()
    orgs.refresh_directory(conn)
    page = client.get("/lots/8010").text
    assert "Конкуренты и поставщики" in page and "В регионе поставки: Павлодарская область" in page
    here, rest = page.split("В регионе поставки:", 1)[1].split("По всему Казахстану", 1)
    assert 'href="/org/050540002222"' in here and 'href="/org/651114400549"' in here
    assert 'href="/org/060640003333"' in rest and "г. Алматы" in rest
    # без тарифа — без процентов
    assert "Снижение цены" not in page and "Побед в регионе" in page
    # Точный уровень — только диваны.
    exact = client.get("/lots/8010?sup=code").text.split("Конкуренты и поставщики", 1)[1]
    assert "060640003333" not in exact.split("</section>", 1)[0]
    _login(client)
    assert ">10.0%<" in client.get("/lots/8010").text
    # У ИП — ИИН, у ТОО — БИН.
    assert "ИИН 651114400549" in client.get("/org/651114400549").text
    assert "БИН 050540002222" in client.get("/org/050540002222").text


def test_directory_forms_follow_role_and_mobile_more_sheet(client, conn):
    from zakup import orgs
    _org_data(conn)
    orgs.refresh_directory(conn)
    buyers = client.get("/orgs?role=buyer").text
    assert 'value="ip"' not in buyers and 'value="gu"' in buyers
    assert 'value="ip"' in client.get("/orgs?role=seller").text
    assert "Найдено: 1" in client.get("/orgs?role=buyer&form=ip").text  # чужая форма сбрасывается
    home = client.get("/").text
    assert "data-more-sheet" in home and 'href="/orgs" class="more-tile' in home and "nav-wide" in home


def test_portal_card_phone_without_prefix_and_individual_form(conn):
    from zakup import subjects
    subjects.save(conn, "060312501523", {"nameRu": "ИП ҚАСЫНБЕК", "refKopfCode": "ИНД",
                                         "phone": "+7 (701) 175-21-91, 7075060312", "email": "a@mail.ru",
                                         "Address": [{"addressType": 1, "address": "г.Шу, дом 62", "katoCode": "314630100"}]})
    card = subjects.card(conn, "060312501523")
    assert [p["tel"] for p in card["phones"]] == ["+77011752191", "+77075060312"]
    assert card["phones"][1]["wa"] and card["kopf"] == "ИП" and card["id_label"] == "ИИН"
    assert card["addresses"] == ["г.Шу, дом 62"]


def test_switch_to_russian_from_kazakh_url(client):
    switched = client.get("/lang/ru", headers={"referer": "https://testserver/regions?lang=kk&x=1"},
                          follow_redirects=False)
    assert switched.headers["location"] == "/regions?x=1"


def test_unpaid_see_only_five_suppliers_in_html(client, conn):
    from zakup import orgs
    bidders = [(f"0605400{n:05d}", f"ТОО Участник {n}") for n in range(8)]
    _tender(conn, 811, code="310912.100.000001", kato="551010000", end="2099-01-01 09:00:00")
    _tender(conn, 812, code="310912.100.000001", kato="551010000", end="2026-10-30 09:00:00",
            winner=bidders[0], offers=bidders[1:])
    conn.commit()
    orgs.refresh_directory(conn)
    block = client.get("/lots/8110").text.split('id="suppliers"', 1)[1].split("</section>", 1)[0]
    assert block.count('class="s-name"') == 5 and block.count("ТОО Участник") == 5 and "Показать всех (8)" in block


def test_lot_level_customer_has_page_and_description_without_amount(client, conn):
    from zakup import orgs
    db.upsert_announcement(conn, {"id": 820, "name_ru": "Совместная закупка", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 820, "nameRu": "Совместная закупка", "endDate": "2099-01-01 09:00:00",
                        "Lots": [{"id": 8201, "trdBuyId": 820, "lotNumber": "S-1", "nameRu": "Мел",
                                  "customerBin": "990740007777", "customerNameRu": "ГУ Школа №7"}]})
    conn.execute("UPDATE lots SET customer_bin = '990740007777', customer_name_ru = 'ГУ Школа №7' WHERE id = 8201")
    conn.commit()
    orgs.refresh_directory(conn)
    page = client.get("/org/990740007777")
    assert page.status_code == 200 and "ГУ Школа №7" in page.text and "/a/820" in page.text
    description = client.get("/lots/8201").text.split('name="description" content="', 1)[1].split('"', 1)[0]
    assert ". ," not in description and description.count(". .") == 0 and "приём заявок до" in description


def test_category_filter_in_search_and_subscriptions(client, conn):
    _announcement_with_lots(conn)  # «Бумага А4» — 171211…, «Ручка» без кода
    _login(client)
    lots = client.get("/lots?applied=1&deadline_mode=&category=17").text
    assert "Бумага А4" in lots and "Ручка шариковая" not in lots
    assert "Бумага А4" not in client.get("/lots?applied=1&deadline_mode=&category=31").text
    assert "Канцтовары для школы" in client.get("/announcements?applied=1&deadline_mode=&category=17").text
    assert "Канцтовары для школы" not in client.get("/announcements?applied=1&deadline_mode=&category=31").text
    assert 'name="category" value="17"' in lots
    from zakup.filters import Filters
    assert Filters.from_json(Filters(categories=["17"]).to_json()).categories == ["17"]
