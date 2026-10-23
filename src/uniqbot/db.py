from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, TypeDecorator, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite and always returns tz-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OrderStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class BroadcastStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    first_name: Mapped[str | None] = mapped_column(String(256), default=None)
    last_name: Mapped[str | None] = mapped_column(String(256), default=None)
    language_code: Mapped[str | None] = mapped_column(String(16), default=None)
    is_premium: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str | None] = mapped_column(String(64), default=None)  # first /start label, never overwritten
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    is_subscribed: Mapped[bool | None] = mapped_column(default=None)  # None until the first check
    subscription_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    videos_done: Mapped[int] = mapped_column(default=0)
    blocked: Mapped[bool] = mapped_column(default=False)
    blocked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(ForeignKey("users.tg_id"), index=True)
    file_id: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger, default=None)
    duration: Mapped[float | None] = mapped_column(default=None)
    mime_type: Mapped[str | None] = mapped_column(String(128), default=None)  # picks the input file extension
    file_name: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.QUEUED, index=True)
    status_message_id: Mapped[int | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    params_json: Mapped[str | None] = mapped_column(Text, default=None)  # chosen profile values, for quality complaints


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int]
    total: Mapped[int]
    sent: Mapped[int] = mapped_column(default=0)
    blocked: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default=BroadcastStatus.RUNNING)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


def make_engine(db_path: Path) -> AsyncEngine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        # the worker, handlers and a broadcast share one file: WAL + busy_timeout instead of "database is locked";
        # busy_timeout goes first, since switching to WAL itself waits for a lock held by another connection
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
