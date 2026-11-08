from datetime import UTC, datetime, timedelta

from zakup import auth, bot, client_bot, telegram


def _calls(monkeypatch, admins=("creator",)):
    calls = []

    def fake(method, http_timeout=15, bot="main", **params):
        calls.append((method, bot, params))
        if method == "getChatMember":
            return {"status": admins[0]}
        if method == "createChatInviteLink":
            return {"invite_link": "https://t.me/+join"}
        return True

    monkeypatch.setattr(telegram, "call", fake)
    return calls


def test_routing_by_chat_prefix():
    assert telegram.route("c:42") == ("client", "42") and telegram.route("-100") == ("main", "-100")


def test_link_group_join_requests_and_expiry(conn, monkeypatch):
    calls = _calls(monkeypatch)
    uid = auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user' WHERE id = %s", (uid,))
    conn.commit()
    code = bot.create_link_code(conn, uid, "client")
    reply = client_bot.handle_message(conn, {"chat": {"id": 555, "type": "private"}, "text": f"/start {code}"})
    assert "+77011112233" in reply
    assert conn.execute("SELECT telegram_chat_id FROM users WHERE id = %s", (uid,)).fetchone()[0] == "c:555"
    # Группу подключает её администратор командой /start.
    assert client_bot.handle_message(conn, {"chat": {"id": -1009, "type": "supergroup", "title": "Клиенты"},
                                            "from": {"id": 1}, "text": "/start@tenderim_kz_bot"})
    assert client_bot.group_id(conn) == "-1009"
    request = {"chat": {"id": -1009}, "from": {"id": 555}, "user_chat_id": 555}
    assert client_bot.handle_join_request(conn, request) == "declined"  # тариф не оплачен
    assert ("declineChatJoinRequest", "client", {"chat_id": -1009, "user_id": 555}) in calls
    conn.execute("UPDATE users SET paid_until = now() + interval '2 days' WHERE id = %s", (uid,))
    conn.commit()
    assert client_bot.handle_join_request(conn, request) == "approved"
    assert client_bot.handle_join_request(conn, {"chat": {"id": -1009}, "from": {"id": 777}}) == "declined"
    # За 3 дня — напоминание (один раз), после конца — удаление из группы.
    assert client_bot.maintenance(conn) == (1, 0) and client_bot.maintenance(conn) == (0, 0)
    assert client_bot.maintenance(conn, datetime.now(UTC) + timedelta(days=3)) == (0, 1)
    assert [c[0] for c in calls[-3:]] == ["banChatMember", "unbanChatMember", "sendMessage"]
    assert conn.execute("SELECT count(*) FROM client_group_members").fetchone()[0] == 0


def test_group_migration_to_supergroup(conn, monkeypatch):
    _calls(monkeypatch)
    client_bot._set_group(conn, "-5538", "Клиенты")
    client_bot.handle_message(conn, {"chat": {"id": -5538, "type": "group", "title": "Клиенты"},
                                     "migrate_to_chat_id": -1005538})
    assert client_bot.group_id(conn) == "-1005538"


def test_group_migration_learned_from_error(conn, monkeypatch):
    client_bot._set_group(conn, "-5538", "Клиенты")
    calls = []

    def fake(method, http_timeout=15, bot="main", **params):
        calls.append(params.get("chat_id"))
        if params.get("chat_id") == "-5538":
            raise telegram.TelegramError(400, "group chat was upgraded to a supergroup chat",
                                         {"migrate_to_chat_id": -1005538})
        return {"invite_link": "https://t.me/+x"}

    monkeypatch.setattr(telegram, "call", fake)
    assert client_bot.invite_link(conn, "client") == "https://t.me/+x"
    assert calls == ["-5538", "-1005538"] and client_bot.group_id(conn) == "-1005538"
