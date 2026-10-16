from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from flask import Flask
from sqlalchemy import select

from .config import Config
from .models import Job, init_db, utcnow
from .runner import JobRunner
from .scheduler import Scheduler, compute_next_run
from .web import bp


def _localtime(value: Optional[datetime], tz_name: str = "UTC") -> str:
    if value is None:
        return "-"
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def create_app(config: Optional[Config] = None, start_scheduler: bool = True) -> Flask:
    cfg = config or Config.from_env()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
    )

    session_factory = init_db(cfg.database_url)
    with session_factory() as s:
        for job in s.scalars(select(Job).where(Job.enabled.is_(True), Job.next_run_at.is_(None))):
            job.next_run_at = compute_next_run(job.cron, job.timezone, utcnow())
        s.commit()

    runner = JobRunner(session_factory, cfg.github_api_url)
    scheduler = Scheduler(session_factory, runner, cfg.tick_seconds, cfg.misfire_grace_seconds)
    app.extensions["cronweb"] = SimpleNamespace(
        config=cfg, session_factory=session_factory, runner=runner, scheduler=scheduler
    )
    app.add_template_filter(_localtime, "localtime")
    app.register_blueprint(bp)

    if start_scheduler and cfg.scheduler_enabled:
        scheduler.start()
    return app
