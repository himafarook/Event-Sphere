import asyncio
import hmac
import io
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import qrcode
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .agents import bus, waitlist
from .auth import (NeedLogin, csrf_token, get_user, hash_password, require, verify_csrf, verify_password)
from .config import now
from .db import Base, SessionLocal, engine, get_db
from .models import (AgentState, Booking, Event, HireRequest, MessageLog, Notification, Ticket, TicketCategory,
                     User, Venue, WaitlistEntry)
from .seed import seed

BASE = Path(__file__).parent
UPLOADS = BASE / "static" / "uploads"
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = lambda v: f"{float(v or 0):,.2f}"
templates.env.filters["dt"] = lambda d: d.strftime("%a %d %b %Y, %H:%M") if d else ""
templates.env.filters["inp"] = lambda d: d.strftime("%Y-%m-%dT%H:%M") if d else ""


def run_sweep():
    with SessionLocal() as db:
        waitlist.sweep(db)


async def _ticker():
    while True:  # proactive behaviour: the WaitlistAgent wakes itself up
        await asyncio.sleep(config.SWEEP_SECONDS)
        try:
            await asyncio.to_thread(run_sweep)
        except Exception as exc:  # never kill the timer
            print("sweep error", exc)


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    if config.SEED_DEMO:
        with SessionLocal() as db:
            seed(db)
    task = asyncio.create_task(_ticker())
    yield
    task.cancel()


app = FastAPI(title="EventSphere", lifespan=lifespan, dependencies=[Depends(verify_csrf)])
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax",
                   https_only=config.COOKIE_SECURE, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def go(url):
    return RedirectResponse(url, status_code=303)


def flash(request, msg, kind="info"):
    request.session.setdefault("flash", []).append([kind, msg])


def render(request, name, user=None, status_code=200, **ctx):
    fl = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "csrf": csrf_token(request), "flashes": fl,
                                                      "now": now(), "categories": config.CATEGORIES, **ctx},
                                      status_code=status_code)


@app.exception_handler(NeedLogin)
async def _need_login(request, exc):
    return go("/login?next=" + request.url.path)


@app.exception_handler(StarletteHTTPException)
async def _http_exc(request, exc):
    if request.url.path.startswith(("/api", "/agents")):
        return Response(f'{{"detail": "{exc.detail}"}}', status_code=exc.status_code, media_type="application/json")
    return render(request, "error.html", None, status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------ auth
@app.get("/register")
def register_form(request: Request):
    return render(request, "register.html")


@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
             db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(password) < 8 or not name.strip():
        flash(request, "Enter a valid name and email, and a password of at least 8 characters.", "danger")
        return go("/register")
    if db.query(User).filter(User.email == email).first():
        flash(request, "That email is already registered.", "danger")
        return go("/register")
    u = User(name=name.strip(), email=email, password_hash=hash_password(password), role="attendee")
    db.add(u)
    db.commit()
    request.session.clear()
    request.session["uid"] = u.id
    return go("/")


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=next)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == email.strip().lower()).first()
    if not u or not verify_password(password, u.password_hash):
        flash(request, "Wrong email or password.", "danger")
        return go("/login")
    request.session.clear()
    request.session["uid"] = u.id
    return go(next if next.startswith("/") and not next.startswith("//") else "/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/")


# ------------------------------------------------------------------ browsing
@app.get("/")
def index(request: Request, q: str = "", category: str = "", venue_id: str = "", price_max: str = "",
          date_from: str = "", date_to: str = "", db: Session = Depends(get_db), user=Depends(get_user)):
    qs = db.query(Event).filter(Event.status == "published", Event.end >= now())
    if q:
        like = f"%{q}%"
        qs = qs.filter(or_(Event.title.ilike(like), Event.description.ilike(like)))
    if category:
        qs = qs.filter(Event.category == category)
    if venue_id.isdigit():
        qs = qs.filter(Event.venue_id == int(venue_id))
    try:
        if date_from:
            qs = qs.filter(Event.start >= datetime.fromisoformat(date_from))
        if date_to:
            qs = qs.filter(Event.start < datetime.fromisoformat(date_to) + timedelta(days=1))
    except ValueError:
        pass
    events = qs.order_by(Event.start).all()
    try:
        if price_max:
            events = [e for e in events if e.categories and min(c.price for c in e.categories) <= float(price_max)]
    except ValueError:
        pass
    return render(request, "events.html", user, events=events, venues=db.query(Venue).all(),
                  f={"q": q, "category": category, "venue_id": venue_id, "price_max": price_max,
                     "date_from": date_from, "date_to": date_to})


