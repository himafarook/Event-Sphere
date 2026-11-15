import threading
import uuid
from sqlalchemy import func
from .. import config
from ..config import now
from ..models import Event, TicketCategory, Booking, Ticket
from .core import Agent

# Seat allocation is serialised in-process by SEAT_LOCK *and* across processes by
# SELECT ... FOR UPDATE row locks (PostgreSQL). The lock is held until COMMIT.
SEAT_LOCK = threading.RLock()


def _cat(db, cid):
    return (db.query(TicketCategory).filter(TicketCategory.id == cid)
            .with_for_update().populate_existing().one_or_none())


class BookingAgent(Agent):
    """Goal: sell tickets fairly without overselling."""
    name = "BookingAgent"

    def _tx(self, db, fn):
        with SEAT_LOCK:
            try:
                out = fn()
                db.commit()
                return out
            except Exception:
                db.rollback()
                raise

    def _no(self, db, reason, **extra):
        self.incr(db, "refused")
        return ("REFUSE", {"reason": reason, **extra})

    @staticmethod
    def refund_for(total, start):
        hrs = (start - now()).total_seconds() / 3600
        if hrs >= 168:
            return total, 100, "Cancelled 7+ days before the event: 100% refund"
        if hrs >= 48:
            return round(total * 0.5, 2), 50, "Cancelled 2-7 days before the event: 50% refund"
        return 0.0, 0, "Cancelled under 48 hours before the event: no refund"

    # ---- reads ----------------------------------------------------------------
    def on_availability(self, db, c, msg):
        cat = db.query(TicketCategory).filter(TicketCategory.id == c["category_id"]).populate_existing().one_or_none()
        if not cat:
            return ("REFUSE", {"reason": "Unknown ticket category"})
        return ("INFORM", {"available": cat.quantity - cat.sold - cat.held})

    def on_checked_in(self, db, c, msg):
        self.incr(db, f"checkins:{c['event_id']}")

    # ---- holds for the WaitlistAgent -----------------------------------------
    def on_hold(self, db, c, msg):
        def run():
            cat = _cat(db, c["category_id"])
            if not cat or cat.quantity - cat.sold - cat.held < c["qty"]:
                return ("REFUSE", {"reason": "Not enough free seats to hold"})
            cat.held += c["qty"]
            db.flush()
            self.incr(db, "holds_placed")
            return ("AGREE", {"held": c["qty"]})
        return self._tx(db, run)

    def on_release_hold(self, db, c, msg):
        def run():
            cat = _cat(db, c["category_id"])
            cat.held = max(0, cat.held - c["qty"])
            db.flush()
            self.incr(db, "holds_released")
            return ("AGREE", {"released": c["qty"]})
        return self._tx(db, run)

    # ---- booking --------------------------------------------------------------
    def on_book(self, db, c, msg):
        return self._tx(db, lambda: self._book(db, c, msg))

    def _book(self, db, c, msg):
        uid, eid, conv = c["user_id"], c["event_id"], msg["conversation_id"]
        hold = c.get("hold_entry")
        merged = {}
        for it in c["items"]:
            if int(it["qty"]) > 0:
                merged[int(it["category_id"])] = merged.get(int(it["category_id"]), 0) + int(it["qty"])
        ev = db.get(Event, eid)
        if not merged:
            return self._no(db, "Choose at least one ticket")
        if not ev or ev.status != "published":
            return self._no(db, "Event is not open for booking")
        if now() >= ev.start:
            return self._no(db, "Sales are closed: the event has already started")
        total_qty = sum(merged.values())
        if not hold:
            if total_qty > config.MAX_TICKETS:
                return self._no(db, f"You can buy at most {config.MAX_TICKETS} tickets per request")
            have = (db.query(func.count(Ticket.id)).join(Booking, Ticket.booking_id == Booking.id)
                    .filter(Booking.user_id == uid, Booking.event_id == eid, Ticket.status != "cancelled").scalar())
            if have + total_qty > config.MAX_TICKETS:
                return self._no(db, f"Limit is {config.MAX_TICKETS} tickets per attendee per event (you already hold {have})")
        lines, total = [], 0.0
        for cid, qty in merged.items():
            cat = _cat(db, cid)
            if not cat or cat.event_id != eid:
                return self._no(db, "Unknown ticket category for this event")
            if hold:
                if cat.held < qty:
                    return self._no(db, "The seat hold is no longer valid")
            elif qty > cat.available:
                return self._no(db, f"Only {max(cat.available, 0)} '{cat.name}' ticket(s) left", available=max(cat.available, 0))
            lines.append((cat, qty))
            total += cat.price * qty
        ref = "SIM-" + uuid.uuid4().hex[:10].upper()   # simulated payment
        b = Booking(user_id=uid, event_id=eid, total=total, payment_ref=ref)
        db.add(b)
        db.flush()
        for cat, qty in lines:
            if hold:
                cat.held -= qty
            cat.sold += qty
        db.flush()
        r = self.send(db, "TicketAgent", "REQUEST",
                      {"action": "issue", "booking_id": b.id, "event_id": eid,
                       "items": [{"category_id": k.id, "qty": q} for k, q in lines]}, conv)
        if not r or r["performative"] != "AGREE":
            for cat, qty in lines:
                cat.sold -= qty
            b.status = "cancelled"
            return ("FAILURE", {"reason": "Ticket issuing failed; nothing was charged"})
        self.incr(db, "bookings_accepted")
        self.incr(db, "tickets_sold", total_qty)
        self.send(db, "NotificationAgent", "INFORM",
                  {"event": "booking_confirmed", "user_id": uid, "booking_id": b.id,
                   "event_title": ev.title, "tickets": total_qty, "payment_ref": ref}, conv)
        return ("AGREE", {"booking_id": b.id, "payment_ref": ref, "total": total, "codes": r["content"]["codes"]})

    # ---- cancellation ---------------------------------------------------------
    def on_cancel(self, db, c, msg):
        freed = []
        out = self._tx(db, lambda: self._cancel(db, c, msg, freed))
        if out[0] == "AGREE":
            for cid in freed:
                self.send(db, "WaitlistAgent", "INFORM", {"event": "seats_freed", "category_id": cid},
                          msg["conversation_id"])
            db.commit()
        return out

    def _cancel(self, db, c, msg, freed):
        b = db.get(Booking, c["booking_id"])
        if not b or b.user_id != c["user_id"]:
            return self._no(db, "Booking not found")
        if b.status != "confirmed":
            return self._no(db, "Booking is already cancelled")
        if any(t.status == "used" for t in b.tickets):
            return self._no(db, "Cannot cancel: a ticket has already been used for entry")
        if now() >= b.event.start:
            return self._no(db, "Cannot cancel after the event has started")
        refund, pct, rule = self.refund_for(b.total, b.event.start)
        counts = {}
        for t in b.tickets:
            if t.status == "valid":
                counts[t.category_id] = counts.get(t.category_id, 0) + 1
        for cid, n in counts.items():
            _cat(db, cid).sold -= n
            freed.append(cid)
        self.send(db, "TicketAgent", "REQUEST", {"action": "void", "booking_id": b.id}, msg["conversation_id"])
        b.status, b.refund, b.refund_rule, b.cancelled_at = "cancelled", refund, rule, now()
        db.flush()
        self.incr(db, "cancellations")
        self.send(db, "NotificationAgent", "INFORM",
                  {"event": "booking_cancelled", "user_id": b.user_id, "booking_id": b.id,
                   "refund": refund, "rule": rule}, msg["conversation_id"])
        return ("AGREE", {"refund": refund, "percent": pct, "rule": rule})

    def on_event_cancelled(self, db, c, msg):
        return self._tx(db, lambda: self._event_cancelled(db, c, msg))

    def _event_cancelled(self, db, c, msg):
        ev, conv, n, total = db.get(Event, c["event_id"]), msg["conversation_id"], 0, 0.0
        for b in db.query(Booking).filter_by(event_id=ev.id, status="confirmed").all():
            b.status, b.refund, b.cancelled_at = "cancelled", b.total, now()
            b.refund_rule = "Event cancelled by organiser: 100% refund"
            self.send(db, "TicketAgent", "REQUEST", {"action": "void", "booking_id": b.id}, conv)
            self.send(db, "NotificationAgent", "INFORM",
                      {"event": "event_cancelled_refund", "user_id": b.user_id,
                       "event_title": ev.title, "refund": b.total}, conv)
            n, total = n + 1, total + b.total
        for k in ev.categories:
            cat = _cat(db, k.id)
            cat.sold, cat.held = 0, 0
        db.flush()
        self.send(db, "WaitlistAgent", "INFORM", {"event": "event_cancelled", "event_id": ev.id}, conv)
        self.incr(db, "event_cancellations")
        return ("AGREE", {"bookings_refunded": n, "refunded_total": total})
