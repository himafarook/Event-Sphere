import json
import threading
from datetime import timedelta

from app.agents import bus, waitlist
from app.config import now
from app.models import (Booking, MessageLog, Notification, TicketCategory, Ticket, WaitlistEntry, HireRequest, AgentState)
from app.db import SessionLocal


def send(db, to, content, perf="REQUEST", conv=None):
    r = bus.send(db, "test", to, perf, content, conv)
    db.commit()
    return r


def book(db, u, e, qty=1, cat=None):
    cat = cat or e.categories[0]
    return send(db, "BookingAgent", {"action": "book", "user_id": u.id, "event_id": e.id,
                                     "items": [{"category_id": cat.id, "qty": qty}]})


def at(days, hour):
    return (now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


# ---------------- VenueAgent ----------------
def test_venue_refuses_clash_and_applies_buffer(db, mk):
    v = mk.venue()
    mk.event(v, start=at(10, 18), hours=2)  # 18:00-20:00
    chk = lambda s, e: send(db, "VenueAgent", {"action": "check_slot", "venue_id": v.id, "start": s.isoformat(), "end": e.isoformat()})
    r = chk(at(10, 19), at(10, 21))
    assert r["performative"] == "REFUSE" and "Clashes" in r["content"]["reason"] and r["content"]["next_free_start"]
    assert chk(at(10, 20), at(10, 22))["performative"] == "REFUSE"          # inside 30-min buffer
    assert chk(at(10, 21), at(10, 23))["performative"] == "AGREE"           # after buffer


def test_hire_auto_confirm_and_peak_pricing(db, mk):
    v, u = mk.venue(rate=1000), mk.user()
    d = at(3, 10)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    r = send(db, "VenueAgent", {"action": "hire_request", "user_id": u.id, "venue_id": v.id, "guests": 10,
                                "start": d.isoformat(), "end": (d + timedelta(hours=2)).isoformat()})
    assert r["performative"] == "AGREE" and r["content"]["price"] == 2000
    sat = d + timedelta(days=(5 - d.weekday()) % 7 or 7)
    sat = sat.replace(hour=19)
    r = send(db, "VenueAgent", {"action": "hire_request", "user_id": u.id, "venue_id": v.id, "guests": 10,
                                "start": sat.isoformat(), "end": (sat + timedelta(hours=2)).isoformat()})
    assert r["content"]["price"] == round(2 * 1000 * 1.25 * 1.15, 2)        # weekend + evening peak


def test_hire_counter_proposal_when_clash(db, mk):
    v, v2, u = mk.venue("A"), mk.venue("B"), mk.user()
    mk.event(v, start=at(5, 10), hours=2)
    r = send(db, "VenueAgent", {"action": "hire_request", "user_id": u.id, "venue_id": v.id, "guests": 10,
                                "start": at(5, 10).isoformat(), "end": at(5, 12).isoformat()})
    assert r["performative"] == "PROPOSE" and r["content"]["reason"]
    alts = r["content"]["alternatives"]
    assert any(a["type"] == "other_venue" and a["venue_id"] == v2.id for a in alts) and any(a["type"] == "other_time" for a in alts)


def test_hire_escalation_and_admin_decision(db, mk):
    v, u = mk.venue(capacity=50), mk.user()
    r = send(db, "VenueAgent", {"action": "hire_request", "user_id": u.id, "venue_id": v.id, "guests": 45,
                                "start": at(4, 10).isoformat(), "end": at(4, 12).isoformat()})
    assert r["performative"] == "INFORM" and r["content"]["decision"] == "escalated"
    assert db.query(Notification).filter_by(to_role="admin").count() == 1
    d = send(db, "VenueAgent", {"action": "decide_hire", "hire_id": r["content"]["hire_id"], "approve": True})
    assert d["content"]["decision"] == "confirmed"
    assert db.get(HireRequest, r["content"]["hire_id"]).status == "confirmed"


def test_hire_refused_when_venue_not_open(db, mk):
    v, u = mk.venue(hire=False), mk.user()
    r = send(db, "VenueAgent", {"action": "hire_request", "user_id": u.id, "venue_id": v.id, "guests": 5,
                                "start": at(4, 10).isoformat(), "end": at(4, 12).isoformat()})
    assert r["performative"] == "REFUSE" and "not open" in r["content"]["reason"]


# ---------------- BookingAgent ----------------
def test_booking_agent_refuses_more_than_six(db, mk):
    e, u = mk.event(mk.venue(), cats=(("G", 10, 50),)), mk.user()
    r = book(db, u, e, 7)
    assert r["performative"] == "REFUSE" and "at most 6" in r["content"]["reason"]
    assert book(db, u, e, 6)["performative"] == "AGREE"
    assert book(db, u, e, 1)["performative"] == "REFUSE"                     # per-attendee limit


def test_never_oversells_under_concurrency(db, mk):
    e = mk.event(mk.venue(), cats=(("G", 10, 3),))
    users = [mk.user() for _ in range(12)]
    results = []

    def worker(uid):
        s = SessionLocal()
        try:
            r = bus.send(s, "t", "BookingAgent", "REQUEST", {"action": "book", "user_id": uid, "event_id": e.id,
                                                             "items": [{"category_id": e.categories[0].id, "qty": 1}]})
            results.append(r["performative"])
        finally:
            s.close()

    th = [threading.Thread(target=worker, args=(u.id,)) for u in users]
    [t.start() for t in th]; [t.join() for t in th]
    assert results.count("AGREE") == 3 and results.count("REFUSE") == 9
    db.expire_all()
    cat = db.get(TicketCategory, e.categories[0].id)
    assert cat.sold == 3 and db.query(Ticket).count() == 3


def test_last_seat_two_requests_one_wins(db, mk):
    e = mk.event(mk.venue(), cats=(("G", 10, 1),))
    a, b = mk.user(), mk.user()
    r1, r2 = book(db, a, e), book(db, b, e)
    assert (r1["performative"], r2["performative"]) == ("AGREE", "REFUSE")
    assert "Only 0" in r2["content"]["reason"]


def test_refund_policy_tiers(db, mk):
    u, v = mk.user(), mk.venue()
    expect = [(10, 100.0), (4, 50.0), (1, 0.0)]
    for days, refund in expect:
        e = mk.event(v, start=now() + timedelta(days=days, hours=1), cats=(("G", 100, 5),), title=f"E{days}")
        b = book(db, u, e)
        r = send(db, "BookingAgent", {"action": "cancel", "user_id": u.id, "booking_id": b["content"]["booking_id"]})
        assert r["performative"] == "AGREE" and r["content"]["refund"] == refund


def test_event_cancellation_refunds_everyone(db, mk):
    e = mk.event(mk.venue(), cats=(("G", 200, 5),))
    u1, u2 = mk.user(), mk.user()
    book(db, u1, e, 2); book(db, u2, e, 1)
    e.status = "cancelled"; db.commit()
    r = send(db, "BookingAgent", {"action": "event_cancelled", "event_id": e.id})
    assert r["content"]["bookings_refunded"] == 2 and r["content"]["refunded_total"] == 600
    assert all(b.refund == b.total for b in db.query(Booking))
    assert all(t.status == "cancelled" for t in db.query(Ticket))


# ---------------- TicketAgent ----------------
def _live_event(mk):
    return mk.event(mk.venue(), start=now() + timedelta(minutes=30), cats=(("G", 10, 5),))


def scan(db, code, eid):
    return send(db, "TicketAgent", {"action": "scan", "code": code, "event_id": eid})


def test_ticket_admitted_once_and_double_checkin_rejected(db, mk):
    e, u = _live_event(mk), mk.user()
    code = book(db, u, e)["content"]["codes"][0]
    assert len(code) >= 30
    assert scan(db, code, e.id)["content"]["result"] == "admit"
    r = scan(db, code, e.id)
    assert r["performative"] == "REFUSE" and r["content"]["result"] == "already_used"


def test_ticket_wrong_event_cancelled_unknown_and_early(db, mk):
    v = mk.venue()
    e = _live_event(mk)
    other = mk.event(v, start=now() + timedelta(days=20))
    u = mk.user()
    code = book(db, u, e)["content"]["codes"][0]
    assert scan(db, code, other.id)["content"]["result"] == "wrong_event"
    assert scan(db, "nope", e.id)["content"]["result"] == "unknown"
    far = mk.event(v, start=now() + timedelta(days=15), title="Far")
    c2 = book(db, u, far)["content"]["codes"][0]
    assert scan(db, c2, far.id)["content"]["result"] == "outside_time"
    bid = db.query(Booking).filter(Booking.event_id == e.id).one().id
    send(db, "BookingAgent", {"action": "cancel", "user_id": u.id, "booking_id": bid}) 
    assert scan(db, code, e.id)["content"]["result"] == "cancelled"


def test_suspicious_repeat_scans_flag_admin(db, mk):
    e, u = _live_event(mk), mk.user()
    code = book(db, u, e)["content"]["codes"][0]
    for _ in range(3):
        scan(db, code, e.id)
    assert db.query(AgentState).filter(AgentState.agent == "TicketAgent", AgentState.key.like("flagged:%")).count() == 1
    assert any("Suspicious" in n.text for n in db.query(Notification).filter_by(to_role="admin"))


# ---------------- WaitlistAgent ----------------
def test_waitlist_hold_expiry_and_reoffer_on_timer(db, mk):
    e = mk.event(mk.venue(), cats=(("S", 10, 1),))
    holder, w1, w2 = mk.user(), mk.user(), mk.user()
    b = book(db, holder, e)
    cid = e.categories[0].id
    assert send(db, "WaitlistAgent", {"action": "join", "user_id": w1.id, "event_id": e.id, "category_id": cid, "qty": 1})["content"]["position"] == 1
    assert send(db, "WaitlistAgent", {"action": "join", "user_id": w2.id, "event_id": e.id, "category_id": cid, "qty": 1})["content"]["position"] == 2
    send(db, "BookingAgent", {"action": "cancel", "user_id": holder.id, "booking_id": b["content"]["booking_id"]})
    db.expire_all()
    e1 = db.query(WaitlistEntry).filter_by(user_id=w1.id).one()
    assert e1.status == "offered" and db.get(TicketCategory, cid).held == 1          # FIFO + hold
    # nobody else can take the held seat
    assert book(db, mk.user(), e)["performative"] == "REFUSE"
    e1.offer_expires = now() - timedelta(seconds=1); db.commit()
    assert waitlist.sweep(db) == 1                                                   # timer sweep, no user action
    db.expire_all()
    assert db.query(WaitlistEntry).filter_by(user_id=w1.id).one().status == "expired"
    assert db.query(WaitlistEntry).filter_by(user_id=w2.id).one().status == "offered"  # re-offered
    assert db.get(TicketCategory, cid).held == 1


def test_waitlist_claim_books_held_seat(db, mk):
    e = mk.event(mk.venue(), cats=(("S", 10, 1),))
    holder, w = mk.user(), mk.user()
    b = book(db, holder, e)
    cid = e.categories[0].id
    send(db, "WaitlistAgent", {"action": "join", "user_id": w.id, "event_id": e.id, "category_id": cid, "qty": 1})
    send(db, "BookingAgent", {"action": "cancel", "user_id": holder.id, "booking_id": b["content"]["booking_id"]})
    entry = db.query(WaitlistEntry).filter_by(user_id=w.id).one()
    r = send(db, "WaitlistAgent", {"action": "claim", "user_id": w.id, "entry_id": entry.id})
    assert r["performative"] == "AGREE"
    db.expire_all()
    cat = db.get(TicketCategory, cid)
    assert (cat.sold, cat.held) == (1, 0)


# ---------------- NotificationAgent + traceability ----------------
def test_notification_agent_turns_inform_into_notification(db, mk):
    u = mk.user()
    r = send(db, "NotificationAgent", {"event": "offer_expired", "user_id": u.id, "event_title": "Gig"}, perf="INFORM")
    assert r is None
    n = db.query(Notification).filter_by(user_id=u.id).one()
    assert "Gig" in n.text
    assert send(db, "NotificationAgent", {"event": "offer_expired", "user_id": u.id}, perf="REQUEST")["performative"] == "REFUSE"


def test_one_booking_is_traceable_across_agents_by_conversation_id(db, mk):
    e, u = mk.event(mk.venue()), mk.user()
    conv = book(db, u, e)["conversation_id"]
    rows = db.query(MessageLog).filter_by(conv_id=conv).all()
    agents_seen = {r.receiver for r in rows} | {r.sender for r in rows}
    assert {"BookingAgent", "TicketAgent", "NotificationAgent"} <= agents_seen
    assert {"REQUEST", "AGREE", "INFORM"} <= {r.performative for r in rows}
    assert json.loads(rows[0].content)["action"] == "book"


def test_agent_state_persists_in_database(db, mk):
    e, u = mk.event(mk.venue()), mk.user()
    book(db, u, e, 2)
    db.expire_all()
    assert json.loads(db.get(AgentState, ("BookingAgent", "tickets_sold")).value) == 2