@app.get("/events/{eid}")
def event_page(request: Request, eid: int, db: Session = Depends(get_db), user=Depends(get_user)):
    ev = db.get(Event, eid)
    if not ev or (ev.status == "draft" and (not user or user.role != "admin")):
        raise HTTPException(404, "Event not found")
    mine = []
    if user:
        mine = db.query(WaitlistEntry).filter(WaitlistEntry.event_id == eid, WaitlistEntry.user_id == user.id,
                                              WaitlistEntry.status.in_(["waiting", "offered"])).all()
    return render(request, "event.html", user, ev=ev, mine=mine, max_tickets=config.MAX_TICKETS)


def _bus(db, user, to, content, perf="REQUEST"):
    r = bus.send(db, f"user:{user.id}", to, perf, content)
    db.commit()
    return r


@app.post("/events/{eid}/book")
def book(request: Request, eid: int, category_id: List[int] = Form(...), qty: List[int] = Form(...),
         method: str = Form("simulated"), db: Session = Depends(get_db), user=Depends(require())):
    items = [{"category_id": c, "qty": q} for c, q in zip(category_id, qty) if q > 0]
    r = _bus(db, user, "BookingAgent", {"action": "book", "user_id": user.id, "event_id": eid, "items": items})
    if r["performative"] == "AGREE":
        flash(request, f"Payment successful (simulated). Reference {r['content']['payment_ref']}. Your tickets are below.", "success")
        return go("/my/bookings")
    flash(request, r["content"].get("reason", "Booking failed"), "danger")
    return go(f"/events/{eid}")


@app.post("/events/{eid}/waitlist")
def join_waitlist(request: Request, eid: int, category_id: int = Form(...), qty: int = Form(1),
                  db: Session = Depends(get_db), user=Depends(require())):
    r = _bus(db, user, "WaitlistAgent", {"action": "join", "user_id": user.id, "event_id": eid,
                                         "category_id": category_id, "qty": qty})
    if r["performative"] == "AGREE":
        flash(request, f"You are on the waitlist (position {r['content']['position']}).", "success")
        return go("/my/waitlist")
    flash(request, r["content"].get("reason", "Could not join"), "danger")
    return go(f"/events/{eid}")


# ------------------------------------------------------------------ attendee area
@app.get("/my/{tab}")
def my(request: Request, tab: str, db: Session = Depends(get_db), user=Depends(require())):
    if tab not in ("bookings", "waitlist", "notifications"):
        raise HTTPException(404, "Not found")
    data = {}
    if tab == "bookings":
        data["bookings"] = db.query(Booking).filter(Booking.user_id == user.id).order_by(Booking.id.desc()).all()
    elif tab == "waitlist":
        es = db.query(WaitlistEntry).filter(WaitlistEntry.user_id == user.id).order_by(WaitlistEntry.id.desc()).all()
        data["entries"] = es
        data["secs"] = {e.id: max(0, int((e.offer_expires - now()).total_seconds())) for e in es if e.status == "offered"}
    else:
        ns = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(100).all()
        data["notes"] = [(n.text, n.created, n.read) for n in ns]
        for n in ns:
            n.read = True
        db.commit()
    return render(request, "my.html", user, tab=tab, **data)


@app.post("/my/bookings/{bid}/cancel")
def cancel_booking(request: Request, bid: int, db: Session = Depends(get_db), user=Depends(require())):
    r = _bus(db, user, "BookingAgent", {"action": "cancel", "user_id": user.id, "booking_id": bid})
    c = r["content"]
    if r["performative"] == "AGREE":
        flash(request, f"Booking cancelled. Refund LKR {c['refund']:,.2f} ({c['rule']}).", "success")
    else:
        flash(request, c.get("reason", "Could not cancel"), "danger")
    return go("/my/bookings")


@app.post("/waitlist/{wid}/{act}")
def waitlist_act(request: Request, wid: int, act: str, db: Session = Depends(get_db), user=Depends(require())):
    if act not in ("claim", "leave"):
        raise HTTPException(404, "Not found")
    r = _bus(db, user, "WaitlistAgent", {"action": act, "user_id": user.id, "entry_id": wid})
    if r["performative"] == "AGREE":
        flash(request, "Seats booked! See your tickets." if act == "claim" else "Removed from the waitlist.", "success")
        return go("/my/bookings" if act == "claim" else "/my/waitlist")
    flash(request, r["content"].get("reason", "Failed"), "danger")
    return go("/my/waitlist")


