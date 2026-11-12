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
