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
## Layout
`app/agents/` the five agents + bus - `app/main.py` routes - `app/models.py` tables - `app/templates/` UI - `tests/` pytest - `docs/REPORT.md` report.

**How the agent works**

In EventSphere the web pages make no decisions. When you click “book”, the page sends a message to an agent and shows its reply. The five agents never call each other’s code. They only pass JSON messages through a message bus, and every message is logged with a conversation id so you can follow one action across agents.

Each message has a sender, a receiver, a type (REQUEST, AGREE, REFUSE, PROPOSE, INFORM or FAILURE), content and the conversation id. A refusal always gives a reason, and a PROPOSE always includes real alternatives. Each agent also keeps its own counters and flags in the database, so they survive a restart.

VenueAgent: owns every venue’s calendar. It refuses overlapping slots, with a 30-minute turnaround buffer, and prices private hires. If a hire request clashes, it proposes other times or other venues. Large or long hires are sent to the admin for approval.
BookingAgent: owns ticket stock. It enforces the 6-ticket limit and never sells more than exists, even when two people book the last seat at once. It also works out refunds on cancellation and refunds everyone if an event is cancelled.
TicketAgent: creates a unique code for each ticket. At the gate it admits a ticket only once and rejects wrong-event, cancelled or too-early scans. It flags a code that is scanned repeatedly.
WaitlistAgent: when a seat is freed, it holds it for the next person in line with a countdown. A timer runs every 30 seconds with no user action, expires unclaimed holds and offers the seat to the next person.
NotificationAgent: listens to INFORM messages from the other agents and turns them into notifications for the right user or the admin.

For example, one purchase goes: you → BookingAgent → TicketAgent (issues the codes) → NotificationAgent (confirmation). All of it shares one conversation id, which you can open under Admin → Agent messages in the app.