@app.get("/tickets/{code}/qr.png")
def qr_png(code: str, db: Session = Depends(get_db), user=Depends(require())):
    t = db.query(Ticket).filter(Ticket.code == code).first()
    if not t or (t.booking.user_id != user.id and user.role not in ("admin", "staff")):
        raise HTTPException(404, "Ticket not found")
    buf = io.BytesIO()
    qrcode.make(code, box_size=8, border=2).save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ private hire
def _hire_ctx(db, user):
    return dict(venues=db.query(Venue).filter(Venue.open_for_hire.is_(True)).all(),
                mine=db.query(HireRequest).filter(HireRequest.user_id == user.id).order_by(HireRequest.id.desc()).all())


@app.get("/hire")
def hire_form(request: Request, db: Session = Depends(get_db), user=Depends(require())):
    return render(request, "hire.html", user, result=None, **_hire_ctx(db, user))


@app.post("/hire")
def hire_post(request: Request, venue_id: int = Form(...), start: str = Form(...), hours: float = Form(...),
              guests: int = Form(...), purpose: str = Form(""), db: Session = Depends(get_db), user=Depends(require())):
    try:
        s = datetime.fromisoformat(start)
    except ValueError:
        flash(request, "Enter a valid date and time.", "danger")
        return go("/hire")
    e = s + timedelta(hours=hours)
    r = _bus(db, user, "VenueAgent", {"action": "hire_request", "user_id": user.id, "venue_id": venue_id,
                                      "start": s.isoformat(), "end": e.isoformat(), "guests": guests, "purpose": purpose})
    return render(request, "hire.html", user, result=r, form={"hours": hours, "guests": guests, "purpose": purpose},
                  **_hire_ctx(db, user))


# ------------------------------------------------------------------ staff scanner
class ScanIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    event_id: int


@app.get("/scanner")
def scanner(request: Request, db: Session = Depends(get_db), user=Depends(require("staff", "admin"))):
    evs = (db.query(Event).filter(Event.status == "published", Event.end >= now() - timedelta(hours=1))
           .order_by(Event.start).limit(30).all())
    return render(request, "scanner.html", user, events=evs)


@app.post("/api/scan")
def api_scan(body: ScanIn, db: Session = Depends(get_db), user=Depends(require("staff", "admin"))):
    r = bus.send(db, f"staff:{user.id}", "TicketAgent", "REQUEST",
                 {"action": "scan", "code": body.code, "event_id": body.event_id})
    db.commit()
    c = r["content"]
    return {"ok": r["performative"] == "AGREE", "result": c.get("result", "error"),
            "reason": c.get("reason", ""), "holder": c.get("holder", ""), "category": c.get("category", "")}


# ------------------------------------------------------------------ admin
admin_only = require("admin")


@app.get("/admin")
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
    rows = []
    for ev in db.query(Event).order_by(Event.start).all():
        rev = db.query(func.coalesce(func.sum(Booking.total - Booking.refund), 0)).filter(Booking.event_id == ev.id).scalar()
        chk = db.query(func.count(Ticket.id)).filter(Ticket.event_id == ev.id, Ticket.status == "used").scalar()
        rows.append({"ev": ev, "sold": sum(c.sold for c in ev.categories), "held": sum(c.held for c in ev.categories),
                     "tickets": sum(c.quantity for c in ev.categories), "revenue": rev, "checkins": chk})
    buckets = {}
    for (u,) in db.query(Ticket.used_at).filter(Ticket.status == "used"):
        k = u.strftime("%d %b %H:00")
        buckets[k] = buckets.get(k, 0) + 1
    chart = {"labels": [r["ev"].title[:22] for r in rows], "sold": [r["sold"] for r in rows],
             "revenue": [round(r["revenue"], 2) for r in rows], "checkins": [r["checkins"] for r in rows],
             "tl": list(buckets.keys()), "tv": list(buckets.values())}
    return render(request, "admin_dashboard.html", user, rows=rows, chart=chart,
                  total_rev=sum(r["revenue"] for r in rows),
                  states=db.query(AgentState).order_by(AgentState.agent, AgentState.key).filter(~AgentState.key.like("scans:%")).all(),
                  msgs=db.query(MessageLog).order_by(MessageLog.id.desc()).limit(12).all(),
                  alerts=db.query(Notification).filter(Notification.to_role == "admin").order_by(Notification.id.desc()).limit(8).all())


