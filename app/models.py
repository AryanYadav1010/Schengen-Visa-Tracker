"""SQLAlchemy ORM models: Watch, AvailabilitySnapshot, AlertLog."""

from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """A registered account on this self-hosted deployment."""

    __tablename__ = "users"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    email: str = Column(String(255), nullable=False, unique=True, index=True)
    password_hash: str = Column(String(255), nullable=False)
    created_at: datetime = Column(DateTime, nullable=False, default=func.now())

    telegram_chat_id: Optional[str] = Column(String(64), nullable=True)
    telegram_link_code: Optional[str] = Column(String(32), nullable=True, unique=True)

    google_email: Optional[str] = Column(String(255), nullable=True)
    google_refresh_token: Optional[str] = Column(String(1000), nullable=True)  # encrypted at rest

    watches = relationship("Watch", back_populates="user", lazy="selectin")
    credentials = relationship("Credential", back_populates="user", lazy="selectin")


class Credential(Base):
    """A user's login for one provider (tlscontact/vfs/bls), password encrypted at rest."""

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_credentials_user_provider"),
    )

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    user_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider: str = Column(String(50), nullable=False)         # tlscontact / vfs / bls
    email: str = Column(String(255), nullable=False)
    encrypted_password: str = Column(String(500), nullable=False)

    user = relationship("User", back_populates="credentials")


class Watch(Base):
    """A centre→destination→visa_type combination to monitor."""

    __tablename__ = "watches"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    user_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    centre: str = Column(String(100), nullable=False)          # e.g. "London"
    destination: str = Column(String(100), nullable=False)     # e.g. "France"
    visa_type: str = Column(String(50), nullable=False)        # tourism / business / long_stay
    provider: str = Column(String(50), nullable=False)         # tlscontact / vfs / bls
    enabled: bool = Column(Boolean, default=True, nullable=False)
    booking_url: str = Column(String(500), nullable=False, default="")
    alert_before_date: Optional[date] = Column(Date, nullable=True)  # suppress alerts past this date

    last_checked_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_error: Optional[str] = Column(String(500), nullable=True)
    backoff_until: Optional[datetime] = Column(DateTime, nullable=True)
    backoff_count: int = Column(Integer, default=0, nullable=False)

    user = relationship("User", back_populates="watches")
    snapshots = relationship("AvailabilitySnapshot", back_populates="watch", lazy="selectin")
    alerts = relationship("AlertLog", back_populates="watch", lazy="selectin")

    # ── Convenience helpers for the UI / scheduler ───────────

    @property
    def flag_emoji(self) -> str:
        """Return a flag emoji for the destination country."""
        flags = {
            "France": "🇫🇷", "Germany": "🇩🇪", "Italy": "🇮🇹",
            "Spain": "🇪🇸", "Portugal": "🇵🇹", "Netherlands": "🇳🇱",
            "Austria": "🇦🇹", "Belgium": "🇧🇪", "Greece": "🇬🇷",
            "Switzerland": "🇨🇭", "Sweden": "🇸🇪", "Norway": "🇳🇴",
            "Denmark": "🇩🇰", "Finland": "🇫🇮", "Poland": "🇵🇱",
            "Czech Republic": "🇨🇿", "Hungary": "🇭🇺", "Croatia": "🇭🇷",
            "Iceland": "🇮🇸", "Luxembourg": "🇱🇺", "Estonia": "🇪🇪",
            "Latvia": "🇱🇻", "Lithuania": "🇱🇹", "Malta": "🇲🇹",
            "Slovakia": "🇸🇰", "Slovenia": "🇸🇮",
        }
        return flags.get(self.destination, "🏳️")


class AvailabilitySnapshot(Base):
    """A single scrape result for a watch."""

    __tablename__ = "availability_snapshots"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    watch_id: int = Column(Integer, ForeignKey("watches.id"), nullable=False)
    checked_at: datetime = Column(DateTime, nullable=False, default=func.now())
    earliest_date: Optional[date] = Column(Date, nullable=True)  # None = no availability
    slots_json: str = Column(Text, default="[]")  # JSON list of {date, count}
    is_error: bool = Column(Boolean, default=False, nullable=False)
    error_message: Optional[str] = Column(String(500), nullable=True)

    watch = relationship("Watch", back_populates="snapshots")


class AlertLog(Base):
    """Record of each alert email sent, used for dedup / cooldown."""

    __tablename__ = "alert_logs"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    watch_id: int = Column(Integer, ForeignKey("watches.id"), nullable=False)
    alerted_at: datetime = Column(DateTime, nullable=False, default=func.now())
    earliest_date: date = Column(Date, nullable=False)
    email_sent_to: str = Column(String(200), nullable=False)

    watch = relationship("Watch", back_populates="alerts")
