import secrets
from datetime import timedelta
from .. import config
from ..config import now
from ..models import Ticket, Booking
from .core import Agent

SUSPICIOUS_AFTER = 3  # total scans of one code before it is flagged


class TicketAgent(Agent):
    """Goal: admit each ticket exactly once."""
    name = "TicketAgent"

    def on_issue(self, db, c, msg):
        codes = []
        for it in c["items"]:
            for _ in range(int(it["qty"])):
                code = secrets.token_urlsafe(24)  # 192 bits, unguessable
                db.add(Ticket(booking_id=c["booking_id"], event_id=c["event_id"],
                              category_id=it["category_id"], code=code))
                codes.append(code)
        db.flush()
        self.incr(db, "issued", len(codes))
        return ("AGREE", {"codes": codes})

    def on_void(self, db, c, msg):
        n = 0
        for t in db.query(Ticket).filter(Ticket.booking_id == c["booking_id"], Ticket.status == "valid"):
            t.status = "cancelled"
            n += 1
        db.flush()
        self.incr(db, "voided", n)
        return ("AGREE", {"voided": n})

    def on_scan(self, db, c, msg):
        code, eid = c["code"].strip(), c["event_id"]
        t = db.query(Ticket).filter(Ticket.code == code).one_or_none()
        if not t:
            self.incr(db, "scan_unknown")
            return ("REFUSE", {"result": "unknown", "reason": "Unknown ticket code"})
        scans = self.get(db, f"scans:{t.id}", 0) + 1
        self.put(db, f"scans:{t.id}", scans)  # persistent belief about this ticket
        ev = t.event

        def no(result, reason):
            self.incr(db, f"scan_{result}")
            return ("REFUSE", {"result": result, "reason": reason, "ticket_id": t.id})

        if t.status == "cancelled":
            return no("cancelled", "Ticket was cancelled/refunded")
        if t.event_id != eid:
            return no("wrong_event", f"Ticket is for a different event: '{ev.title}'")
        if t.status == "used":
            if scans >= SUSPICIOUS_AFTER and not self.get(db, f"flagged:{t.id}", False):
                self.put(db, f"flagged:{t.id}", True)
                self.incr(db, "suspicious_flags")
                self.send(db, "NotificationAgent", "INFORM",
                          {"event": "suspicious_scan", "ticket_id": t.id, "event_title": ev.title, "scans": scans},
                          msg["conversation_id"])
            return no("already_used", f"Already admitted at {t.used_at:%H:%M:%S} (scan #{scans} of this code)")
        opens = ev.start - timedelta(hours=config.CHECKIN_OPEN_HOURS)
        if now() < opens or now() > ev.end:
            return no("outside_time", f"Check-in is open from {opens:%d %b %H:%M} until {ev.end:%d %b %H:%M}")
        t.status, t.used_at = "used", now()
        db.flush()
        self.incr(db, "admitted")
        self.send(db, "BookingAgent", "INFORM", {"event": "checked_in", "event_id": t.event_id},
                  msg["conversation_id"])
        b = db.get(Booking, t.booking_id)
        return ("AGREE", {"result": "admit", "holder": b.user.name, "category": t.category.name})
