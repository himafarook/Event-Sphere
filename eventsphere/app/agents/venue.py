from datetime import datetime, timedelta
from .. import config
from ..config import now
from ..models import Venue, Event, HireRequest
from .core import Agent


def _dt(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


def _fmt(d):
    return d.strftime("%d %b %H:%M")


class VenueAgent(Agent):
    """Goal: keep every venue calendar free of clashes.
    Rules: see docs/REPORT.md section 4 (V1-V10)."""
    name = "VenueAgent"

    # ---- helpers ------------------------------------------------------------
    def _conflicts(self, db, vid, s, e, exclude_event=None, exclude_hire=None):
        buf = timedelta(minutes=config.BUFFER_MINUTES)
        lo, hi = s - buf, e + buf
        out = []
        q = db.query(Event).filter(Event.venue_id == vid, Event.status != "cancelled",
                                   Event.start < hi, Event.end > lo)
        if exclude_event:
            q = q.filter(Event.id != exclude_event)
        out += [{"kind": "event", "title": x.title, "start": x.start, "end": x.end} for x in q]
        q = db.query(HireRequest).filter(HireRequest.venue_id == vid,
                                         HireRequest.status.in_(["confirmed", "pending"]),
                                         HireRequest.start < hi, HireRequest.end > lo)
        if exclude_hire:
            q = q.filter(HireRequest.id != exclude_hire)
        out += [{"kind": "hire", "title": "Private hire", "start": x.start, "end": x.end} for x in q]
        return out

    def _next_free(self, db, vid, s, dur, exclude_event=None):
        for _ in range(50):
            cl = self._conflicts(db, vid, s, s + dur, exclude_event)
            if not cl:
                return s
            s = max(c["end"] for c in cl) + timedelta(minutes=config.BUFFER_MINUTES)
        return s

    def _price(self, v, s, e):
        hours = (e - s).total_seconds() / 3600
        mult, notes = 1.0, []
        if s.weekday() >= 5:
            mult *= 1.25
            notes.append("weekend +25%")
        if 18 <= s.hour < 22:
            mult *= 1.15
            notes.append("evening peak +15%")
        return round(hours * v.hourly_rate * mult, 2), notes

    def _alts(self, db, v, s, e, guests, same_venue=True):
        dur, alts = e - s, []
        if same_venue:
            for k in range(1, 13):
                for sign in (1, -1):
                    ns = s + sign * timedelta(hours=k)
                    if ns < now() + timedelta(hours=24) or len(alts) >= 3:
                        continue
                    if not self._conflicts(db, v.id, ns, ns + dur):
                        alts.append({"type": "other_time", "venue_id": v.id, "venue": v.name,
                                     "start": ns.isoformat(), "end": (ns + dur).isoformat(),
                                     "price": self._price(v, ns, ns + dur)[0]})
        others = db.query(Venue).filter(Venue.open_for_hire.is_(True), Venue.capacity >= guests,
                                        Venue.id != v.id).all()
        n = 0
        for o in others:
            if n < 3 and not self._conflicts(db, o.id, s, e):
                alts.append({"type": "other_venue", "venue_id": o.id, "venue": o.name,
                             "start": s.isoformat(), "end": e.isoformat(),
                             "price": self._price(o, s, e)[0]})
                n += 1
        return alts

    # ---- event slot ---------------------------------------------------------
    def on_check_slot(self, db, c, msg):
        v = db.get(Venue, c["venue_id"])
        s, e = _dt(c["start"]), _dt(c["end"])
        self.incr(db, "slot_checks")
        if not v:
            return ("REFUSE", {"reason": "Venue does not exist"})
        if e <= s:
            return ("REFUSE", {"reason": "End time must be after start time"})
        cl = self._conflicts(db, v.id, s, e, c.get("exclude_event_id"))
        if cl:
            self.incr(db, "slot_refusals")
            nxt = self._next_free(db, v.id, s, e - s, c.get("exclude_event_id"))
            x = cl[0]
            return ("REFUSE", {
                "reason": f"Clashes with '{x['title']}' ({_fmt(x['start'])}-{_fmt(x['end'])}); "
                          f"a {config.BUFFER_MINUTES}-minute turnaround buffer applies. "
                          f"Next free start: {_fmt(nxt)}",
                "conflicts": [{**k, "start": k["start"].isoformat(), "end": k["end"].isoformat()} for k in cl],
                "next_free_start": nxt.isoformat()})
        return ("AGREE", {"reason": "Slot is free (including turnaround buffer)"})

    # ---- private hire -------------------------------------------------------
    def on_hire_request(self, db, c, msg):
        v = db.get(Venue, c["venue_id"])
        s, e, guests = _dt(c["start"]), _dt(c["end"]), int(c["guests"])
        self.incr(db, "hire_requests")

        def refuse(r):
            self.incr(db, "hire_refusals")
            return ("REFUSE", {"reason": r})

        def propose(r, alts):
            self.incr(db, "hire_counter_proposals")
            return ("PROPOSE", {"reason": r, "alternatives": alts})

        if not v:
            return refuse("Venue does not exist")
        if not v.open_for_hire:
            return refuse(f"{v.name} is not open for private hire")
        hours = (e - s).total_seconds() / 3600
        if hours < 1 or hours > 12:
            return refuse("Hire duration must be between 1 and 12 hours")
        if s < now() + timedelta(hours=24):
            return refuse("Hire requests need at least 24 hours notice")
        if guests < 1:
            return refuse("Guest count must be at least 1")
        if guests > v.capacity:
            alts = self._alts(db, v, s, e, guests, same_venue=False)
            r = f"{guests} guests exceed the capacity of {v.name} ({v.capacity})"
            return propose(r, alts) if alts else refuse(r)
        cl = self._conflicts(db, v.id, s, e)
        if cl:
            alts = self._alts(db, v, s, e, guests)
            r = f"{v.name} is not free at that time (clashes with {cl[0]['title']})"
            return propose(r, alts) if alts else refuse(r + "; no alternatives found")
        price, notes = self._price(v, s, e)
        escalate = guests > 0.8 * v.capacity or hours > 6
        h = HireRequest(user_id=c["user_id"], venue_id=v.id, start=s, end=e, guests=guests,
                        purpose=c.get("purpose", ""), price=price,
                        status="pending" if escalate else "confirmed")
        db.add(h)
        db.flush()
        conv = msg["conversation_id"]
        base = {"venue": v.name, "start": _fmt(s), "price": price, "hire_id": h.id}
        if escalate:
            self.incr(db, "hire_escalations")
            self.send(db, "NotificationAgent", "INFORM",
                      {"event": "hire_escalated", "user_id": c["user_id"], **base}, conv)
            self.send(db, "NotificationAgent", "INFORM", {"event": "hire_needs_approval", **base}, conv)
            return ("INFORM", {"decision": "escalated", "hire_id": h.id, "price": price, "pricing": notes,
                               "reason": "Large group (>80% of capacity) or long booking (>6h): admin approval needed"})
        self.incr(db, "hire_auto_confirmed")
        self.send(db, "NotificationAgent", "INFORM", {"event": "hire_confirmed", "user_id": c["user_id"], **base}, conv)
        return ("AGREE", {"decision": "confirmed", "hire_id": h.id, "price": price, "pricing": notes})

    def on_decide_hire(self, db, c, msg):
        h = db.get(HireRequest, c["hire_id"])
        if not h or h.status != "pending":
            return ("REFUSE", {"reason": "Request is not pending"})
        if c.get("approve"):
            if self._conflicts(db, h.venue_id, h.start, h.end, exclude_hire=h.id):
                return ("REFUSE", {"reason": "Calendar changed: the slot is no longer free"})
            h.status = "confirmed"
        else:
            h.status = "rejected"
        h.note = c.get("note", "")
        db.flush()
        self.incr(db, "hire_decided_by_admin")
        self.send(db, "NotificationAgent", "INFORM",
                  {"event": "hire_decided", "user_id": h.user_id, "venue": h.venue.name,
                   "start": _fmt(h.start), "status": h.status, "note": h.note}, msg["conversation_id"])
        return ("AGREE", {"decision": h.status})
