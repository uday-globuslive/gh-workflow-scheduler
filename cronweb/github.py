from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9._-]{1,100}$")
WORKFLOW_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9._-]{1,255}$")


@dataclass
class DispatchResult:
    ok: bool
    http_status: Optional[int]
    message: str


def dispatch_workflow(
    *,
    api_url: str,
    token: str,
    owner: str,
    repo: str,
    workflow: str,
    ref: str,
    inputs: dict,
    timeout: int = 30,
) -> DispatchResult:
    """Trigger a workflow_dispatch event (same as the curl call from the old shell script)."""
    if not (OWNER_RE.match(owner) and REPO_RE.match(repo) and WORKFLOW_RE.match(workflow)):
        return DispatchResult(False, None, "Invalid owner, repo or workflow name")

    url = f"{api_url.rstrip('/')}/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
    payload: dict = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return DispatchResult(False, None, f"Request failed: {exc.__class__.__name__}: {exc}")

    if resp.status_code in (200, 204):
        return DispatchResult(True, resp.status_code, "Workflow dispatched")

    try:
        detail = resp.json().get("message", "")
    except ValueError:
        detail = resp.text[:500]
    return DispatchResult(False, resp.status_code, detail or resp.reason)
