from unittest.mock import AsyncMock, MagicMock

from mautrix.types import Event

from bitsai.matrix.messenger import MatrixMessenger

ROOM, DM, USER = "!room:x", "!dm:x", "@alice:x"


def make(is_dm: bool, open_result=(DM, False)):
    client = MagicMock()
    client.send_message_event = AsyncMock(return_value="$e")
    open_dm = AsyncMock(return_value=open_result)
    m = MatrixMessenger(client, "bits", open_dm, AsyncMock(return_value=is_dm))
    return m, client, open_dm


def bodies(client):
    return [(c.args[0], c.args[2].body) for c in client.send_message_event.await_args_list]


async def test_already_in_dm_does_not_open_another():
    m, client, open_dm = make(is_dm=True)
    await m.notify_login_needed(DM, USER)
    open_dm.assert_not_awaited()
    assert bodies(client) == [(DM, bodies(client)[0][1])]
    assert "here" in bodies(client)[0][1]


async def test_dm_reminders_are_never_silenced():
    m, client, _ = make(is_dm=True)
    await m.notify_login_needed(DM, USER)
    await m.notify_login_needed(DM, USER)
    assert len(bodies(client)) == 2


async def test_shared_room_reuses_existing_dm_and_hints_there():
    m, client, open_dm = make(is_dm=False, open_result=(DM, False))
    await m.notify_login_needed(ROOM, USER)
    open_dm.assert_awaited_once_with(USER)
    rooms = [r for r, _ in bodies(client)]
    assert rooms == [DM, ROOM]
    assert "our direct chat" in bodies(client)[1][1]


async def test_shared_room_new_dm_says_invited_and_is_throttled():
    m, client, _ = make(is_dm=False, open_result=(DM, True))
    await m.notify_login_needed(ROOM, USER)
    await m.notify_login_needed(ROOM, USER)
    assert [r for r, _ in bodies(client)] == [ROOM]
    assert "invited" in bodies(client)[0][1]


def raw_msg(eid, sender, body, msgtype="m.text", **extra):
    return {
        "type": "m.room.message", "event_id": eid, "sender": sender, "room_id": ROOM,
        "origin_server_ts": 1, "content": {"msgtype": msgtype, "body": body}, **extra,
    }


def thread_client(root, chunk):
    m, client, _ = make(False)
    client.get_event = AsyncMock(return_value=Event.deserialize(root))
    client.api.request = AsyncMock(return_value={"chunk": chunk})
    client.crypto = None
    return m, client


async def test_thread_messages_order_filter_and_edits():
    root = raw_msg("$r", "@a:x", "root")
    newest_first = [
        raw_msg("$3", "@b:x", "img", msgtype="m.image"),
        raw_msg("$2", "@bot:x", "old", msgtype="m.notice", unsigned={"m.relations": {
            "m.replace": {"content": {"m.new_content": {"msgtype": "m.notice", "body": "new"}}}}}),
        raw_msg("$1", "@b:x", "hello", msgtype="m.emote"),
        {**raw_msg("$e", "@b:x", "* edit"), "content": {
            "msgtype": "m.text", "body": "* edit",
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"}}},
        {"type": "m.reaction", "event_id": "$x", "sender": "@b:x", "room_id": ROOM,
         "origin_server_ts": 1, "content": {}},
    ]
    m, client = thread_client(root, newest_first)
    out = await m.get_thread_messages(ROOM, "$r", limit=10)
    assert out == [("@a:x", "root"), ("@b:x", "hello"), ("@bot:x", "new")]
    call = client.api.request.await_args
    assert "relations" in str(call.args[1]) and "m.thread" in str(call.args[1])
    assert call.kwargs["query_params"] == {"limit": "10", "dir": "b"}


async def test_thread_messages_decrypts_and_skips_failures():
    root = raw_msg("$r", "@a:x", "root")
    enc = lambda eid: {  # noqa: E731
        "type": "m.room.encrypted", "event_id": eid, "sender": "@b:x", "room_id": ROOM,
        "origin_server_ts": 1, "content": {"algorithm": "m.megolm.v1.aes-sha2",
                                           "ciphertext": "x", "session_id": "s",
                                           "sender_key": "k", "device_id": "d"}}
    m, client = thread_client(root, [enc("$bad"), enc("$ok")])

    async def decrypt(evt):
        if evt.event_id == "$bad":
            raise RuntimeError("no session")
        return Event.deserialize(raw_msg("$ok", "@b:x", "secret"))

    client.crypto = MagicMock()
    client.crypto.decrypt_megolm_event = AsyncMock(side_effect=decrypt)
    assert await m.get_thread_messages(ROOM, "$r") == [("@a:x", "root"), ("@b:x", "secret")]
    client.crypto = None
    assert await m.get_thread_messages(ROOM, "$r") == [("@a:x", "root")]


async def test_thread_messages_error_returns_empty():
    m, client = thread_client(raw_msg("$r", "@a:x", "root"), [])
    client.api.request = AsyncMock(side_effect=RuntimeError("boom"))
    assert await m.get_thread_messages(ROOM, "$r") == []
