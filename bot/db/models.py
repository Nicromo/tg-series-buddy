"""DB models (SQLAlchemy 2.0, async)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Pair(Base):
    """Family pair: wife and Dima share one pair_id."""

    __tablename__ = "pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="pair")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    pair_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pairs.id"), nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    pair: Mapped[Optional[Pair]] = relationship(back_populates="users")


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kp_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title_ru: Mapped[str] = mapped_column(String(256))
    title_en: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    description_ru: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    genres: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    rating_kp: Mapped[Optional[float]] = mapped_column(nullable=True)
    rating_imdb: Mapped[Optional[float]] = mapped_column(nullable=True)
    seasons: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status_kp: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    trailer_youtube_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    trailer_file_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    trailer_language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class UserSeries(Base):
    """user <-> series association with status and rating.

    Statuses:
      want         - want to watch
      watching     - currently watching
      watched      - finished watching (must have rating)
      want_rewatch - want to rewatch
      dropped      - dropped
    """

    __tablename__ = "user_series"
    __table_args__ = (UniqueConstraint("user_id", "series_id", name="uq_user_series"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)

    status: Mapped[str] = mapped_column(String(16), default="want")
    rating: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # like / dislike
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # When we last sent the weekly check-in for this series (to avoid spam).
    last_checkin_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
