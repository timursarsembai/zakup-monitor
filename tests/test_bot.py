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


def test_group_links_by_code_and_moves_to_supergroup(conn, monkeypatch):
    monkeypatch.setenv("ZAKUP_TELEGRAM_BOT_USERNAME", "zakup_bot")
    user = auth.set_password(conn, "a@b.kz", "long-enough-password")
    private_code = bot.create_link_code(conn, user)
    code = bot.create_link_code(conn, user, "group")
    assert bot.group_link_url(code).endswith(f"?startgroup={code}")
    group = {"chat": {"id": -100, "type": "group", "title": "Партнёры"},
             "text": f"/start@zakup_bot {private_code}"}
    assert "устарела" in bot.handle(conn, group)  # личный код в группе не работает
    group["text"] = f"/start@other_bot {code}"
    assert bot.handle(conn, group) is None  # команда другому боту
    group["text"] = f"/start@zakup_bot {code}"
    assert "Группа подключена" in bot.handle(conn, group)
    conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, kind, chat_id)
           VALUES (%s, 'x', '', 'announcements', '-100')""", (user,))
    bot.handle(conn, {"chat": {"id": -100, "type": "group"}, "migrate_to_chat_id": -1009})
    assert conn.execute("SELECT chat_id FROM user_groups").fetchone()[0] == "-1009"
    assert conn.execute("SELECT chat_id FROM subscriptions").fetchone()[0] == "-1009"
    stop = {"chat": {"id": -1009, "type": "supergroup"}, "text": "/stop@zakup_bot", "from": {"id": 7}}
    statuses = {7: "member"}
    monkeypatch.setattr(bot.telegram, "call", lambda method, **kw: {"status": statuses[kw["user_id"]]})
    assert "только администратор" in bot.handle(conn, stop)
    assert conn.execute("SELECT count(*) FROM user_groups").fetchone()[0] == 1
    statuses[7] = "administrator"
    assert "отключена" in bot.handle(conn, stop)
    assert conn.execute("SELECT count(*) FROM user_groups").fetchone()[0] == 0
    assert conn.execute("SELECT chat_id FROM subscriptions").fetchone()[0] is None
