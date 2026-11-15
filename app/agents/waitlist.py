import uuid
from datetime import timedelta
from .. import config
from ..config import now
from ..models import WaitlistEntry, Event, TicketCategory
from .core import Agent


class WaitlistAgent(Agent):
    """Goal: pass freed seats to waiting customers (FIFO), hold them, expire unclaimed holds."""
    name = "WaitlistAgent"

    def on_join(self, db, c, msg):
        ev, cat = db.get(Event, c["event_id"]), db.get(TicketCategory, c["category_id"])
        qty = int(c["qty"])
        if not ev or ev.status != "published" or ev.start <= now():
            return ("REFUSE", {"reason": "Event is not open for booking"})
        if not cat or cat.event_id != ev.id:
            return ("REFUSE", {"reason": "Unknown ticket category"})
        if not 1 <= qty <= config.MAX_TICKETS:
            return ("REFUSE", {"reason": f"Waitlist requests must be for 1-{config.MAX_TICKETS} tickets"})
        r = self.send(db, "BookingAgent", "REQUEST", {"action": "availability", "category_id": cat.id}, msg["conversation_id"])
        if r and r["content"].get("available", 0) >= qty:
            return ("REFUSE", {"reason": "Seats are available right now - please book directly"})
        if db.query(WaitlistEntry).filter(WaitlistEntry.user_id == c["user_id"], WaitlistEntry.category_id == cat.id,
                                          WaitlistEntry.status.in_(["waiting", "offered"])).first():
            return ("REFUSE", {"reason": "You are already on the waitlist for this category"})
        e = WaitlistEntry(event_id=ev.id, category_id=cat.id, user_id=c["user_id"], qty=qty)
        db.add(e)
        db.flush()
        ahead = db.query(WaitlistEntry).filter(WaitlistEntry.category_id == cat.id, WaitlistEntry.status == "waiting",
                                               WaitlistEntry.id < e.id).count()
        self.incr(db, "joined")
        return ("AGREE", {"entry_id": e.id, "position": ahead + 1})

    def on_seats_freed(self, db, c, msg):
        self._allocate(db, c["category_id"], msg["conversation_id"])

    def _allocate(self, db, cid, conv):
        while True:
            r = self.send(db, "BookingAgent", "REQUEST", {"action": "availability", "category_id": cid}, conv)
            avail = r["content"].get("available", 0) if r else 0
            if avail <= 0:
                break
            q = (db.query(WaitlistEntry).filter(WaitlistEntry.category_id == cid, WaitlistEntry.status == "waiting")
                 .order_by(WaitlistEntry.created, WaitlistEntry.id))
            e = next((x for x in q if x.qty <= avail), None)   # FIFO among those whose request fits
            if not e:
                break
            h = self.send(db, "BookingAgent", "REQUEST", {"action": "hold", "category_id": cid, "qty": e.qty}, conv)
            if not h or h["performative"] != "AGREE":
                break
            e.status = "offered"
            e.offer_expires = now() + timedelta(minutes=config.HOLD_MINUTES)
            db.flush()
            self.incr(db, "offers_made")
            self.send(db, "NotificationAgent", "INFORM",
                      {"event": "waitlist_offer", "user_id": e.user_id, "qty": e.qty, "category": e.category.name,
                       "event_title": e.event.title, "expires": f"{e.offer_expires:%H:%M}", "minutes": config.HOLD_MINUTES}, conv)
        db.commit()

    # ---- proactive behaviour: called by the timer, no user action ---------------
    def sweep(self, db, conv=None):
        conv = conv or "sweep-" + uuid.uuid4().hex[:8]
        t = now()
        expired = db.query(WaitlistEntry).filter(WaitlistEntry.status == "offered", WaitlistEntry.offer_expires <= t).all()
        cats = set()
        for e in expired:
            e.status = "expired"
            db.flush()
            self.send(db, "BookingAgent", "REQUEST", {"action": "release_hold", "category_id": e.category_id, "qty": e.qty}, conv)
            self.send(db, "NotificationAgent", "INFORM",
                      {"event": "offer_expired", "user_id": e.user_id, "event_title": e.event.title}, conv)
            cats.add(e.category_id)
        self.put(db, "last_sweep", t.isoformat())
        self.incr(db, "sweeps")
        if expired:
            self.incr(db, "holds_expired", len(expired))
        db.commit()
        for cid in cats:
            self._allocate(db, cid, conv)   # re-offer to the next person in line
        return len(expired)

    def on_sweep(self, db, c, msg):
        return ("INFORM", {"expired": self.sweep(db, msg["conversation_id"])})

    def on_claim(self, db, c, msg):
        e = db.get(WaitlistEntry, c["entry_id"])
        if not e or e.user_id != c["user_id"] or e.status != "offered":
            return ("REFUSE", {"reason": "No active offer found"})
        if e.offer_expires <= now():
            return ("REFUSE", {"reason": "This offer has expired"})
        r = self.send(db, "BookingAgent", "REQUEST",
                      {"action": "book", "user_id": e.user_id, "event_id": e.event_id, "hold_entry": e.id,
                       "items": [{"category_id": e.category_id, "qty": e.qty}]}, msg["conversation_id"])
        if r and r["performative"] == "AGREE":
            e.status = "claimed"
            db.flush()
            self.incr(db, "claimed")
            return ("AGREE", r["content"])
        return ("REFUSE", {"reason": (r or {}).get("content", {}).get("reason", "Booking failed")})

    def on_leave(self, db, c, msg):
        e = db.get(WaitlistEntry, c["entry_id"])
        if not e or e.user_id != c["user_id"] or e.status not in ("waiting", "offered"):
            return ("REFUSE", {"reason": "Nothing to leave"})
        was_offered = e.status == "offered"
        e.status = "cancelled"
        db.flush()
        if was_offered:
            self.send(db, "BookingAgent", "REQUEST", {"action": "release_hold", "category_id": e.category_id, "qty": e.qty}, msg["conversation_id"])
            self._allocate(db, e.category_id, msg["conversation_id"])
        return ("AGREE", {"left": True})

    def on_event_cancelled(self, db, c, msg):
        for e in db.query(WaitlistEntry).filter(WaitlistEntry.event_id == c["event_id"],
                                                WaitlistEntry.status.in_(["waiting", "offered"])):
            e.status = "cancelled"
            self.send(db, "NotificationAgent", "INFORM",
                      {"event": "waitlist_event_cancelled", "user_id": e.user_id, "event_title": e.event.title},
                      msg["conversation_id"])
        db.flush()
