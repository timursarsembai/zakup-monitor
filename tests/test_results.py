from zakup import auth, db, details, marks, results

PROTOCOL = """Протокол об итогах 1-ЗЦП1
Лот №
100-ЗЦП1
Наименование лота
Весы
Потенциальными поставщиками представлены следующие ценовые
предложения: 2
№
Наименование
поставщика
Дата и время подачи заявки (по
хронологии)
1
Товарищество с ограниченной ответственностью "Сервисно -
производственное объединение "ОКА"
961040001455
37950
2550
37950
06.11.2026 11:58:46
2
ТОО "АлАйКо"
171040031283
38300
2200
38300
05.11.2026 21:19:25
1. Определить победителем по лоту № 100-ЗЦП1 : 961040001455 Товарищество с ограниченной
ответственностью "Сервисно - производственное объединение "ОКА" , потенциальным
поставщиком, занявшим второе место 171040031283 ТОО "АлАйКо"
2. Заказчику заключить договор о государственных закупках с 961040001455 .
Лот №
101-ЗЦП1
Потенциальными поставщиками представлены следующие ценовые
предложения: 0
Признать государственную закупку Весы по лоту № 101-ЗЦП1 несостоявшейся в связи с
отсутствием представленных ценовых предложений
Примечание: *Одно из следующих значений
"""


def test_parse_protocol_finds_offers_winner_and_failures():
    parsed = results.parse_protocol(PROTOCOL)
    won = parsed["100-ЗЦП1"]
    assert won["outcome"] == "winner"
    assert won["winner_bin"] == "961040001455"
    assert won["winner_name"].endswith('объединение "ОКА"')
    assert won["second_bin"] == "171040031283" and won["second_name"] == 'ТОО "АлАйКо"'
    assert [o["bin"] for o in won["offers"]] == ["961040001455", "171040031283"]
    assert won["offers"][0]["name"].startswith("Товарищество") and won["offers"][1]["total"] == "38300"
    assert parsed["101-ЗЦП1"] == {
        "offers": [], "outcome": "failed", "reason": "отсутствием представленных ценовых предложений"
    }


class FakeClient:
    def graphql(self, query):
        if "Contract" in query:
            return {"Contract": [{"id": 1, "contractNumberSys": "C-1", "contractSum": 37950,
                                  "Supplier": {"nameRu": "ОКА"}, "ContractUnits": [{"lotId": 70}]}],
                    "ComplaintAppeal": []}
        raise AssertionError(query)


def _announcement(conn):
    db.upsert_announcement(conn, {"id": 7, "name_ru": "Весы", "end_date": "2020-01-01 10:00:00"})
    details.save(conn, {
        "id": 7, "endDate": "2020-01-01 10:00:00",
        "Files": [{"id": 1, "nameRu": "Протокол итогов",
                   "filePath": "https://ows.goszakup.gov.kz/download/p"}],
        "Lots": [{"id": 70, "trdBuyId": 7, "lotNumber": "100-ЗЦП1", "nameRu": "Весы",
                  "amount": 45000},
                 {"id": 71, "trdBuyId": 7, "lotNumber": "101-ЗЦП1", "nameRu": "Весы 2",
                  "amount": 45000}],
    })


def test_refresh_saves_results_and_marks_get_outcome(conn, monkeypatch):
    _announcement(conn)
    monkeypatch.setattr(results.spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(results, "protocol_text", lambda pdf: PROTOCOL)
    results.refresh(conn, FakeClient(), 7)
    found = results.by_lot(conn, [70, 71])
    assert found[70]["winner_total"] == 37950 and found[71]["outcome"] == "failed"
    assert results.contracts_by_lot(
        conn.execute("SELECT results FROM announcements WHERE id = 7").fetchone()[0]
    )[70][0]["contractNumberSys"] == "C-1"

    user = auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    conn.execute("UPDATE users SET telegram_chat_id = '42', company_bin = '171040031283', paid_until = now() + interval '30 days'")
    marks.set_mark(conn, user, 70, "interested")
    marks.set_mark(conn, user, 71, "interested")
    sent = []
    monkeypatch.setattr(marks.telegram, "send_message", lambda chat, text: sent.append(text))
    assert marks.announce_results(conn, pause=0) == 2
    # Избранное остаётся избранным, а в сообщении — выиграли или нет (по БИН).
    assert marks.of_user(conn, user, [70, 71]) == {70: ("interested", ""), 71: ("interested", "")}
    assert any("Вы не выиграли / Сіз ұтпадыңыз" in text for text in sent)
    assert marks.announce_results(conn, pause=0) == 0


def test_parse_pending_skips_announcements_without_files(conn):
    db.upsert_announcement(conn, {"id": 8, "name_ru": "Без файлов", "end_date": "2020-01-01 10:00:00",
                                  "ref_buy_status_id": 350})
    conn.execute("""UPDATE announcements SET detail = '{"Files": null, "Lots": null}' WHERE id = 8""")
    assert results.parse_pending(conn, 10) == 0


SINGLE = """Протокол об итогах государственных закупок способом из одного источника по несостоявшимся закупкам №1-ИОИ1
№ лота:
100-ОИ2
Информация о приглашенном потенциальном поставщике:
Наименование поставщика
БИН (ИИН) / ИНН / УНП
Реквизиты
ИП Масалимова
780316401707
Адрес: Казахстан
Информация о ценовом предложении потенциального поставщика:
Выделенная сумма
Цена поставщика
Дата и время предоставления ценового предложения
13000
12000
06.11.2026 17:22:11.370
Решено заключить договор о закупках способом «Из одного источника» с приглашенным потенциальным поставщиком
"""


def test_single_source_protocol():
    parsed = results.parse_protocol(SINGLE)["100-ОИ2"]
    assert parsed["outcome"] == "winner" and parsed["winner_bin"] == "780316401707"
    assert parsed["offers"][0]["total"] == "12000" and parsed["winner_name"] == "ИП Масалимова"
