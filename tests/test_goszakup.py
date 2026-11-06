from datetime import datetime

from zakup.goszakup import PORTAL_TZ, next_after, normalize, parse_date


def test_next_after_reads_cursor_from_next_page():
    assert next_after("/v3/lots?page=next&search_after=12345") == 12345
    assert next_after("") is None
    assert next_after(None) is None
    assert next_after("/v3/lots?page=next") is None


def test_normalize_turns_camel_case_into_snake_case():
    assert normalize({"nameRu": "x", "refBuyStatusId": 1, "total_sum": 2}) == {
        "name_ru": "x",
        "ref_buy_status_id": 1,
        "total_sum": 2,
    }


def test_parse_date_assumes_portal_time_zone():
    assert parse_date("2026-11-06 10:00:00") == datetime(2026, 11, 6, 10, tzinfo=PORTAL_TZ)
    assert parse_date("2026-11-06T10:00:00+05:00").utcoffset().total_seconds() == 5 * 3600
    assert parse_date(None) is None
    assert parse_date("not a date") is None
