import re
from tests.conftest import csrf_of, login
from app.models import User
from app.config import now
from datetime import timedelta


def test_passwords_are_hashed(db, mk):
    u = mk.user(pw="Secret123!")
    assert "Secret123!" not in u.password_hash and u.password_hash.startswith("pbkdf2$")


def test_role_protection(db, mk, client):
    mk.user("admin", "a@t.test", "Admin@123"); mk.user("attendee", "u@t.test", "User@1234"); mk.user("staff", "s@t.test", "Staff@123")
    assert client.get("/admin", follow_redirects=False).status_code == 303            # anonymous -> login
    assert client.get("/scanner", follow_redirects=False).status_code == 303
    login(client, "u@t.test", "User@1234")
    assert client.get("/admin").status_code == 403 and client.get("/scanner").status_code == 403
    client.cookies.clear()
    login(client, "s@t.test", "Staff@123")
    assert client.get("/scanner").status_code == 200 and client.get("/admin").status_code == 403
    client.cookies.clear()
    login(client, "a@t.test", "Admin@123")
    assert client.get("/admin").status_code == 200 and client.get("/admin/messages").status_code == 200


def test_csrf_blocks_forged_post(db, mk, client):
    mk.user("attendee", "u@t.test", "User@1234")
    r = client.post("/login", data={"email": "u@t.test", "password": "User@1234"}, follow_redirects=False)
    assert r.status_code == 403


def test_register_and_full_booking_flow_over_http(db, mk, client):
    e = mk.event(mk.venue(), cats=(("G", 100, 5),))
    tok = csrf_of(client, "/register")
    client.post("/register", data={"name": "Zed", "email": "z@t.test", "password": "longenough1", "csrf_token": tok})
    page = client.get(f"/events/{e.id}")
    assert "Pay" in page.text
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post(f"/events/{e.id}/book", data={"category_id": e.categories[0].id, "qty": 2, "csrf_token": tok}, follow_redirects=True)
    assert "SIM-" in r.text and "/qr.png" in r.text
    code = re.search(r"/tickets/([\w-]+)/qr\.png", r.text).group(1)
    assert client.get(f"/tickets/{code}/qr.png").headers["content-type"] == "image/png"


def test_scan_api_and_event_cancel_by_staff_admin(db, mk, client):
    mk.user("staff", "s@t.test", "Staff@123")
    live = mk.event(mk.venue(), start=now() + timedelta(minutes=20), cats=(("G", 1, 3),))
    from app.agents import bus
    buyer = mk.user()
    code = bus.send(db, "t", "BookingAgent", "REQUEST", {"action": "book", "user_id": buyer.id, "event_id": live.id,
                    "items": [{"category_id": live.categories[0].id, "qty": 1}]})["content"]["codes"][0]
    db.commit()
    login(client, "s@t.test", "Staff@123")
    tok = csrf_of(client, "/scanner")
    h = {"X-CSRF-Token": tok}
    assert client.post("/api/scan", json={"code": code, "event_id": live.id}, headers=h).json()["result"] == "admit"
    j = client.post("/api/scan", json={"code": code, "event_id": live.id}, headers=h).json()
    assert j["ok"] is False and j["result"] == "already_used"
    assert client.post("/api/scan", json={"code": code, "event_id": live.id}).status_code == 403   # no CSRF header


def test_http_agent_inbox(db, mk, client):
    u = mk.user()
    body = {"sender": "external", "performative": "REQUEST", "content": {"action": "availability", "category_id": 1}}
    assert client.post("/agents/BookingAgent/inbox", json=body).status_code == 401
    e = mk.event(mk.venue())
    body["content"]["category_id"] = e.categories[0].id
    r = client.post("/agents/BookingAgent/inbox", json=body, headers={"X-Agent-Key": "dev-agent-key"})
    assert r.status_code == 200 and r.json()["content"]["available"] == 10 and r.json()["performative"] == "INFORM"
