"""Цена заявки: расчёт по итогам похожих лотов и объяснение ИИ."""

import os

from psycopg.types.json import Jsonb

from tests.test_research import _lot
from tests.web_helpers import ORIGIN, _login
from zakup import bids, db, details, research


def _similar(conn, discounts, method=3):
    """Лот 90 (раздел 17, регион 15) и завершённые похожие лоты с заданным снижением."""
    user = _lot(conn)
    conn.execute("UPDATE lots SET divisions = '{17}', regions = '{15}' WHERE id = 90")
    conn.execute("UPDATE announcements SET trade_method_id = %s WHERE id = 9", (method,))
    db.upsert_announcement(conn, {"id": 10, "name_ru": "Старое", "end_date": "2020-01-01 09:00:00"})
    details.save(conn, {"id": 10, "endDate": "2020-01-01 09:00:00", "Lots": [
        {"id": 200 + i, "trdBuyId": 10, "nameRu": "Стол", "amount": 100000, "count": 1, "Plans": []}
        for i in range(len(discounts))]})
    conn.execute("UPDATE announcements SET trade_method_id = %s WHERE id = 10", (method,))
    conn.execute("UPDATE lots SET divisions = '{17}', regions = '{15}' WHERE trd_buy_id = 10")
    for i, d in enumerate(discounts):
        conn.execute("""INSERT INTO lot_results (lot_id, outcome, winner_total, offers)
                        VALUES (%s, 'winner', %s, %s)""",
                     (200 + i, 100000 * (1 - d), Jsonb([{}] if i % 2 else [{}, {}, {}])))
    conn.commit()
    return user


def test_compute_prices_and_margin(conn):
    _similar(conn, [i / 100 for i in range(1, 21)])  # снижение 1…20%
    bid = bids.compute(conn, 90, [{"unit_price": 70000, "fit": "полностью"}, {"unit_price": 10, "fit": "аналог"},
                                    {"unit_price": 5, "fit": "полностью", "unit_estimated": True}])
    assert bid["level"] == "division_region_method" and bid["won"] == 20 and bid["reliable"]
    assert abs(bid["median"] - 0.105) < 1e-9
    # Лот 90: 400 000 ₸ на 4 штуки; себестоимость — 70 000 × 4.
    assert bid["prices"]["usual"] == round(400000 * (1 - 0.105), 2)
    assert bid["prices"]["careful"] > bid["prices"]["usual"] > bid["prices"]["bold"]
    assert bid["cost"] == 280000 and bid["margins"]["usual"] == round(bid["prices"]["usual"] - 280000, 2)
    assert abs(bid["single"] - 0.5) < 1e-9 and abs(bid["offers"] - 2) < 1e-9


def test_compute_falls_back_and_none_without_results(conn):
    _similar(conn, [0.1] * 5, method=7)
    conn.execute("UPDATE announcements SET trade_method_id = 3 WHERE id = 9")
    conn.commit()
    bid = bids.compute(conn, 90)
    # Того же способа нет — берём «ту же категорию»; мало лотов — «ориентировочно».
    assert bid["level"] == "division" and not bid["reliable"] and bid["cost"] is None
    conn.execute("DELETE FROM lot_results WHERE lot_id = 200")
    conn.commit()
    assert bids.compute(conn, 90) is None  # 4 похожих итога — мало, цену не советуем


def test_advice_queue_bilingual(conn, monkeypatch):
    _similar(conn, [0.1] * 16)
    prompts = []

    def fake(prompt, model, schema=None, web=True, **_):
        prompts.append(prompt)
        # Первый ответ — казахский со смесью алфавитов: должна быть вторая попытка.
        kk = "Шамамен 360 000 ₸ бermeңіз." if len(prompts) == 1 else "Шамамен 360 000 ₸ қойыңыз."
        return {"text": "Ставьте около 360 000 ₸.", "kk": kk}
    monkeypatch.setattr(research.providers, "_claude_code", fake)
    assert bids.request(conn, 90) == "queued"
    assert bids.request(conn, 90) == "queued"  # повторный запрос — та же задача
    research.work(os.environ["TEST_DATABASE_URL"], ["claude_code"], once=True)
    advice = bids.of(conn, 90)
    assert advice["status"] == "done" and advice["result"]["kk"] == "Шамамен 360 000 ₸ қойыңыз."
    assert len(prompts) == 2 and "одна заявка — " in prompts[0]
    assert "из них с победителем: 16" in prompts[0] and "360 000" in prompts[0]
    assert bids.request(conn, 90) == "done"  # готовое — общее, без нового запроса к ИИ


def test_lot_page_bid_tab_paid_only(client, conn):
    _similar(conn, [0.1] * 16)
    page = client.get("/lots/90").text
    assert 'data-tab="bid"' in page and 'data-feature="bid_price"' in page and "360 000" not in page
    _login(client)
    page = client.get("/lots/90").text
    assert "360 000.00" in page and "data-advice-start" in page
    response = client.post("/lots/90/bid-advice", headers=ORIGIN)
    assert response.json()["status"] == "queued"
    assert "ИИ разбирает цифры" in client.get("/lots/90/bid-advice").text
    client.post("/lots/90/bid-advice", headers=ORIGIN)  # повторный клик — не в дневной лимит
    assert conn.execute("SELECT count(*) FROM user_actions WHERE action = 'bid_advice'").fetchone()[0] == 1
