from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from .models import Job, utcnow
from .runner import JobRunner

log = logging.getLogger(__name__)


def compute_next_run(cron_expr: str, tz_name: str, after: datetime) -> datetime:
    """Next fire time strictly after `after` (naive UTC), evaluated in the job's timezone."""
    local = after.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    nxt = croniter(cron_expr, local).get_next(datetime)
    return nxt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def preview_runs(cron_expr: str, tz_name: str, count: int = 5) -> List[datetime]:
    """Upcoming fire times as aware datetimes in the job's timezone."""
    tz = ZoneInfo(tz_name)
    it = croniter(cron_expr, datetime.now(tz))
    return [it.get_next(datetime) for _ in range(count)]


class Scheduler:
    """Polls the database and fires due jobs.

    Each due job is claimed with a conditional UPDATE on next_run_at, so several
    instances sharing one database never fire the same occurrence twice.
    """

    def __init__(self, session_factory: sessionmaker, runner: JobRunner, tick_seconds: int, misfire_grace_seconds: int):
        self.session_factory = session_factory
        self.runner = runner
        self.tick_seconds = tick_seconds
        self.misfire_grace_seconds = misfire_grace_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cron-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        log.info("Scheduler started (tick every %ss)", self.tick_seconds)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            self._stop.wait(self.tick_seconds)
        log.info("Scheduler stopped")

    def tick(self, now: Optional[datetime] = None) -> List[int]:
        now = now or utcnow()
        fired: List[int] = []
        with self.session_factory() as s:
            due = s.execute(
                select(Job.id, Job.cron, Job.timezone, Job.next_run_at).where(
                    Job.enabled.is_(True), Job.next_run_at.is_not(None), Job.next_run_at <= now
                )
            ).all()

        for job_id, cron_expr, tz_name, scheduled in due:
            try:
                new_next = compute_next_run(cron_expr, tz_name, now)
            except Exception:
                log.exception("Job %s has an invalid schedule", job_id)
                continue

            with self.session_factory() as s:
                claimed = s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.next_run_at == scheduled, Job.enabled.is_(True))
                    .values(next_run_at=new_next)
                ).rowcount
                s.commit()
            if claimed != 1:
                continue  # another instance claimed it, or the job was edited meanwhile

            if (now - scheduled).total_seconds() > self.misfire_grace_seconds:
                self.runner.record_skip(job_id, scheduled)
                continue

            try:
                self.runner.run(job_id, "schedule")
                fired.append(job_id)
            except Exception:
                log.exception("Job %s failed to run", job_id)
        return fired
