from zakup import auth, bot


def _msg(text, chat_id=555, chat_type="private"):
    return {"chat": {"id": chat_id, "type": chat_type}, "text": text}


def _chat(conn, email):
    return conn.execute("SELECT telegram_chat_id FROM users WHERE email = %s", (email,)).fetchone()[0]


def test_link_code_binds_chat_once(conn):
    user = auth.set_password(conn, "a@b.kz", "long-enough-password")
    code = bot.create_link_code(conn, user)
    assert "a@b.kz" in bot.handle(conn, _msg(f"/start {code}"))
    assert _chat(conn, "a@b.kz") == "555"
    assert "устарела" in bot.handle(conn, _msg(f"/start {code}", chat_id=777))
    assert _chat(conn, "a@b.kz") == "555"


def test_expired_code_is_rejected(conn):
    user = auth.set_password(conn, "a@b.kz", "long-enough-password")
    code = bot.create_link_code(conn, user)
    conn.execute("UPDATE telegram_link_codes SET expires_at = now() - interval '1 second'")
    assert "устарела" in bot.handle(conn, _msg(f"/start {code}"))
    assert _chat(conn, "a@b.kz") is None


def test_chat_moves_to_the_latest_account_and_stop_unlinks(conn):
    first = auth.set_password(conn, "a@b.kz", "long-enough-password")
    second = auth.set_password(conn, "c@d.kz", "long-enough-password")
    bot.handle(conn, _msg(f"/start {bot.create_link_code(conn, first)}"))
    bot.handle(conn, _msg(f"/start {bot.create_link_code(conn, second)}"))
    assert (_chat(conn, "a@b.kz"), _chat(conn, "c@d.kz")) == (None, "555")
    assert bot.handle(conn, _msg("/stop")) == "Уведомления отключены."
    assert _chat(conn, "c@d.kz") is None


def test_plain_messages_get_help_and_groups_are_ignored(conn):
    assert "/settings" in bot.handle(conn, _msg("привет"))
    assert bot.handle(conn, _msg("/start", chat_type="group")) is None
