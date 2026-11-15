from datetime import timedelta
from .auth import hash_password
from .config import now
from .models import User, Venue, Event, TicketCategory


def seed(db):
    if db.query(User).count():
        return
    for name, email, pw, role in [("Admin", "admin@eventsphere.test", "Admin@123", "admin"),
                                  ("Gate Staff", "staff@eventsphere.test", "Staff@123", "staff"),
                                  ("Alice Attendee", "attendee@eventsphere.test", "Attendee@123", "attendee"),
                                  ("Bob Attendee", "bob@eventsphere.test", "Attendee@123", "attendee")]:
        db.add(User(name=name, email=email, password_hash=hash_password(pw), role=role))
    aud = Venue(name="Main Auditorium", location="Block A, Ground floor", capacity=300, hourly_rate=15000,
                open_for_hire=True, description="Tiered auditorium with stage, PA and projection.")
    room = Venue(name="Seminar Room B", location="Block C, Level 2", capacity=40, hourly_rate=3000,
                 open_for_hire=True, description="Flexible room for workshops and meetings.")
    hall = Venue(name="Garden Pavilion", location="Central lawn", capacity=120, hourly_rate=8000,
                 open_for_hire=True, description="Open-sided pavilion, ideal for receptions and farewells.")
    lab = Venue(name="Computing Lab", location="Block D", capacity=60, hourly_rate=0, open_for_hire=False,
                description="Teaching lab - not available for private hire.")
    db.add_all([aud, room, hall, lab])
    db.flush()
    t = now().replace(minute=0, second=0, microsecond=0)

    def ev(title, cat, venue, start, hours, desc, cats, status="published"):
        e = Event(title=title, category=cat, venue_id=venue.id, start=start, end=start + timedelta(hours=hours),
                  description=desc, status=status)
        e.categories = [TicketCategory(name=n, price=p, quantity=q) for n, p, q in cats]
        db.add(e)

    ev("Live Demo Workshop (check-in open now)", "Workshop", room, t + timedelta(hours=1), 2,
       "Starts within the check-in window so the scanner can admit tickets during the viva.",
       [("Standard", 500, 30)])
    ev("Campus Rock Night", "Concert", aud, (t + timedelta(days=9)).replace(hour=18), 3,
       "Student bands live on the main stage.", [("General", 2000, 200), ("VIP", 5000, 40)])
    ev("AI in Education Seminar", "Seminar", room, (t + timedelta(days=4)).replace(hour=10), 2,
       "Invited talks and Q&A on AI in teaching.", [("Student", 0, 25), ("Public", 800, 10)])
    ev("Sold-out Demo Talk (waitlist demo)", "Seminar", hall, (t + timedelta(days=12)).replace(hour=15), 2,
       "Only 2 seats - sell them out, then join the waitlist and cancel to see timed holds.",
       [("Seat", 1000, 2)])
    ev("Draft: Spring Conference", "Conference", aud, (t + timedelta(days=30)).replace(hour=9), 8,
       "Unpublished draft event.", [("Delegate", 4500, 250)], status="draft")
    db.commit()
