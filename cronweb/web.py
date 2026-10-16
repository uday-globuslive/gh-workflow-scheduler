from __future__ import annotations

import hmac
import json
import secrets
from functools import wraps
from typing import Tuple
from zoneinfo import ZoneInfo, available_timezones

from croniter import croniter
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .github import OWNER_RE, REPO_RE, WORKFLOW_RE
from .models import Job, Run, utcnow
from .runner import TOKEN_ENV_PREFIX
from .scheduler import compute_next_run, preview_runs

bp = Blueprint("web", __name__)

JOB_FIELDS = ("name", "cron", "timezone", "owner", "repo", "workflow", "ref", "inputs_json", "token_env")


def _ctx():
    return current_app.extensions["cronweb"]


@bp.before_app_request
def _csrf_protect():
    if request.method == "POST":
        expected = session.get("csrf_token")
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not sent or not hmac.compare_digest(expected, sent):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")


@bp.app_context_processor
def _inject():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf_token"], "current_user": session.get("user")}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("web.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def _timezones():
    return sorted(available_timezones())


def _redirect_back(job_id: int):
    if request.form.get("back") == "detail":
        return redirect(url_for("web.job_detail", job_id=job_id))
    return redirect(url_for("web.index"))


def _parse_job_form(form) -> Tuple[dict, list]:
    data = {k: (form.get(k) or "").strip() for k in JOB_FIELDS}
    data["enabled"] = form.get("enabled") == "on"
    data["timezone"] = data["timezone"] or _ctx().config.default_timezone
    data["ref"] = data["ref"] or "main"
    data["token_env"] = data["token_env"] or TOKEN_ENV_PREFIX
    data["inputs_json"] = data["inputs_json"] or "{}"

    errors = []
    if not data["name"] or len(data["name"]) > 100:
        errors.append("Name is required (max 100 characters).")
    if not croniter.is_valid(data["cron"]):
        errors.append("Cron expression is invalid. Example: '0 9 * * 1' = every Monday 09:00.")
    try:
        ZoneInfo(data["timezone"])
    except Exception:
        errors.append(f"Unknown timezone '{data['timezone']}'.")
    if not OWNER_RE.match(data["owner"]):
        errors.append("Owner must be a valid GitHub user or organization name.")
    if not REPO_RE.match(data["repo"]):
        errors.append("Repository name is invalid.")
    if not WORKFLOW_RE.match(data["workflow"]):
        errors.append("Workflow must be a file name like 'build.yml' or a numeric workflow ID.")
    if len(data["ref"]) > 255 or any(c.isspace() for c in data["ref"]):
        errors.append("Ref must be a branch or tag name without spaces.")
    if not data["token_env"].startswith(TOKEN_ENV_PREFIX) or not data["token_env"].replace("_", "").isalnum():
        errors.append(f"Token variable must start with {TOKEN_ENV_PREFIX} (e.g. GITHUB_TOKEN or GITHUB_TOKEN_MYORG).")
    try:
        inputs = json.loads(data["inputs_json"])
        if not isinstance(inputs, dict) or not all(
            isinstance(v, (str, int, float, bool)) for v in inputs.values()
        ):
            raise ValueError
        data["inputs_json"] = json.dumps(inputs, indent=2)
    except ValueError:
        errors.append('Inputs must be a JSON object of simple values, e.g. {"environment": "prod"}.')
    return data, errors


# --- auth -------------------------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        cfg = _ctx().config
        user_ok = hmac.compare_digest(request.form.get("username", ""), cfg.admin_username)
        pass_ok = hmac.compare_digest(request.form.get("password", ""), cfg.admin_password)
        if user_ok and pass_ok:
            session.clear()
            session["user"] = cfg.admin_username
            nxt = request.args.get("next", "")
            # Only allow local redirects.
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("web.index")
            return redirect(nxt)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


# --- jobs -------------------------------------------------------------------


@bp.get("/")
@login_required
def index():
    with _ctx().session_factory() as s:
        jobs = s.scalars(select(Job).order_by(Job.name)).all()
        latest = {}
        for job in jobs:
            latest[job.id] = s.scalars(
                select(Run).where(Run.job_id == job.id).order_by(Run.id.desc()).limit(1)
            ).first()
        return render_template(
            "jobs.html", jobs=jobs, latest=latest, scheduler_running=_ctx().scheduler.running
        )


