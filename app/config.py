import os
from datetime import datetime, timedelta, timezone

try:  # optional .env support without extra dependency
    for _l in open(".env"):
        if "=" in _l and not _l.startswith("#"):
            _k, _v = _l.strip().split("=", 1)
            os.environ.setdefault(_k, _v)
except OSError:
    pass

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./eventsphere.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "dev-agent-key")
HOLD_MINUTES = float(os.getenv("HOLD_MINUTES", "10"))
SWEEP_SECONDS = int(os.getenv("SWEEP_SECONDS", "30"))
TZ_OFFSET_MINUTES = int(os.getenv("TZ_OFFSET_MINUTES", "330"))  # Sri Lanka UTC+5:30
CHECKIN_OPEN_HOURS = float(os.getenv("CHECKIN_OPEN_HOURS", "2"))
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"
MAX_TICKETS = 6          # per request and per attendee per event
BUFFER_MINUTES = 30      # venue turnaround buffer
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "eventsphere@localhost")
CATEGORIES = ["Concert", "Workshop", "Seminar", "Conference", "Other"]


def now() -> datetime:
    """Naive 'campus local' time (UTC + TZ_OFFSET_MINUTES)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ_OFFSET_MINUTES)
