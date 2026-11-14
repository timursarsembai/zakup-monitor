from datetime import UTC, datetime, timedelta

from tests.web_helpers import _announcement_with_lots
from zakup import selections


def test_selection_pages(client, conn):
    _announcement_with_lots(conn)  # лоты 5001 (1 млн ₸) и 5002 (0,5 млн ₸), приём до 2099 г.
    conn.execute("UPDATE lots SET kato = '{751410000}' WHERE id = 5001")
    conn.commit()
    small = client.get("/tendery/malyy-biznes").text
    assert "Тендеры для малого бизнеса в Казахстане" in small and "/lots/5001" in small and "/lots/5002" in small
    assert 'href="/tendery/malyy-biznes/75"' in small and "BreadcrumbList" in small
    almaty = client.get("/tendery/malyy-biznes/75").text
    assert "Тендеры для малого бизнеса — г. Алматы" in almaty and "/lots/5001" in almaty and "/lots/5002" not in almaty
    assert "/lots/5001" not in client.get("/tendery/krupnye").text
    assert "/lots/5001" not in client.get("/tendery/goryashchie").text

    conn.execute("UPDATE lots SET amount = 60000000 WHERE id = 5001")
    conn.execute("UPDATE announcements SET end_date = %s WHERE id = 500", (datetime.now(UTC) + timedelta(hours=3),))
    conn.commit()
    selections._counts.update(at=0.0, rows={})
    selections._pages.clear()
    conn.execute("DELETE FROM app_settings WHERE key = %s", (selections.SNAPSHOT_KEY,))
    conn.commit()
    large = client.get("/tendery/krupnye").text
    assert "/lots/5001" in large and "/lots/5002" not in large
    hot = client.get("/tendery/goryashchie").text
    assert "/lots/5001" in hot and "/lots/5002" in hot
    assert "/lots/5001" not in client.get("/tendery/malyy-biznes").text

    index = client.get("/tendery").text
    assert all(f'href="/tendery/{slug}"' in index for slug in selections.SELECTIONS)
    assert "Ірі тендерлер" in client.get("/tendery/krupnye?lang=kk").text
    assert client.get("/tendery/nothing").status_code == 404
    assert client.get("/tendery/krupnye/00").status_code == 404
    static = client.get("/sitemap-static.xml").text
    assert "/tendery/krupnye/75" in static and "/tendery/zapros-cen<" in static
    assert 'href="/tendery"' in client.get("/categories").text
    # «Другие подборки» — только где есть лоты: в Алматы малого бизнеса больше нет.
    assert 'href="/tendery/malyy-biznes/75"' not in client.get("/tendery/krupnye/75").text
    assert 'href="/tendery/goryashchie/75"' in client.get("/tendery/krupnye/75").text


def test_selection_counts_from_snapshot(conn):
    _announcement_with_lots(conn)
    fresh = selections.refresh(conn)
    assert fresh["malyy-biznes"][""] == (2, 1500000.0)
    selections._counts.update(at=0.0, rows={})
    started = []
    assert selections.counts(conn, background=lambda: started.append(1)) == fresh  # из снимка, не считая
    assert started == [1]


def test_selection_search_link_matches_filter():
    now = datetime(2026, 11, 14, 20, 0, tzinfo=UTC)   # в Казахстане уже 15-е
    hot = selections.filters("goryashchie", "75", now)
    assert hot.deadline == "2026-11-16" and hot.regions == ["75"] and hot.sort == "end"
    assert selections.filters("zapros-cen").methods == ["zcp"]
    assert "min_sum=50000000" in selections.filters("krupnye").to_query("lots")
