from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import sessionmaker

from .azure_devops import dispatch_pipeline
from .github import DispatchResult, dispatch_workflow
from .models import Job, Run, utcnow

log = logging.getLogger(__name__)

TOKEN_ENV_PREFIX = "GITHUB_TOKEN"
AZURE_TOKEN_ENV_PREFIX = "AZURE_DEVOPS_PAT"

def _reject_json_constant(_value: str):
    raise ValueError("Invalid JSON constant")


class JobRunner:
    def __init__(self, session_factory: sessionmaker, api_url: str,
                 azure_devops_api_url: str = "https://dev.azure.com"):
        self.session_factory = session_factory
        self.api_url = api_url
        self.azure_devops_api_url = azure_devops_api_url

    def run(self, job_id: int, trigger: str) -> Optional[Run]:
        started = utcnow()
        with self.session_factory() as s:
            job = s.get(Job, job_id)
            if job is None:
                return None
            name, owner, repo, workflow, ref = job.name, job.owner, job.repo, job.workflow, job.ref
            inputs_json, token_env = job.inputs_json, job.token_env
            provider = job.provider

        prefixes = {"github": TOKEN_ENV_PREFIX, "azure_devops": AZURE_TOKEN_ENV_PREFIX}
        prefix = prefixes.get(provider)
        token = ""
        if prefix is None:
            result = DispatchResult(False, None, "Unknown job provider")
        elif (not isinstance(token_env, str) or not token_env.startswith(prefix)
              or (provider == "azure_devops" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env))):
            result = DispatchResult(False, None, "Token environment variable is not allowed for this provider")
        else:
            token = os.environ.get(token_env, "")
            if not token:
                result = DispatchResult(False, None, "Token environment variable is not set")
            else:
                try:
                    if provider == "azure_devops":
                        inputs = json.loads(inputs_json or "{}", parse_constant=_reject_json_constant)
                    else:
                        inputs = json.loads(inputs_json or "{}")
                    if not isinstance(inputs, dict):
                        raise ValueError("Inputs must be an object")
                except (TypeError, ValueError):
                    result = DispatchResult(False, None, "Inputs must be a valid JSON object")
                else:
                    dispatch = dispatch_workflow if provider == "github" else dispatch_pipeline
                    api_url = self.api_url if provider == "github" else self.azure_devops_api_url
                    result = dispatch(
                        api_url=api_url, token=token, owner=owner, repo=repo,
                        workflow=workflow, ref=ref, inputs=inputs,
                    )

        # GitHub's response/error text can reflect the supplied bearer token.
        if token and token in result.message:
            result = DispatchResult(result.ok, result.http_status, result.message.replace(token, "[redacted]"))

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
