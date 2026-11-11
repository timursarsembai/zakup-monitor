"""Обеспечение заявки и договора: проценты из документов и расчёт сумм."""

from psycopg.types.json import Jsonb

from zakup import db, securities

CONTRACT = ("2) в течение десяти рабочих дней со дня вступления в силу Договора, внести сумму обеспечения "
            "исполнения\n Договора в размере 5 (пяти) процентов от общей суммы Договора равную …")
KD = ("4. Потенциальный поставщик вносит с заявкой обеспечение заявки на участие в конкурсе в размере одного "
      "процента от суммы, выделенной для приобретения товаров")


def test_parse_digits_and_words():
    assert securities.parse(CONTRACT, securities._CONTRACT) == 5
    assert securities.parse("обеспечение исполнения Договора в размере трех процентов от суммы", securities._CONTRACT) == 3
    assert securities.parse(KD, securities._BID) == 1
    assert securities.parse("обеспечение исполнения договора в размере 100 процентов", securities._CONTRACT) is None


def test_for_lot_amounts_and_fallback():
    found = {"contract": 3.0, "bid": 1.0, "source": "documents"}
    d = securities.for_lot(found, 2, 1000000, 30, price=800000)
    assert d["bid"] == 10000 and d["contract_max"] == 330000 and d["contract_at_price"] == 264000
    # Документы ещё не разобраны — по способу: запрос ценовых предложений — 5% договора, без заявки.
    d = securities.for_lot(None, 3, 1000000, None)
    assert d["bid"] is None and d["contract_max"] == 50000 and d["source"] == "method"
    assert securities.for_lot(None, 999, 1000000, None) is None


def test_prefetch_reads_documents(conn, monkeypatch):
    db.upsert_announcement(conn, {"id": 77, "name_ru": "Конкурс", "end_date": "2099-01-01 09:00:00"})
    conn.execute("UPDATE announcements SET lots_ready = true, detail = %s WHERE id = 77", (Jsonb({"Files": [
        {"nameRu": "Проект договора об электронных закупках", "filePath": "https://ows.goszakup.gov.kz/c"},
        {"nameRu": "Конкурсная документация ", "filePath": "https://ows.goszakup.gov.kz/k"}]}),))
    conn.commit()
    monkeypatch.setattr(securities.spec, "download", lambda url: url.encode())
    monkeypatch.setattr(securities.spec, "extract", lambda data: CONTRACT if data.endswith(b"/c") else KD)
    assert securities.prefetch(conn, 5) == 1
    assert conn.execute("SELECT securities FROM announcements WHERE id = 77").fetchone()[0] == {
        "contract": 5.0, "bid": 1.0, "source": "documents"}
    assert securities.prefetch(conn, 5) == 0  # уже разобрано
