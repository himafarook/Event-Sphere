# EventSphere - Report (IT4123 ICA 02)

**Live URL:** _<paste Render URL>_  **GitHub:** _<paste repo URL>_
**Demo accounts:** admin@eventsphere.test / Admin@123 - staff@eventsphere.test / Staff@123 - attendee@eventsphere.test / Attendee@123

## 1. Architecture
Browser (Bootstrap, 375 px ready) -> FastAPI routes (auth, CSRF, role checks, validation) -> **MessageBus** -> agents -> PostgreSQL.
Routes never contain business rules: they send a REQUEST message to an agent and render its reply (AGREE / REFUSE / PROPOSE / INFORM / FAILURE).
Messages are JSON `{sender, receiver, performative, content, conversation_id}`; each is stored in `message_log` and shown at **Admin -> Agent messages**
(filter by conversation id to follow one user action across agents). Agents never import each other. Each agent persists beliefs in `agent_state`
(visible on the dashboard). A background timer wakes the WaitlistAgent every `SWEEP_SECONDS` (proactive). NotificationAgent is a reactive listener of INFORM events.
Optional HTTP access: `POST /agents/{name}/inbox` (header `X-Agent-Key`), documented in `/docs`.

Example trace of one booking: `user:3 -> BookingAgent REQUEST book` -> `BookingAgent -> TicketAgent REQUEST issue` -> `TicketAgent -> BookingAgent AGREE codes`
-> `BookingAgent -> NotificationAgent INFORM booking_confirmed` -> `BookingAgent -> user AGREE`.

## 2. Decision rules per agent
**VenueAgent** (goal: clash-free calendars; state: slot_checks, slot_refusals, hire_* counters)
- V1 Refuse a slot if end <= start. V2 Refuse if it overlaps any non-cancelled event or confirmed/pending hire at the venue, **including a 30-minute turnaround buffer** before and after; reason names the clash and the next free start.
- V3 Refuse hire if venue is not open for hire. V4 Refuse if duration < 1 h or > 12 h. V5 Refuse if less than 24 h notice. V6 Refuse guests < 1.
- V7 If guests > capacity or the slot clashes: **PROPOSE** up to 3 other times (+-1..12 h, same venue) and up to 3 other free venues (open for hire, big enough); REFUSE only if no alternative exists.
- V8 Escalate (INFORM `escalated`, status pending, admin notified) if guests > 80 % of capacity or duration > 6 h; otherwise confirm automatically (AGREE).
- V9 Price = hours x hourly rate x 1.25 on Sat/Sun x 1.15 if start is 18:00-21:59 (peak pricing, advanced). V10 Admin decision re-checks the calendar; approval refused if the slot was taken meanwhile.

**BookingAgent** (goal: fair sales, no overselling; state: bookings_accepted, tickets_sold, refused, cancellations, holds, checkins:<event>)
- B1 Event must be published. B2 Refuse once the event has started. B3 Refuse > 6 tickets per request. B4 Refuse if the attendee would hold > 6 active tickets for the event.
- B5 Refuse if requested quantity > (quantity - sold - held) for any category. B6 Refund policy on cancellation: >= 7 days before start 100 %, 2-7 days 50 %, < 48 h 0 %.
- B7 Refuse cancellation if any ticket was used or the event started. B8 Event cancelled by organiser: 100 % refund to every booking, tickets voided, waitlist cleared, users notified.
- B9 Seats held for waitlist offers are not sellable; a claim converts held to sold. B10 Payment is simulated; a `SIM-` reference is recorded.

**TicketAgent** (goal: admit each ticket once; state: issued, admitted, scans per ticket, flagged tickets)
- T1 Codes are `secrets.token_urlsafe(24)` (192-bit, unguessable) and unique. Scan checks in this order: unknown -> **cancelled** -> **wrong_event** -> **already_used** -> **outside_time** (opens 2 h before start, closes at end) -> **admit** (marks used, informs BookingAgent).
- T2 Suspicious scan: a code scanned >= 3 times is flagged once and the admin is notified (possible shared screenshot).

**WaitlistAgent** (goal: pass freed seats on; state: joined, offers_made, claimed, holds_expired, sweeps, last_sweep)
- W1 Join refused if seats are available (book directly), if already waiting for that category, or qty not 1-6. W2 Offers are FIFO among entries whose quantity fits the freed seats.
- W3 An offer places a hold for `HOLD_MINUTES` (10) with a visible countdown. W4 **Timer sweep**: expired offers -> status expired, hold released, user notified, seats re-offered to the next in line. W5 Claim refused if expired; declining releases and re-offers.

**NotificationAgent** (goal: tell the right person; state: sent, emails_sent)
- N1 Accepts only INFORM. N2 A rule table maps each event type to an audience (the user concerned, or the admin role) and a message template. N3 Unknown events are REFUSED. Optional e-mail via SMTP.

## 3. Concurrency (no overselling)
Seat changes run inside `SEAT_LOCK` (in-process lock) and use `SELECT ... FOR UPDATE` on the ticket-category row (PostgreSQL row lock); the transaction is **committed before the lock is released**, and rows are re-read with `populate_existing`. A DB `CHECK (sold + held <= quantity)` is a last line of defence. Test `test_never_oversells_under_concurrency` fires 12 threads at 3 seats: exactly 3 succeed.

## 4. Security
PBKDF2-SHA256 salted hashes (200k iterations); signed session cookies; role checks via dependencies on every admin/staff route; CSRF token required on every POST (forms hidden field, scanner via `X-CSRF-Token`); secrets only from environment variables; QR images served only to the ticket owner or staff.

## 5. Advanced features implemented
Waitlist with timed holds, countdown and automatic re-offer - peak/weekend pricing - suspicious-scan detection - agent HTTP inboxes - optional e-mail notifications - dashboard charts (Chart.js) - advanced search (keyword, category, venue, date range, max price). _Not implemented: cart holds._

## 6. Testing
`pytest -q` runs 24 tests: VenueAgent (clash+buffer, pricing, counter-proposal, escalation, not-open), BookingAgent (limits, concurrency overbooking, last seat, refund tiers, event cancel), TicketAgent (admit once, double check-in, wrong event/cancelled/unknown/early, suspicious), WaitlistAgent (hold, FIFO, timer expiry + re-offer, claim), NotificationAgent, traceability across agents, persisted state, roles, CSRF, hashing, end-to-end HTTP booking, scan API, HTTP inbox.

## 7. Deployment
Render blueprint (`render.yaml`): Python web service + managed PostgreSQL, HTTPS, `/healthz`, env vars generated. Use a single worker (the timer and in-process lock live in the process; row locks keep multi-instance safe for seats).