@app.get("/admin/messages")
def messages(request: Request, conv: str = "", agent: str = "", perf: str = "", db: Session = Depends(get_db),
             user=Depends(admin_only)):
    q = db.query(MessageLog)
    if conv:
        q = q.filter(MessageLog.conv_id == conv)
    if agent:
        q = q.filter(or_(MessageLog.sender == agent, MessageLog.receiver == agent))
    if perf:
        q = q.filter(MessageLog.performative == perf)
    msgs = q.order_by(MessageLog.id.desc() if not conv else MessageLog.id).limit(300).all()
    return render(request, "admin_messages.html", user, msgs=msgs, conv=conv, agent=agent, perf=perf)


@app.get("/admin/venues")
def venues(request: Request, edit: int = 0, db: Session = Depends(get_db), user=Depends(admin_only)):
    return render(request, "admin_venues.html", user, venues=db.query(Venue).order_by(Venue.id).all(),
                  v=db.get(Venue, edit) if edit else None)


@app.post("/admin/venues/save")
def save_venue(request: Request, id: str = Form(""), name: str = Form(...), location: str = Form(""),
               capacity: int = Form(...), description: str = Form(""), hourly_rate: float = Form(0),
               image_url: str = Form(""), open_for_hire: Optional[str] = Form(None),
               image: UploadFile = File(None), db: Session = Depends(get_db), user=Depends(admin_only)):
    if capacity < 1 or hourly_rate < 0 or not name.strip():
        flash(request, "Name required, capacity must be positive and rate cannot be negative.", "danger")
        return go("/admin/venues")
    v = db.get(Venue, int(id)) if id else Venue(name="", capacity=1)
    v.name, v.location, v.capacity, v.description = name.strip(), location, capacity, description
    v.hourly_rate, v.open_for_hire = hourly_rate, bool(open_for_hire)
    if image_url:
        v.image_url = image_url
    if image is not None and image.filename:
        ext = Path(image.filename).suffix.lower()
        data = image.file.read(2_000_001)
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif") and len(data) <= 2_000_000:
            fn = uuid.uuid4().hex + ext
            (UPLOADS / fn).write_bytes(data)
            v.image_url = "/static/uploads/" + fn
        else:
            flash(request, "Image must be png/jpg/webp/gif under 2 MB.", "danger")
    db.add(v)
    db.commit()
    flash(request, f"Venue '{v.name}' saved.", "success")
    return go("/admin/venues")


