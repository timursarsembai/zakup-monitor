"""Фото своих товаров (zakup.photos): проверка, WebP без EXIF, главное фото, удаление файлов."""

import io

import pytest
from PIL import Image

from tests.test_shop import SHOP, _as_client
from tests.web_helpers import ORIGIN
from zakup import photos, products


@pytest.fixture(autouse=True)
def media(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAKUP_MEDIA_DIR", str(tmp_path))
    return tmp_path


def _jpeg(width=2400, height=1200, color=(200, 30, 30)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # производитель
    exif[0x8825] = {2: (43.0, 15.0, 0.0)}  # GPS — геометка, её быть не должно
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_convert_strips_exif_and_resizes():
    large, thumb, width, height = photos.convert(_jpeg())
    assert (width, height) == (1600, 800)
    with Image.open(io.BytesIO(large)) as image:
        assert image.format == "WEBP" and not image.getexif() and "exif" not in image.info
    with Image.open(io.BytesIO(thumb)) as image:
        assert image.size == (400, 200)
    for bad in (b"", b"<svg onload=alert(1)>", b"GIF89a" + b"0" * 50, _jpeg(80, 80)):
        with pytest.raises(ValueError):
            photos.convert(bad)
    with pytest.raises(ValueError):
        photos.convert(b"0" * (photos.MAX_BYTES + 1))
    gif = io.BytesIO()
    Image.new("RGB", (300, 300)).save(gif, "GIF")
    with pytest.raises(ValueError):
        photos.convert(gif.getvalue())


def test_upload_main_and_delete(client, conn, media):
    _as_client(client, conn)
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    client.post("/my/shop/products", data={"name": "Стул", "unit": "шт", "retail": "5000"}, headers=ORIGIN)
    pid = conn.execute("SELECT id FROM product_catalog WHERE name = 'Стул'").fetchone()[0]
    url = f"/my/shop/products/{pid}/photos"
    files = [("files", ("a.jpg", _jpeg(), "image/jpeg")), ("files", ("b.jpg", _jpeg(color=(0, 0, 255)), "image/jpeg"))]
    assert client.post(url, files=files, headers=ORIGIN, follow_redirects=False).status_code == 303
    first, second = photos.listing(conn, pid)
    assert (media / "p" / first["key"][:2] / f"{first['key']}.webp").exists()
    assert conn.execute("SELECT image FROM product_catalog WHERE id = %s", (pid,)).fetchone()[0] == first["thumb"]
    # Не картинка — сообщение на форме, остальное сохранено.
    bad = client.post(url, files=[("files", ("x.jpg", b"not an image", "image/jpeg"))], headers=ORIGIN)
    assert bad.status_code == 422 and "не фото" in bad.text and len(photos.listing(conn, pid)) == 2
    # Главное — второе; страница товара — с фото.
    client.post(f"{url}/{second['id']}/main", headers=ORIGIN)
    assert photos.listing(conn, pid)[0]["id"] == second["id"]
    client.post("/my/shop", data=SHOP, headers=ORIGIN)  # магазин на проверке — страница владельцу
    page = client.get(products.path({"id": pid, "name": "Стул"})).text
    assert second["url"] in page and first["thumb"] in page
    # Удалить фото — и файлы; удалить товар — и все его фото.
    client.post(f"{url}/{first['id']}/delete", headers=ORIGIN)
    assert not (media / "p" / first["key"][:2] / f"{first['key']}.webp").exists()
    # Чужой товар — не тронуть.
    _as_client(client, conn, "+77019998877")
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    assert client.post(url, files=files[:1], headers=ORIGIN).status_code == 404
    _as_client(client, conn)
    client.post(f"/my/shop/products/{pid}/delete", headers=ORIGIN)
    assert not any(media.rglob("*.webp"))


def test_photo_limit(client, conn, monkeypatch):
    monkeypatch.setattr(photos, "MAX_PER_PRODUCT", 1)
    _as_client(client, conn)
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    client.post("/my/shop/products", data={"name": "Стол", "unit": "шт", "retail": "5000"}, headers=ORIGIN)
    pid = conn.execute("SELECT id FROM product_catalog WHERE name = 'Стол'").fetchone()[0]
    files = [("files", ("a.jpg", _jpeg(), "image/jpeg")), ("files", ("b.jpg", _jpeg(), "image/jpeg"))]
    client.post(f"/my/shop/products/{pid}/photos", files=files, headers=ORIGIN)
    assert len(photos.listing(conn, pid)) == 1


def test_sweep_removes_orphan_files(conn, media):
    import os
    import time
    conn.execute("""INSERT INTO product_catalog (url_key, name, own) VALUES ('own:1', 'Стул', true)""")
    pid = conn.execute("SELECT id FROM product_catalog").fetchone()[0]
    kept = photos.add(conn, pid, photos.convert(_jpeg()))
    key = conn.execute("SELECT key FROM product_photos WHERE id = %s", (kept,)).fetchone()[0]
    orphan = media / "p" / "ab" / ("ab" + "0" * 30 + ".webp")
    fresh = media / "p" / "cd" / ("cd" + "0" * 30 + ".webp")
    for path in (orphan, fresh):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    old = time.time() - 7200
    for path in [orphan, *media.rglob(f"{key}*.webp")]:
        os.utime(path, (old, old))
    assert photos.sweep(conn) == 1
    assert not orphan.exists() and fresh.exists() and len(list(media.rglob(f"{key}*.webp"))) == 2
