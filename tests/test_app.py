import os
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kobosend-test-")
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ["LOOKUP_RATE_LIMIT_PER_MIN"] = "1000"

import pytest
from fastapi.testclient import TestClient

from app import kobo_filenames, main
from app.rooms import CODE_ALPHABET, normalize_code


@pytest.fixture
def phone():
    return TestClient(main.app)


@pytest.fixture
def kobo():
    return TestClient(main.app)


def room_code(client: TestClient) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return client.cookies["kobosend_room"].split(":")[0]


def upload(client, code, name, data=b"PK\x03\x04fake-epub", ctype="application/epub+zip"):
    return client.post(
        f"/api/rooms/{code}/upload",
        files={"file": (name, data, ctype)},
        headers={"Accept": "application/json"},
    )


def test_safe_filenames():
    assert kobo_filenames.safe_filename("Les Misérables – Tome 1.epub", ".epub") == "Les_Miserables_Tome_1.epub"
    assert kobo_filenames.safe_filename("C:\\Books\\北京.pdf", ".pdf") == "book.pdf"
    assert kobo_filenames.safe_filename("x.kepub.epub", ".kepub.epub") == "x.kepub.epub"
    allowed = (".epub", ".pdf", ".kepub.epub")
    assert kobo_filenames.match_extension("A.KEPUB.EPUB", allowed) == ".kepub.epub"
    assert kobo_filenames.match_extension("a.mobi", allowed) is None
    assert kobo_filenames.match_extension(".epub", allowed) is None


def test_code_alphabet_and_normalize():
    assert not set("01OIL") & set(CODE_ALPHABET)
    assert normalize_code(" k7qm ") == "K7QM"
    assert normalize_code("K0QM") is None
    assert normalize_code("ABC") is None


def test_room_is_sticky_per_browser(phone):
    assert room_code(phone) == room_code(phone)


def test_full_flow(phone, kobo):
    code = room_code(phone)
    r = upload(phone, code, "Café Book.epub")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"

    r = kobo.get(f"/get?code={code.lower()}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/r/{code}"

    r = kobo.get(f"/r/{code}")
    assert r.status_code == 200
    assert f'href="/d/{code}/Cafe_Book.epub"' in r.text
    assert "Café Book.epub" in r.text

    r = kobo.head(f"/d/{code}/Cafe_Book.epub")
    assert r.status_code == 200

    r = kobo.get(f"/d/{code}/Cafe_Book.epub")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/epub+zip"
    assert r.headers["content-disposition"] == 'attachment; filename="Cafe_Book.epub"'
    assert r.content == b"PK\x03\x04fake-epub"

    assert phone.get(f"/api/rooms/{code}/status").json()["downloads"] == 1
    assert kobo.get(f"/d/{code}/other.epub").status_code == 404


def test_pdf_content_type(phone, kobo):
    code = room_code(phone)
    assert upload(phone, code, "Paper.pdf", b"%PDF-1.4", "application/pdf").status_code == 200
    r = kobo.get(f"/d/{code}/Paper.pdf")
    assert r.headers["content-type"] == "application/pdf"


def test_upload_replaces_previous(phone, kobo):
    code = room_code(phone)
    upload(phone, code, "one.epub")
    upload(phone, code, "two.epub")
    assert kobo.get(f"/d/{code}/one.epub").status_code == 404
    assert kobo.get(f"/d/{code}/two.epub").status_code == 200


def test_upload_requires_owner_cookie(phone, kobo):
    code = room_code(phone)
    assert upload(kobo, code, "evil.epub").status_code == 404


def test_rejects_bad_type_and_size(phone):
    code = room_code(phone)
    assert upload(phone, code, "x.mobi").status_code == 415
    assert upload(phone, code, "big.epub", b"x" * (2 * 1024 * 1024)).status_code == 413
    assert phone.get(f"/api/rooms/{code}/status").json()["state"] == "waiting"


def test_form_upload_redirects(phone):
    code = room_code(phone)
    r = phone.post(
        f"/api/rooms/{code}/upload", files={"file": ("x.txt", b"hi")}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/?error=bad_type"


def test_receiver_messages(phone, kobo):
    assert kobo.get("/r/ZZZZ").status_code == 404
    assert kobo.get("/get?code=12").status_code == 400
    code = room_code(phone)
    r = kobo.get(f"/r/{code}")
    assert r.status_code == 200 and "no book has been uploaded" in r.text


def test_delete_after_download(phone, kobo, monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.__class__(**{**main.settings.__dict__, "delete_after_download": True}))
    code = room_code(phone)
    upload(phone, code, "once.epub")
    assert kobo.get(f"/d/{code}/once.epub").status_code == 200
    assert kobo.get(f"/d/{code}/once.epub").status_code == 404
    assert kobo.get(f"/r/{code}").status_code == 410


def test_sweep_removes_expired(phone):
    code = room_code(phone)
    upload(phone, code, "old.epub")
    room = main.store.get_active(code)
    with main.store._conn() as conn:
        conn.execute("UPDATE rooms SET expires_at = ? WHERE code = ?", (time.time() - 1, code))
    main.store.sweep_expired()
    assert main.store.get_active(code) is None
    assert not (main.store.files_dir / room.stored_name).exists()


def test_rate_limit():
    from app.ratelimit import RateLimiter

    limiter = RateLimiter(2)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")


def test_unused_code_lasts_five_minutes_then_upload_extends(phone):
    code = room_code(phone)
    left = phone.get(f"/api/rooms/{code}/status").json()["expires_in_seconds"]
    assert 280 <= left <= 300
    upload(phone, code, "keep.epub")
    assert phone.get(f"/api/rooms/{code}/status").json()["expires_in_seconds"] > 23 * 3600


def test_cookie_survives_plain_http_with_cookie_secure(monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.__class__(**{**main.settings.__dict__, "cookie_secure": True}))
    client = TestClient(main.app, base_url="http://nas.local:8085")
    r = client.get("/")
    assert "secure" not in r.headers["set-cookie"].lower()
    assert room_code(client) == room_code(client)

    https = TestClient(main.app, base_url="https://books.example.com")
    assert "secure" in https.get("/").headers["set-cookie"].lower()


def test_healthz(kobo):
    assert kobo.get("/healthz").json() == {"status": "ok"}
