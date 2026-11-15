import os
import re
import tempfile
from datetime import timedelta

_f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = "sqlite:///" + _f.name
os.environ["SEED_DEMO"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.config import now  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User, Venue, Event, TicketCategory  # noqa: E402
from app import agents  # noqa: E402,F401


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = SessionLocal()
    yield s
    s.close()


class Factory:
    def __init__(self, db):
        self.db = db

    def user(self, role="attendee", email=None, pw="Passw0rd!"):
        u = User(name=role.title(), email=email or f"{role}{User.__table__.name}{os.urandom(3).hex()}@t.test",
                 password_hash=hash_password(pw), role=role)
        self.db.add(u); self.db.commit()
        return u

    def venue(self, name="Hall", capacity=100, hire=True, rate=1000):
        v = Venue(name=name, capacity=capacity, open_for_hire=hire, hourly_rate=rate, location="X")
        self.db.add(v); self.db.commit()
        return v

    def event(self, venue, start=None, hours=2, cats=(("General", 100, 10),), status="published", title="Gig"):
        start = start or now() + timedelta(days=10)
        e = Event(title=title, category="Concert", venue_id=venue.id, start=start, end=start + timedelta(hours=hours), status=status)
        e.categories = [TicketCategory(name=n, price=p, quantity=q) for n, p, q in cats]
        self.db.add(e); self.db.commit()
        return e


@pytest.fixture()
def mk(db):
    return Factory(db)


@pytest.fixture()
def client(db):
    return TestClient(app)


def csrf_of(client, path="/login"):
    html = client.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, email, pw):
    tok = csrf_of(client)
    return client.post("/login", data={"email": email, "password": pw, "csrf_token": tok}, follow_redirects=False)
