from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import sessionmaker

from .github import DispatchResult, dispatch_workflow
from .models import Job, Run, utcnow

log = logging.getLogger(__name__)

TOKEN_ENV_PREFIX = "GITHUB_TOKEN"


class JobRunner:
    def __init__(self, session_factory: sessionmaker, api_url: str):
        self.session_factory = session_factory
        self.api_url = api_url

    def run(self, job_id: int, trigger: str) -> Optional[Run]:
        started = utcnow()
        with self.session_factory() as s:
            job = s.get(Job, job_id)
            if job is None:
                return None
            name, owner, repo, workflow, ref = job.name, job.owner, job.repo, job.workflow, job.ref
            inputs_json, token_env = job.inputs_json, job.token_env

        # Only GITHUB_TOKEN* variables may be referenced, so jobs can't read other secrets.
        token = os.environ.get(token_env, "") if token_env.startswith(TOKEN_ENV_PREFIX) else ""
        if not token:
            result = DispatchResult(False, None, f"Environment variable {token_env} is not set or not allowed")
        else:
            result = dispatch_workflow(
                api_url=self.api_url,
                token=token,
                owner=owner,
                repo=repo,
                workflow=workflow,
                ref=ref,
                inputs=json.loads(inputs_json or "{}"),
            )

        with self.session_factory() as s:
            run = Run(
                job_id=job_id,
                trigger=trigger,
                status="succeeded" if result.ok else "failed",
                http_status=result.http_status,
                message=result.message,
                started_at=started,
                finished_at=utcnow(),
            )
            s.add(run)
            job = s.get(Job, job_id)
            if job is not None:
                job.last_run_at = started
            s.commit()

        log.info("Job %r (%s) %s: %s", name, trigger, run.status, result.message)
        return run

    def record_skip(self, job_id: int, scheduled: datetime) -> None:
        with self.session_factory() as s:
            s.add(
                Run(
                    job_id=job_id,
                    trigger="schedule",
                    status="skipped",
                    message=f"Missed run scheduled for {scheduled:%Y-%m-%d %H:%M} UTC (scheduler was not running)",
                    started_at=utcnow(),
                    finished_at=utcnow(),
                )
            )
            s.commit()
        log.warning("Job %s: skipped missed run scheduled for %s UTC", job_id, scheduled)
