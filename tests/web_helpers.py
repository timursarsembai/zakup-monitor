"""Общее для тестов сайта: вход, тестовые данные, подписи вебхука, SMS."""

import hashlib
import hmac

from zakup import db, details, signup

ORIGIN = {"origin": "https://testserver"}


def _login(client, password="long-enough-password", next="/"):
    return client.post(
        "/login",
        data={"email": "owner@mail.kz", "password": password, "next": next},
        headers=ORIGIN,
        follow_redirects=False,
    )


def _announcement_with_lots(conn):
    db.upsert_announcement(conn, {"id": 500, "number_anno": "500-1", "name_ru": "Канцтовары для школы",
                                  "total_sum": 1500000, "org_name_ru": "Школа №1",
                                  "ref_buy_status_id": 240, "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {
        "id": 500, "finYear": [2026, 2027], "endDate": "2099-01-01 09:00:00",
        "Lots": [
            {"id": 5001, "trdBuyId": 500, "lotNumber": "L-1", "nameRu": "Бумага А4", "amount": 1000000,
             "count": 100, "Plans": [{"price": 10000, "refEnstruCode": "171211.100.000000"}]},
            {"id": 5002, "trdBuyId": 500, "lotNumber": "L-2", "nameRu": "Ручка шариковая",
             "amount": 500000, "count": 1000, "Plans": [{"price": 500}]},
        ],
    })
    conn.execute("UPDATE lots SET status_id = 240")
    conn.commit()


def _fake_sms(monkeypatch, code="1234"):
    sent = []
    monkeypatch.setattr(signup.kazpayment, "request_otp",
                        lambda phone: sent.append(phone) or f"req-{len(sent)}")
    monkeypatch.setattr(signup.kazpayment, "verify_otp",
                        lambda request_id, entered: "77011112233" if entered == code else None)
    return sent


def _flow(page: str) -> str:
    return page.split('name="flow" value="', 1)[1].split('"', 1)[0]


def _signed(body: dict, secret: str = "whsec") -> tuple[bytes, dict]:
    import json as _json

    raw = _json.dumps(body).encode()
    return raw, {"x-kazpayment-signature": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                 "content-type": "application/json"}
