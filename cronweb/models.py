from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, event, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    """Naive UTC timestamp, the format stored in the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    cron: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(32), default="github", server_default="github", nullable=False)

    owner: Mapped[str] = mapped_column(String(100))
    repo: Mapped[str] = mapped_column(String(100))
    workflow: Mapped[str] = mapped_column(String(255))
    ref: Mapped[str] = mapped_column(String(255), default="main")
    inputs_json: Mapped[str] = mapped_column(Text, default="{}")
    token_env: Mapped[str] = mapped_column(String(100), default="GITHUB_TOKEN")

    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    runs: Mapped[List["Run"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Run.id.desc()",
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))  # schedule | manual
    status: Mapped[str] = mapped_column(String(20))  # succeeded | failed | skipped
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="runs")


def _is_duplicate_provider_column(exc: DBAPIError, dialect: str) -> bool:
    original = exc.orig
    if dialect == "sqlite":
        return str(original).lower() == "duplicate column name: provider"
    if dialect == "postgresql":
        return getattr(original, "sqlstate", None) == "42701" or getattr(original, "pgcode", None) == "42701"
    if dialect in ("mysql", "mariadb"):
        return bool(original.args) and original.args[0] == 1060
    return False


def _migrate_provider(engine) -> None:
    # create_all does not alter existing tables. Keep this before any Job query.
    if any(column["name"] == "provider" for column in inspect(engine).get_columns("jobs")):
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN provider VARCHAR(32) NOT NULL DEFAULT 'github'"))
    except DBAPIError as exc:
        # Another startup may have added the column after our inspection. Do not
        # hide other DDL failures, and inspect on a fresh connection after rollback.
        if not _is_duplicate_provider_column(exc, engine.dialect.name):
            raise
        if not any(column["name"] == "provider" for column in inspect(engine).get_columns("jobs")):
            raise


def init_db(url: str) -> sessionmaker:
    is_sqlite = url.startswith("sqlite")
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if is_sqlite else {}
    engine = create_engine(url, pool_pre_ping=True, **kwargs)

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    Base.metadata.create_all(engine)
    _migrate_provider(engine)
    return sessionmaker(engine, expire_on_commit=False)