def _form_from(ev: Optional[Event]):
    if not ev:
        t = (now() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
        return {"id": "", "title": "", "category": "Concert", "venue_id": 0, "start": t, "end": t + timedelta(hours=2),
                "description": "", "status": "draft", "locked": False, "cats": [{"name": "General", "price": 0, "quantity": 50}]}
    return {"id": ev.id, "title": ev.title, "category": ev.category, "venue_id": ev.venue_id, "start": ev.start,
            "end": ev.end, "description": ev.description, "status": ev.status,
            "locked": any(c.sold or c.held for c in ev.categories),
            "cats": [{"name": c.name, "price": c.price, "quantity": c.quantity} for c in ev.categories]}


@app.get("/admin/events")
def admin_events(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
    return render(request, "admin_events.html", user, events=db.query(Event).order_by(Event.start.desc()).all())


@app.get("/admin/events/new")
def new_event(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
    return render(request, "admin_event_form.html", user, f=_form_from(None), venues=db.query(Venue).all())


@app.get("/admin/events/{eid}/edit")
def edit_event(request: Request, eid: int, db: Session = Depends(get_db), user=Depends(admin_only)):
    ev = db.get(Event, eid)
    if not ev or ev.status == "cancelled":
        raise HTTPException(404, "Event not found or cancelled")
    return render(request, "admin_event_form.html", user, f=_form_from(ev), venues=db.query(Venue).all())


@app.post("/admin/events/save")
def save_event(request: Request, id: str = Form(""), title: str = Form(...), category: str = Form(...),
               venue_id: int = Form(...), start: str = Form(...), end: str = Form(...), description: str = Form(""),
               action: str = Form("draft"), cat_name: List[str] = Form([]), cat_price: List[float] = Form([]),
               cat_qty: List[int] = Form([]), db: Session = Depends(get_db), user=Depends(admin_only)):
    ev = db.get(Event, int(id)) if id else None
    venue = db.get(Venue, venue_id)
    try:
        s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        s = e = now()
    cats = [(n.strip(), p, q) for n, p, q in zip(cat_name, cat_price, cat_qty) if n.strip()]
    locked = bool(ev and any(c.sold or c.held for c in ev.categories))
    form = {"id": id, "title": title, "category": category, "venue_id": venue_id, "start": s, "end": e,
            "description": description, "status": action, "locked": locked,
            "cats": [{"name": n, "price": p, "quantity": q} for n, p, q in cats]}
    errors = []
    if not title.strip():
        errors.append("Title is required.")
    if not venue:
        errors.append("Choose a venue.")
    if e <= s:
        errors.append("End time must be after the start time.")
    if locked:
        total_q = sum(c.quantity for c in ev.categories)
        form["cats"] = _form_from(ev)["cats"]
    else:
        if not cats:
            errors.append("Add at least one ticket category.")
        if any(p < 0 or q < 1 for _, p, q in cats):
            errors.append("Prices cannot be negative and quantities must be at least 1.")
        total_q = sum(q for _, _, q in cats)
    if venue and total_q > venue.capacity:
        errors.append(f"Total tickets ({total_q}) exceed the venue capacity ({venue.capacity}).")
    if not errors:
        r = bus.send(db, f"user:{user.id}", "VenueAgent", "REQUEST",
                     {"action": "check_slot", "venue_id": venue_id, "start": s.isoformat(), "end": e.isoformat(),
                      "exclude_event_id": ev.id if ev else None})
        db.commit()
        if r["performative"] != "AGREE":
            errors.append("VenueAgent refused the time slot: " + r["content"]["reason"])
    if errors:
        for m in errors:
            flash(request, m, "danger")
        return render(request, "admin_event_form.html", user, f=form, venues=db.query(Venue).all(), status_code=400)
    if not ev:
        ev = Event(title="", category="", venue_id=venue_id, start=s, end=e)
        db.add(ev)
    ev.title, ev.category, ev.venue_id, ev.start, ev.end, ev.description = title.strip(), category, venue_id, s, e, description
    ev.status = "published" if action == "publish" else ("published" if locked else "draft")
    if not locked:
        ev.categories = [TicketCategory(name=n, price=p, quantity=q) for n, p, q in cats]
    db.commit()
    flash(request, f"Event '{ev.title}' saved as {ev.status}.", "success")
    return go("/admin/events")


@app.post("/admin/events/{eid}/cancel")
def cancel_event(request: Request, eid: int, db: Session = Depends(get_db), user=Depends(admin_only)):
    ev = db.get(Event, eid)
    if not ev or ev.status != "published":
        flash(request, "Only published events can be cancelled.", "danger")
        return go("/admin/events")
    ev.status = "cancelled"
    db.flush()
    r = bus.send(db, f"user:{user.id}", "BookingAgent", "REQUEST", {"action": "event_cancelled", "event_id": eid})
    db.commit()
    flash(request, f"Event cancelled. {r['content'].get('bookings_refunded', 0)} booking(s) fully refunded.", "success")
    return go("/admin/events")


@app.get("/admin/hires")
def admin_hires(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
    return render(request, "admin_hires.html", user, hires=db.query(HireRequest).order_by(HireRequest.id.desc()).all())


@app.post("/admin/hires/{hid}/decide")
def decide_hire(request: Request, hid: int, approve: int = Form(...), note: str = Form(""),
                db: Session = Depends(get_db), user=Depends(admin_only)):
    r = bus.send(db, f"user:{user.id}", "VenueAgent", "REQUEST",
                 {"action": "decide_hire", "hire_id": hid, "approve": bool(approve), "note": note})
    db.commit()
    flash(request, f"Request {r['content'].get('decision', r['content'].get('reason'))}.",
          "success" if r["performative"] == "AGREE" else "danger")
    return go("/admin/hires")


# ------------------------------------------------------------------ agents over HTTP (advanced)
class AgentMessage(BaseModel):
    sender: str = Field(min_length=1, max_length=60)
    performative: str = Field(pattern="^(REQUEST|AGREE|REFUSE|PROPOSE|INFORM|FAILURE)$")
    content: dict
    conversation_id: Optional[str] = None


@app.post("/agents/{name}/inbox", tags=["agents"], summary="Deliver a JSON message to an agent")
def agent_inbox(name: str, m: AgentMessage, x_agent_key: str = Header(""), db: Session = Depends(get_db)):
    """Header `X-Agent-Key` must equal AGENT_API_KEY. Returns the agent's reply message (or null)."""
    if not hmac.compare_digest(x_agent_key, config.AGENT_API_KEY):
        raise HTTPException(401, "Bad agent key")
    if name not in bus.agents:
        raise HTTPException(404, "Unknown agent")
    r = bus.send(db, m.sender, name, m.performative, m.content, m.conversation_id)
    db.commit()
    return r
