from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base
from .config import now


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="attendee")  # admin | staff | attendee


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    location: Mapped[str] = mapped_column(String(200), default="")
    capacity: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(300), default="")
    hourly_rate: Mapped[float] = mapped_column(Float, default=0)
    open_for_hire: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|published|cancelled
    venue: Mapped[Venue] = relationship()
    categories: Mapped[list["TicketCategory"]] = relationship(
        back_populates="event", order_by="TicketCategory.id", cascade="all, delete-orphan")


class TicketCategory(Base):
    __tablename__ = "ticket_categories"
    # DB-level safety net: even a bug cannot make sold+held exceed quantity.
    __table_args__ = (CheckConstraint("sold + held <= quantity", name="ck_no_oversell"),
                      CheckConstraint("sold >= 0 AND held >= 0", name="ck_non_negative"))
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(80))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    held: Mapped[int] = mapped_column(Integer, default=0)  # seats held for waitlist offers
    event: Mapped[Event] = relationship(back_populates="categories")

    @property
    def available(self) -> int:
        return self.quantity - self.sold - self.held


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(String(20), default="confirmed")  # confirmed|cancelled
    total: Mapped[float] = mapped_column(Float, default=0)
    refund: Mapped[float] = mapped_column(Float, default=0)
    refund_rule: Mapped[str] = mapped_column(String(200), default="")
    payment_ref: Mapped[str] = mapped_column(String(40), default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    user: Mapped[User] = relationship()
    event: Mapped[Event] = relationship()
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="booking", order_by="Ticket.id")


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"))
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="valid")  # valid|used|cancelled
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    booking: Mapped[Booking] = relationship(back_populates="tickets")
    event: Mapped[Event] = relationship()
    category: Mapped[TicketCategory] = relationship()


class WaitlistEntry(Base):
    __tablename__ = "waitlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting|offered|claimed|expired|cancelled
    offer_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
    event: Mapped[Event] = relationship()
    category: Mapped[TicketCategory] = relationship()
    user: Mapped[User] = relationship()


class HireRequest(Base):
    __tablename__ = "hire_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    purpose: Mapped[str] = mapped_column(String(300), default="")
    guests: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # confirmed|pending|rejected
    price: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str] = mapped_column(String(300), default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
    user: Mapped[User] = relationship()
    venue: Mapped[Venue] = relationship()


class MessageLog(Base):
    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    conv_id: Mapped[str] = mapped_column(String(40), index=True)
    sender: Mapped[str] = mapped_column(String(60))
    receiver: Mapped[str] = mapped_column(String(60))
    performative: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)


class AgentState(Base):
    """Persistent beliefs of each agent (survive restarts)."""
    __tablename__ = "agent_state"
    agent: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="null")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    to_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    text: Mapped[str] = mapped_column(String(500))
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
