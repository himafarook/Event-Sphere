# EventSphere - agent-based venue booking & digital ticketing (IT4123 ICA 02)

FastAPI + SQLAlchemy + Jinja/Bootstrap. Every booking decision is made by five cooperating agents
(VenueAgent, BookingAgent, TicketAgent, WaitlistAgent, NotificationAgent) that talk only through
JSON messages on a message bus (`app/agents/core.py`). Payments are simulated.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 (SQLite file `eventsphere.db` and demo data are created automatically).
API docs: http://127.0.0.1:8000/docs

## Demo accounts
| Role | Email | Password |
|---|---|---|
| Admin | admin@eventsphere.test | Admin@123 |
| Staff | staff@eventsphere.test | Staff@123 |
| Attendee | attendee@eventsphere.test | Attendee@123 |
| Attendee 2 | bob@eventsphere.test | Attendee@123 |

The seed includes an event starting in ~1 hour ("Live Demo Workshop") so check-in works during a viva,
and a 2-seat "Sold-out Demo Talk" for the waitlist demo (set `HOLD_MINUTES=1` to see expiry quickly).

## Environment variables
| Variable | Default | Purpose |
|---|---|---|
| SECRET_KEY | dev value | Session signing - **set in production** |
| DATABASE_URL | sqlite:///./eventsphere.db | Use the PostgreSQL URL in production |
| AGENT_API_KEY | dev value | Header `X-Agent-Key` for `/agents/{name}/inbox` |
| COOKIE_SECURE | 0 | Set 1 on HTTPS hosts |
| HOLD_MINUTES / SWEEP_SECONDS | 10 / 30 | Waitlist hold length / timer period |
| TZ_OFFSET_MINUTES | 330 | Campus time offset from UTC |
| CHECKIN_OPEN_HOURS | 2 | Check-in opens this long before start |
| SEED_DEMO | 1 | Create demo users/events on an empty DB (set 0 after first deploy if you prefer) |
| SMTP_HOST/PORT/USER/PASSWORD/FROM | empty | Optional e-mail notifications |

## Tests
```bash
pytest -q        # 24 tests: overbooking, double check-in, every agent, roles, CSRF, HTTP inbox
```

## Deploy on Render (HTTPS)
1. Push to GitHub. 2. Render -> New -> Blueprint -> pick the repo (uses `render.yaml`: web service + free PostgreSQL).
3. Open the live URL once before the viva (free tier sleeps).

## Layout
`app/agents/` the five agents + bus - `app/main.py` routes - `app/models.py` tables - `app/templates/` UI - `tests/` pytest - `docs/REPORT.md` report.
