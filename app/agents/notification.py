from .. import config
from ..models import Notification, User
from .core import Agent


class _D(dict):
    def __missing__(self, k):
        return ""


# event -> (audience, template).  This table IS the agent's decision rule (N1-N3).
TEMPLATES = {
    "booking_confirmed": ("user", "Booking #{booking_id} confirmed: {tickets} ticket(s) for {event_title}. Payment ref {payment_ref}."),
    "booking_cancelled": ("user", "Booking #{booking_id} cancelled. Refund LKR {refund} - {rule}."),
    "event_cancelled_refund": ("user", "'{event_title}' was cancelled by the organiser. You have been fully refunded LKR {refund}."),
    "waitlist_offer": ("user", "Seats free! {qty} x {category} for '{event_title}' are held for you until {expires} ({minutes} min). Claim them in My waitlist."),
    "offer_expired": ("user", "Your held seats for '{event_title}' expired and were released to the next person."),
    "waitlist_event_cancelled": ("user", "'{event_title}' was cancelled; your waitlist entry was removed."),
    "hire_confirmed": ("user", "Venue hire confirmed: {venue} on {start}, LKR {price}."),
    "hire_escalated": ("user", "Your hire request for {venue} on {start} needs admin approval. Quoted price LKR {price}."),
    "hire_decided": ("user", "Your hire request for {venue} on {start} was {status}. {note}"),
    "hire_needs_approval": ("admin", "Hire request #{hire_id} for {venue} on {start} needs your approval."),
    "suspicious_scan": ("admin", "Suspicious: ticket #{ticket_id} for '{event_title}' has been scanned {scans} times (shared screenshot?)."),
}


class NotificationAgent(Agent):
    """Goal: tell the right person what affects them. Purely reactive listener of INFORM events."""
    name = "NotificationAgent"

    def handle(self, db, msg):
        if msg["performative"] != "INFORM":
            return ("REFUSE", {"reason": "NotificationAgent only accepts INFORM events"})
        c = msg["content"]
        rule = TEMPLATES.get(c.get("event"))
        if not rule:
            return ("REFUSE", {"reason": f"No rule for event '{c.get('event')}'"})
        audience, tpl = rule
        data = _D(c)
        if isinstance(c.get("refund"), (int, float)):
            data["refund"] = f"{c['refund']:,.2f}"
        text = tpl.format_map(data)
        if audience == "admin":
            db.add(Notification(to_role="admin", text=text))
        else:
            db.add(Notification(user_id=c["user_id"], text=text))
            self._email(db, c["user_id"], text)
        db.flush()
        self.incr(db, "sent")
        return None

    def _email(self, db, uid, text):
        if not config.SMTP_HOST:
            return
        try:
            import smtplib
            from email.message import EmailMessage
            u = db.get(User, uid)
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = config.SMTP_FROM, u.email, "EventSphere notification"
            m.set_content(text)
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
                s.starttls()
                if config.SMTP_USER:
                    s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(m)
            self.incr(db, "emails_sent")
        except Exception:
            self.incr(db, "emails_failed")
