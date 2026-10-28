from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, urlsplit

import requests

from .github import DispatchResult


def _valid_segment(value: str) -> bool:
    # Each value must remain a single, non-traversing URL path segment.
    return (
        isinstance(value, str)
        and 0 < len(value) <= 100
        and value.strip() == value
        and value not in (".", "..")
        and not any(c in "/\\%" or unicodedata.category(c).startswith("C") for c in value)
    )


def _normalize_ref(ref: str) -> str | None:
    if not isinstance(ref, str):
        return None
    if not ref.strip():
        return ""
    if len(ref) > 255 or ref != ref.strip():
        return None
    if not ref.startswith("refs/"):
        ref = "refs/heads/" + ref
    parts = ref.split("/")
    if (
        len(parts) < 2
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)
        or ref.endswith(".")
        or ".." in ref
        or "@{" in ref
        or any(unicodedata.category(c).startswith("C") or c.isspace() or c in "~^:?*[\\" for c in ref)
    ):
        return None
    return ref


def _valid_base_url(url: str) -> bool:
    if not isinstance(url, str) or any(
        c.isspace() or unicodedata.category(c).startswith("C") or c in "\\?#" for c in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def dispatch_pipeline(
    *, api_url: str, token: str, owner: str, repo: str, workflow: str,
    ref: str, inputs: dict, timeout: int = 30,
) -> DispatchResult:
    """Queue an Azure DevOps Services pipeline without exposing credentials."""
    branch = _normalize_ref(ref)
    if not (
        _valid_base_url(api_url)
        and _valid_segment(owner)
        and _valid_segment(repo)
        and isinstance(workflow, str)
        and re.fullmatch(r"[0-9]{1,255}", workflow)
        and any(c != "0" for c in workflow)
        and branch is not None
        and isinstance(inputs, dict)
        and isinstance(token, str)
        and token
    ):
        return DispatchResult(False, None, "Invalid Azure DevOps dispatch configuration")

    url = (f"{api_url.rstrip('/')}/{quote(owner, safe='')}/{quote(repo, safe='')}"
           f"/_apis/pipelines/{workflow}/runs?api-version=7.1")
    payload: dict = {"templateParameters": inputs}
    if branch:
        payload["resources"] = {"repositories": {"self": {"refName": branch}}}

    try:
        response = requests.post(url, json=payload, auth=("", token), timeout=timeout, allow_redirects=False)
    except (requests.RequestException, UnicodeError, ValueError):
        # Exception strings and response bodies may contain credentials or request data.
        return DispatchResult(False, None, "Azure DevOps request failed")
    if response.status_code == 200:
        return DispatchResult(True, 200, "Pipeline dispatched")
    return DispatchResult(False, response.status_code, f"Azure DevOps request failed (HTTP {response.status_code})")