@bp.route("/jobs/new", methods=["GET", "POST"])
@login_required
def new_job():
    if request.method == "POST":
        data, errors = _parse_job_form(request.form)
        if not errors:
            job = Job(**data)
            job.next_run_at = compute_next_run(job.cron, job.timezone, utcnow()) if job.enabled else None
            with _ctx().session_factory() as s:
                s.add(job)
                try:
                    s.commit()
                    flash(f"Job '{job.name}' created.", "success")
                    return redirect(url_for("web.job_detail", job_id=job.id))
                except IntegrityError:
                    s.rollback()
                    errors.append("A job with that name already exists.")
        for e in errors:
            flash(e, "error")
        return render_template("job_form.html", job=data, is_new=True, timezones=_timezones())

    defaults = {
        "cron": "0 9 * * 1",
        "timezone": _ctx().config.default_timezone,
        "ref": "main",
        "inputs_json": "{}",
        "token_env": TOKEN_ENV_PREFIX,
        "enabled": True,
    }
    return render_template("job_form.html", job=defaults, is_new=True, timezones=_timezones())


@bp.get("/jobs/<int:job_id>")
@login_required
def job_detail(job_id: int):
    with _ctx().session_factory() as s:
        job = s.get(Job, job_id) or abort(404)
        runs = s.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(50)).all()
        upcoming = preview_runs(job.cron, job.timezone) if job.enabled else []
        return render_template("job_detail.html", job=job, runs=runs, upcoming=upcoming)


@bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def edit_job(job_id: int):
    with _ctx().session_factory() as s:
        job = s.get(Job, job_id) or abort(404)
        if request.method == "POST":
            data, errors = _parse_job_form(request.form)
            if not errors:
                for k, v in data.items():
                    setattr(job, k, v)
                job.next_run_at = compute_next_run(job.cron, job.timezone, utcnow()) if job.enabled else None
                try:
                    s.commit()
                    flash("Job updated.", "success")
                    return redirect(url_for("web.job_detail", job_id=job_id))
                except IntegrityError:
                    s.rollback()
                    errors.append("A job with that name already exists.")
            for e in errors:
                flash(e, "error")
            data["id"] = job_id
            return render_template("job_form.html", job=data, is_new=False, timezones=_timezones())
        return render_template("job_form.html", job=job, is_new=False, timezones=_timezones())


@bp.post("/jobs/<int:job_id>/toggle")
@login_required
def toggle_job(job_id: int):
    with _ctx().session_factory() as s:
        job = s.get(Job, job_id) or abort(404)
        job.enabled = not job.enabled
        # Re-enabling starts from now so missed occurrences are not replayed.
        job.next_run_at = compute_next_run(job.cron, job.timezone, utcnow()) if job.enabled else None
        s.commit()
        flash(f"Job '{job.name}' {'enabled' if job.enabled else 'disabled'}.", "success")
    return _redirect_back(job_id)


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job(job_id: int):
    run = _ctx().runner.run(job_id, "manual")
    if run is None:
        abort(404)
    flash(f"Run {run.status}: {run.message}", "success" if run.status == "succeeded" else "error")
    return _redirect_back(job_id)


@bp.post("/jobs/<int:job_id>/delete")
@login_required
def delete_job(job_id: int):
    with _ctx().session_factory() as s:
        job = s.get(Job, job_id) or abort(404)
        name = job.name
        s.delete(job)
        s.commit()
    flash(f"Job '{name}' deleted.", "success")
    return redirect(url_for("web.index"))


# --- api --------------------------------------------------------------------


@bp.get("/api/cron-preview")
@login_required
def cron_preview():
    expr = request.args.get("cron", "")
    tz = request.args.get("tz", "") or _ctx().config.default_timezone
    if not croniter.is_valid(expr):
        return jsonify(ok=False, error="Invalid cron expression"), 400
    try:
        runs = preview_runs(expr, tz)
    except Exception:
        return jsonify(ok=False, error="Unknown timezone"), 400
    return jsonify(ok=True, runs=[r.strftime("%a %Y-%m-%d %H:%M %Z") for r in runs])


@bp.get("/healthz")
def healthz():
    return jsonify(status="ok", scheduler_running=_ctx().scheduler.running)
