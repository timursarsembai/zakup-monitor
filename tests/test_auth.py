from zakup import auth


def test_password_hash_roundtrip():
    stored = auth.hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "garbage")


def test_login_creates_session_and_logout_ends_it(conn):
    auth.set_password(conn, " Owner@Mail.kz ", "long-enough-password")
    assert auth.login(conn, "owner@mail.kz", "nope", "1.1.1.1", None) is None
    token = auth.login(conn, "OWNER@mail.kz", "long-enough-password", "1.1.1.1", "ua")
    assert token
    assert auth.user_by_session(conn, token)[1] == "owner@mail.kz"
    auth.logout(conn, token)
    assert auth.user_by_session(conn, token) is None
    assert auth.user_by_session(conn, None) is None


def test_expired_session_is_rejected(conn):
    auth.set_password(conn, "a@b.kz", "long-enough-password")
    token = auth.login(conn, "a@b.kz", "long-enough-password", None, None)
    conn.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")
    assert auth.user_by_session(conn, token) is None


def test_password_change_drops_sessions(conn):
    auth.set_password(conn, "a@b.kz", "long-enough-password")
    token = auth.login(conn, "a@b.kz", "long-enough-password", None, None)
    auth.set_password(conn, "a@b.kz", "another-long-password")
    assert auth.user_by_session(conn, token) is None


def test_failures_are_throttled_per_email_and_ip(conn):
    for _ in range(auth.MAX_FAILURES_PER_EMAIL):
        assert not auth.too_many_failures(conn, "a@b.kz", "1.1.1.1")
        auth.login(conn, "a@b.kz", "wrong", "1.1.1.1", None)
    assert auth.too_many_failures(conn, "a@b.kz", "2.2.2.2")
    assert not auth.too_many_failures(conn, "c@d.kz", "2.2.2.2")
    for i in range(auth.MAX_FAILURES_PER_IP):
        auth.login(conn, f"u{i}@x.kz", "wrong", "3.3.3.3", None)
    assert auth.too_many_failures(conn, "fresh@x.kz", "3.3.3.3")
