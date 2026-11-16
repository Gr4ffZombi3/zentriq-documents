# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Interner 1:1-Teamchat: Kollegen des eigenen Bueros, Verlauf und Lesestand serverseitig, Rechte.

Buero A: Admin A, Dennis, Laura; Buero B: Admin B, Bob; SUPER_ADMIN Justin (tests/test_permissions_security)."""

from app.models import TeamChat, TeamChatMessage
from app.tenancy import bypass_tenant_scope
from tests.test_permissions_security import login, world  # noqa: F401

BASE = "/arbeitsplatz/teamchat"


def _open(client, user_id):
    resp = client.post(f"{BASE}/mit/{user_id}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["chat"]


def _send(client, chat_id, text, ref=None):
    return client.post(f"{BASE}/{chat_id}/nachrichten", json={"text": text, "client_ref": ref})


def test_colleagues_are_active_members_of_own_office_only(app, world):
    data = login(app, "dennis@example.com").get(BASE).get_json()
    names = {c["name"] for c in data["colleagues"]}
    assert names == {"Admin A", "Laura"}  # ohne sich selbst, ohne Buero B, ohne SUPER_ADMIN
    assert data["chats"] == []


def test_messages_arrive_both_ways_with_unread_counter(app, world):
    dennis, laura = login(app, "dennis@example.com"), login(app, "laura@example.com")
    chat = _open(dennis, world.laura.id)
    assert chat["partner"]["name"] == "Laura" and _open(laura, world.dennis.id)["id"] == chat["id"]  # eine je Paar
    assert _send(dennis, chat["id"], "Hallo Laura, hast du kurz Zeit?").status_code == 201
    overview = laura.get(BASE).get_json()
    assert overview["chats"][0]["unread"] == 1 and overview["chats"][0]["partner"]["name"] == "Dennis"
    msgs = laura.get(f"{BASE}/{chat['id']}/nachrichten").get_json()["messages"]
    assert [(m["text"], m["mine"]) for m in msgs] == [("Hallo Laura, hast du kurz Zeit?", False)]
    assert laura.post(f"{BASE}/{chat['id']}/gelesen", json={"last_id": msgs[-1]["id"]}).get_json()["unread"] == 0
    reply = _send(laura, chat["id"], "Ja, ruf mich an.").get_json()["message"]
    assert reply["mine"] is True
    assert dennis.get(BASE).get_json()["chats"][0]["unread"] == 1
    newer = dennis.get(f"{BASE}/{chat['id']}/nachrichten?nach={msgs[-1]['id']}").get_json()["messages"]
    assert [m["text"] for m in newer] == ["Ja, ruf mich an."]
    # eigene Nachricht zaehlt nicht als ungelesen
    assert laura.get(BASE).get_json()["chats"][0]["unread"] == 0


def test_resend_with_same_reference_saves_once_and_live_notifies_both(app, world):
    dennis = login(app, "dennis@example.com")
    chat = _open(dennis, world.laura.id)
    app.extensions["zq_live_sent"] = []
    app.config["LIVE_ENABLED"] = True
    first = _send(dennis, chat["id"], "Doppelt?", ref="r1").get_json()["message"]
    second = _send(dennis, chat["id"], "Doppelt?", ref="r1").get_json()["message"]
    assert first["id"] == second["id"]
    with bypass_tenant_scope():
        assert TeamChatMessage.query.count() == 1
    sent = app.extensions["zq_live_sent"]
    channels = {target.rsplit(":", 2)[-2] + ":" + target.rsplit(":", 1)[-1] for target, _m in sent}
    assert {f"user:{world.dennis.id}", f"user:{world.laura.id}"} <= channels
    assert all(m == {"type": "chat", "chat": chat["id"]} for _t, m in sent)  # nur IDs, nie der Text


def test_outsiders_cannot_read_or_write(app, world):
    dennis = login(app, "dennis@example.com")
    chat = _open(dennis, world.laura.id)
    _send(dennis, chat["id"], "Nur fuer Laura")
    admin_a = login(app, "admin-a@example.com")  # gleiches Buero, aber nicht beteiligt
    for client in (admin_a, login(app, "bob@example.com"), login(app, "admin-b@example.com")):
        assert client.get(f"{BASE}/{chat['id']}/nachrichten").status_code == 404
        assert _send(client, chat["id"], "Einmischung").status_code == 404
        assert client.post(f"{BASE}/{chat['id']}/gelesen", json={"last_id": 1}).status_code == 404
    assert login(app, "justin@example.com").get(BASE).status_code == 403
    # Kollege aus fremdem Buero kann nicht angeschrieben werden
    assert dennis.post(f"{BASE}/mit/{world.bob.id}").status_code == 404
    assert dennis.post(f"{BASE}/mit/{world.dennis.id}").status_code == 404
    with bypass_tenant_scope():
        assert TeamChatMessage.query.count() == 1 and TeamChat.query.count() == 1


def test_inactive_colleague_cannot_be_written_to(app, db, world):
    dennis = login(app, "dennis@example.com")
    chat = _open(dennis, world.laura.id)
    with bypass_tenant_scope():
        world.laura.is_active = False
        db.session.commit()
    resp = _send(dennis, chat["id"], "Bist du da?")
    assert resp.status_code == 409 and "nicht mehr im Büro aktiv" in resp.get_json()["error"]
    assert dennis.get(BASE).get_json()["colleagues"] == [{"id": world.admin_a.id, "name": "Admin A"}]


def test_validation(app, world):
    dennis = login(app, "dennis@example.com")
    chat = _open(dennis, world.laura.id)
    assert _send(dennis, chat["id"], "   ").status_code == 400
    assert _send(dennis, chat["id"], "x" * 2001).status_code == 400


def test_dock_is_on_office_pages_next_to_time_clock(app, world):
    html = login(app, "dennis@example.com").get("/kalender").get_data(as_text=True)
    assert "data-zq-tc" in html and "js/team-chat.js" in html and "data-zs-clock" in html


def test_overview_query_is_valid_for_mariadb(app, world):
    """Produktion laeuft auf MariaDB: kein NULLS LAST (SQLite in den Tests kennt es, MariaDB nicht)."""
    from sqlalchemy.dialects import mysql

    from app.services import team_chat
    from app.tenancy import use_tenant_id

    with use_tenant_id(world.tenant_a.id):
        sql = str(team_chat.chats_query(world.dennis).statement.compile(dialect=mysql.dialect()))
    assert "NULLS" not in sql.upper() and "ORDER BY" in sql


def test_overview_orders_recent_chats_first(app, world):
    dennis = login(app, "dennis@example.com")
    first = _open(dennis, world.laura.id)
    second = _open(dennis, world.admin_a.id)
    _send(dennis, first["id"], "Neueste Nachricht")
    ids = [c["id"] for c in dennis.get(BASE).get_json()["chats"]]
    assert ids == [first["id"], second["id"]]


def test_mobile_header_has_fixed_teamchat_button_on_assistant_and_other_pages(app, world):
    """Handy: Teamchat hat einen festen Platz in der Kopfzeile (nicht schwebend ueber der Begruessung)."""
    client = login(app, "admin-a@example.com")
    for path in ("/zentriq", "/kalender"):
        html = client.get(path).get_data(as_text=True)
        assert 'data-zq-tc-head' in html and 'aria-label="Teamchat"' in html, path
        assert "data-zq-tc-head-count" in html and "data-zq-tc-sheet-close" in html, path


# --- Emojis und Antworten ----------------------------------------------------------------------------

def db_get(message_id):
    from app.extensions import db as _db

    return _db.session.get(TeamChatMessage, message_id)


EMOJI = "Super 👍🏽 danke ❤️ – Team 👩‍💻 🇩🇪"


def test_composed_emojis_survive_send_and_reload(app, world):
    dennis, laura = login(app, "dennis@example.com"), login(app, "laura@example.com")
    chat = _open(dennis, world.laura.id)
    sent = _send(dennis, chat["id"], EMOJI).get_json()["message"]
    assert sent["text"] == EMOJI
    assert laura.get(f"{BASE}/{chat['id']}/nachrichten").get_json()["messages"][-1]["text"] == EMOJI
    with bypass_tenant_scope():
        assert db_get(sent["id"]).text == EMOJI


def test_reply_quotes_original_with_sender_and_excerpt_derived_server_side(app, world):
    dennis, laura = login(app, "dennis@example.com"), login(app, "laura@example.com")
    chat = _open(dennis, world.laura.id)
    first = _send(dennis, chat["id"], "Kannst du den Vertrag von Familie Muster prüfen? " + "x" * 120).get_json()["message"]
    for i in range(3):
        _send(laura, chat["id"], f"Zwischendurch {i}")
    resp = laura.post(f"{BASE}/{chat['id']}/nachrichten",
                      json={"text": "Ja, mache ich 👍", "reply_to": first["id"], "sender": "Gefälscht", "excerpt": "Gefälscht"})
    assert resp.status_code == 201
    reply = resp.get_json()["message"]["reply"]
    assert reply["id"] == first["id"] and reply["sender"] == "Dennis" and reply["mine"] is False
    assert reply["excerpt"].startswith("Kannst du den Vertrag") and reply["excerpt"].endswith("…") and len(reply["excerpt"]) <= 92
    # beim Absender des Originals: "Du"
    mine = [m for m in dennis.get(f"{BASE}/{chat['id']}/nachrichten").get_json()["messages"] if m.get("reply")]
    assert mine[0]["reply"]["sender"] == "Du" and mine[0]["reply"]["mine"] is True
    # alte Nachricht (ohne Antwort) bleibt unveraendert lesbar und zitierbar
    assert "reply" not in dennis.get(f"{BASE}/{chat['id']}/nachrichten").get_json()["messages"][0]


def test_reply_to_foreign_conversation_or_unknown_message_is_rejected(app, world):
    dennis, laura = login(app, "dennis@example.com"), login(app, "laura@example.com")
    admin_a = login(app, "admin-a@example.com")
    private = _open(admin_a, world.laura.id)
    secret = _send(admin_a, private["id"], "Nur fuer Laura").get_json()["message"]
    chat = _open(dennis, world.laura.id)
    for bad in (secret["id"], 999999, "abc"):
        resp = dennis.post(f"{BASE}/{chat['id']}/nachrichten", json={"text": "Antwort", "reply_to": bad})
        assert resp.status_code in (400, 422), bad
        assert "Nur fuer Laura" not in resp.get_data(as_text=True)
    with bypass_tenant_scope():
        assert TeamChatMessage.query.filter_by(chat_id=chat["id"]).count() == 0
    # Laura darf in IHRER Unterhaltung mit Admin A darauf antworten
    assert laura.post(f"{BASE}/{private['id']}/nachrichten", json={"text": "ok", "reply_to": secret["id"]}).status_code == 201


def test_missing_original_shows_unavailable_and_jump_loads_older_history(app, db, world):
    dennis, laura = login(app, "dennis@example.com"), login(app, "laura@example.com")
    chat = _open(dennis, world.laura.id)
    old = _send(dennis, chat["id"], "Ganz alte Frage").get_json()["message"]
    for i in range(60):
        _send(laura, chat["id"], f"Nachricht {i}")
    reply = laura.post(f"{BASE}/{chat['id']}/nachrichten", json={"text": "Zur alten Frage", "reply_to": old["id"]})
    assert reply.status_code == 201
    page = dennis.get(f"{BASE}/{chat['id']}/nachrichten").get_json()["messages"]
    assert old["id"] not in [m["id"] for m in page]  # Original liegt im aelteren Verlauf
    jump = dennis.get(f"{BASE}/{chat['id']}/nachrichten?ab={old['id']}&vor={page[0]['id']}").get_json()["messages"]
    assert jump[0]["id"] == old["id"] and jump[-1]["id"] == page[0]["id"] - 1
    with bypass_tenant_scope():
        TeamChatMessage.query.filter_by(id=old["id"]).delete()
        db.session.commit()
    last = dennis.get(f"{BASE}/{chat['id']}/nachrichten").get_json()["messages"][-1]
    assert last["id"] == reply.get_json()["message"]["id"]
    assert last["reply"] == {"id": old["id"], "missing": True}
