from zakup import contacts


def test_phones_from_page_code_and_text():
    html = ('<a href="tel:+77073037063">позвонить</a>'
            '<script>{"phone":"+7 (707) 303-70-63","phones":[]}</script>'
            '<p>Офис: 8 727 250-10-10, факс 2501010</p><a href="https://wa.me/77011234567">wa</a>')
    assert contacts.phones_in(html) == ["+7 (707) 303-70-63", "+7 (701) 123-45-67", "+7 (727) 250-10-10"]
    assert contacts.phones_in("<p>ИНН 123456789012, счёт 77001234567890</p>") == []


def test_no_fetch_to_private_addresses():
    assert contacts.fetch_phones("http://127.0.0.1:8096/") == []
    assert contacts.fetch_phones("http://localhost/") == []
    assert contacts.fetch_phones("file:///etc/passwd") == []


ZETA = """<h3>Интернет-магазин</h3><p>По вопросам заказов</p>
<p>+7 (777) 313-13-64<br>(WhatsApp)</p><p>+7 (727) 313-13-71 (Отдел контроля)</p>
<h3>Call-центр</h3><p>+7 (727) 313-13-51 (многоканальный)</p>"""


def test_labeled_phones_from_contacts_page():
    assert contacts.labeled_phones(ZETA) == [
        {"number": "+7 (777) 313-13-64", "label": "По вопросам заказов · WhatsApp", "whatsapp": True},
        {"number": "+7 (727) 313-13-71", "label": "По вопросам заказов · Отдел контроля", "whatsapp": False},
        {"number": "+7 (727) 313-13-51", "label": "Call-центр · многоканальный", "whatsapp": False},
    ]


def test_enrich_uses_and_fills_contact_base(conn, monkeypatch):
    pages, sites = [], []
    monkeypatch.setattr(contacts, "fetch_phones", lambda url: pages.append(url) or ["+7 (707) 303-70-63"])
    monkeypatch.setattr(contacts, "site_contacts", lambda url: sites.append(url) or [
        {"number": "+7 (727) 313-13-51", "label": "Call-центр", "whatsapp": False}])
    result = {"items": [{"url": "https://zeta.kz/a", "phone": "87073037063"},
                        {"url": "https://www.zeta.kz/b"},
                        {"url": "https://satu.kz/p1", "seller": "ТОО «Мебель»"},
                        {"url": "https://satu.kz/p2", "seller": "ИП Другой"}]}
    assert contacts.enrich(result, conn) == 4
    first, second, third, fourth = result["items"]
    assert first["phones"] == [
        {"number": "+7 (707) 303-70-63", "label": "страница товара", "whatsapp": False},
        {"number": "+7 (727) 313-13-51", "label": "Call-центр", "whatsapp": False}]
    assert first["phone"] == "+7 (707) 303-70-63, +7 (727) 313-13-51"
    assert second["phones"] == first["phones"]  # тот же сайт — из базы, страницы не открываем
    assert sites == ["https://zeta.kz/a"]  # «Контакты» маркетплейса — не продавец
    assert pages == ["https://zeta.kz/a", "https://satu.kz/p1", "https://satu.kz/p2"]
    # Следующий поиск: всё уже в базе — ни одной страницы.
    again = {"items": [{"url": "https://satu.kz/p9", "seller": "тоо мебель"}, {"url": "https://zeta.kz/c"}]}
    pages.clear()
    assert contacts.enrich(again, conn) == 2 and pages == []
    assert again["items"][0]["phones"][0]["number"] == "+7 (707) 303-70-63"
    assert [name for name, _, _ in contacts.of_domain(conn, "satu.kz")] == ["ИП Другой", "ТОО «Мебель»"]
