import json
import logging
import sqlite3
from datetime import datetime, timedelta
from unittest import mock

import pytest
import requests
from sqlalchemy import inspect
from werkzeug.datastructures import MultiDict

from cronweb import create_app
from cronweb.config import Config
from cronweb.github import DispatchResult
from cronweb.models import Job, Run
from cronweb.scheduler import compute_next_run


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    cfg = Config(
        secret_key="test",
        admin_username="admin",
        admin_password="pw",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )
    return create_app(cfg, start_scheduler=False)


def _ctx(app):
    return app.extensions["cronweb"]


def _add_job(app, next_run_at, **kw):
    fields = dict(name="weekly", cron="0 9 * * 1", timezone="UTC", owner="octo", repo="demo",
                  workflow="weekly.yml", ref="main", inputs_json="{}", token_env="GITHUB_TOKEN",
                  enabled=True, next_run_at=next_run_at)
    fields.update(kw)
    with _ctx(app).session_factory() as s:
        job = Job(**fields)
        s.add(job)
        s.commit()
        return job.id


def test_compute_next_run_respects_timezone():
    # Monday 2026-10-12 08:00 UTC = 10:00 Europe/Berlin (CEST); next 09:00 Berlin is Tue 07:00 UTC.
    nxt = compute_next_run("0 9 * * *", "Europe/Berlin", datetime(2026, 10, 12, 8, 0))
    assert nxt == datetime(2026, 10, 13, 7, 0)


def test_tick_fires_due_job_once(app):
    now = datetime(2026, 10, 12, 9, 0, 5)
    job_id = _add_job(app, datetime(2026, 10, 12, 9, 0))
    ok = DispatchResult(True, 204, "Workflow dispatched")
    with mock.patch("cronweb.runner.dispatch_workflow", return_value=ok) as dispatch:
        assert _ctx(app).scheduler.tick(now) == [job_id]
        assert _ctx(app).scheduler.tick(now) == []
    assert dispatch.call_count == 1
    assert dispatch.call_args.kwargs["token"] == "test-token"
    with _ctx(app).session_factory() as s:
        job = s.get(Job, job_id)
        assert job.next_run_at == datetime(2026, 10, 19, 9, 0)
        assert [r.status for r in s.query(Run).all()] == ["succeeded"]


def test_job_claimed_by_other_instance_is_not_fired(app):
    now = datetime(2026, 10, 12, 9, 0, 5)
    job_id = _add_job(app, datetime(2026, 10, 12, 9, 0))
    real_compute = compute_next_run

    def other_instance_claims_first(*args):
        with _ctx(app).session_factory() as s:
            s.get(Job, job_id).next_run_at = datetime(2026, 10, 19, 9, 0)
            s.commit()
        return real_compute(*args)

    with mock.patch("cronweb.scheduler.compute_next_run", side_effect=other_instance_claims_first), \
            mock.patch("cronweb.runner.dispatch_workflow") as dispatch:
        assert _ctx(app).scheduler.tick(now) == []
    dispatch.assert_not_called()


def test_missed_run_beyond_grace_is_skipped(app):
    now = datetime(2026, 10, 12, 9, 0)
    job_id = _add_job(app, now - timedelta(days=7))
    with mock.patch("cronweb.runner.dispatch_workflow") as dispatch:
        assert _ctx(app).scheduler.tick(now) == []
    dispatch.assert_not_called()
    with _ctx(app).session_factory() as s:
        assert [r.status for r in s.query(Run).filter_by(job_id=job_id)] == ["skipped"]


def _login(client):
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    resp = client.post("/login", data={"username": "admin", "password": "pw", "csrf_token": token})
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        return sess.get("csrf_token") or token


def test_web_requires_login(app):
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/healthz").status_code == 200


def test_post_without_csrf_rejected(app):
    client = app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "pw"}).status_code == 400


def test_create_and_run_job_via_web(app):
    client = app.test_client()
    client.get("/login")
    _login(client)
    client.get("/")  # regenerates csrf token after session.clear()
    with client.session_transaction() as sess:
        token = sess["csrf_token"]

    form = dict(name="weekly", cron="0 9 * * 1", timezone="UTC", owner="octo", repo="demo",
                workflow="weekly.yml", ref="main", inputs_json='{"env": "prod"}',
                token_env="GITHUB_TOKEN", enabled="on", csrf_token=token)
    resp = client.post("/jobs/new", data=form)
    assert resp.status_code == 302

    bad = dict(form, name="bad", cron="not a cron", repo="..")
    resp = client.post("/jobs/new", data=bad)
    assert b"Cron expression is invalid" in resp.data
    assert b"Repository name is invalid" in resp.data

    with _ctx(app).session_factory() as s:
        job = s.query(Job).filter_by(name="weekly").one()
        assert job.next_run_at is not None

    with mock.patch("cronweb.runner.dispatch_workflow", return_value=DispatchResult(True, 204, "ok")) as dispatch:
        resp = client.post(f"/jobs/{job.id}/run", data={"csrf_token": token})
    assert resp.status_code == 302
    assert dispatch.call_args.kwargs["inputs"] == {"env": "prod"}

    assert b"weekly" in client.get("/").data
    assert client.get(f"/jobs/{job.id}").status_code == 200
    assert client.get("/api/cron-preview?cron=0+9+*+*+1&tz=UTC").get_json()["ok"] is True


def _azure_form(csrf_token, **changes):
    form = dict(name="azure job", cron="0 9 * * 1", timezone="UTC", provider="azure_devops",
                owner="Example Org", repo="My Project", workflow="42", ref="feature/deploy",
                inputs_json='{"environment":"prod","settings":{"regions":["east", "west"]}}',
                token_env="AZURE_DEVOPS_PAT_TEAM", enabled="on", csrf_token=csrf_token)
    form.update(changes)
    return form


def _logged_in_client(app):
    client = app.test_client()
    _login(client)
    client.get("/")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    return client, token


def _add_azure_job(app, next_run_at=None, **changes):
    values = dict(provider="azure_devops", owner="Example Org", repo="My Project", workflow="42",
                  ref="main", token_env="AZURE_DEVOPS_PAT_TEAM",
                  inputs_json='{"settings":{"regions":["east", "west"]}}')
    values.update(changes)
    return _add_job(app, next_run_at, **values)


def test_real_legacy_schema_migrates_and_remains_usable(tmp_path, monkeypatch):
    # Schema matches the committed pre-provider Job and Run models, including indexes.
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE jobs (
                id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE,
                cron VARCHAR(100) NOT NULL, timezone VARCHAR(64) NOT NULL,
                enabled BOOLEAN NOT NULL, owner VARCHAR(100) NOT NULL,
                repo VARCHAR(100) NOT NULL, workflow VARCHAR(255) NOT NULL,
                ref VARCHAR(255) NOT NULL, inputs_json TEXT NOT NULL,
                token_env VARCHAR(100) NOT NULL, next_run_at DATETIME,
                last_run_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            CREATE INDEX ix_jobs_next_run_at ON jobs (next_run_at);
            CREATE TABLE runs (
                id INTEGER NOT NULL PRIMARY KEY, job_id INTEGER NOT NULL,
                trigger VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,
                http_status INTEGER, message TEXT NOT NULL, started_at DATETIME NOT NULL,
                finished_at DATETIME,
                FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
            );
            CREATE INDEX ix_runs_job_id ON runs (job_id);
            INSERT INTO jobs VALUES
                (1, 'legacy enabled', '0 9 * * 1', 'UTC', 1, 'octo', 'demo',
                 'weekly.yml', 'main', '{"environment":"prod"}', 'GITHUB_TOKEN', NULL, NULL,
                 '2026-01-01 00:00:00', '2026-01-01 00:00:00'),
                (2, 'legacy disabled', '0 9 * * 1', 'UTC', 0, 'octo', 'demo',
                 'weekly.yml', 'main', '{}', 'GITHUB_TOKEN', NULL, NULL,
                 '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            INSERT INTO runs VALUES (7, 1, 'manual', 'succeeded', 204, 'legacy history',
                                     '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        """)
        assert "provider" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}

    cfg = Config(secret_key="test", admin_username="admin", admin_password="pw",
                 database_url=f"sqlite:///{database}")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-legacy-token")
    with mock.patch("cronweb.utcnow", return_value=datetime(2026, 10, 12, 8)):
        migrated = create_app(cfg, start_scheduler=False)
    engine = _ctx(migrated).session_factory.kw["bind"]
    column = next(c for c in inspect(engine).get_columns("jobs") if c["name"] == "provider")
    assert column["nullable"] is False
    assert "github" in column["default"]
    with _ctx(migrated).session_factory() as session:
        job = session.get(Job, 1)
        assert (job.provider, job.name, job.workflow, job.inputs_json) == (
            "github", "legacy enabled", "weekly.yml", '{"environment":"prod"}')
        assert job.next_run_at == datetime(2026, 10, 12, 9)
        assert session.get(Job, 2).provider == "github"
        assert session.get(Job, 2).next_run_at is None
        assert session.get(Run, 7).message == "legacy history"
    with mock.patch("cronweb.runner.dispatch_workflow", return_value=DispatchResult(True, 204, "ok")) as github:
        run = _ctx(migrated).runner.run(1, "manual")
    assert (run.status, run.http_status) == ("succeeded", 204)
    assert github.call_args.kwargs["inputs"] == {"environment": "prod"}

    # Legacy SQL writers that do not know about provider get the server default.
    with sqlite3.connect(database) as db:
        db.execute("""
            INSERT INTO jobs (id, name, cron, timezone, enabled, owner, repo, workflow,
                              ref, inputs_json, token_env, created_at, updated_at)
            VALUES (3, 'old writer', '0 9 * * 1', 'UTC', 0, 'octo', 'demo', 'old.yml',
                    'main', '{}', 'GITHUB_TOKEN', '2026-01-01', '2026-01-01')
        """)
        assert db.execute("SELECT provider FROM jobs WHERE id = 3").fetchone() == ("github",)

    # A second startup is idempotent and preserves non-GitHub values and history.
    with _ctx(migrated).session_factory() as session:
        session.get(Job, 2).provider = "azure_devops"
        session.commit()
    again = create_app(cfg, start_scheduler=False)
    with _ctx(again).session_factory() as session:
        assert session.get(Job, 1).provider == "github"
        assert session.get(Job, 1).next_run_at == datetime(2026, 10, 12, 9)
        assert session.get(Job, 2).provider == "azure_devops"
        assert session.get(Job, 3).provider == "github"
        assert session.get(Run, 7).message == "legacy history"
        assert session.query(Run).filter_by(job_id=1).count() == 2
    _ctx(again).session_factory.kw["bind"].dispose()
    engine.dispose()


def test_azure_web_create_and_manual_dispatch_http_contract(app, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-azure-pat")
    client, csrf = _logged_in_client(app)
    response = client.post("/jobs/new", data=_azure_form(csrf))
    assert response.status_code == 302
    with _ctx(app).session_factory() as session:
        job = session.query(Job).filter_by(name="azure job").one()
        job_id = job.id
        assert job.provider == "azure_devops"
        assert job.next_run_at is not None
        assert json.loads(job.inputs_json) == {"environment": "prod", "settings": {"regions": ["east", "west"]}}

    with mock.patch("cronweb.azure_devops.requests.post") as post, \
            mock.patch("cronweb.runner.dispatch_workflow") as github:
        post.return_value.status_code = 200
        response = client.post(f"/jobs/{job_id}/run", data={"csrf_token": csrf, "back": "detail"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/jobs/{job_id}")
    github.assert_not_called()
    post.assert_called_once_with(
        "https://dev.azure.com/Example%20Org/My%20Project/_apis/pipelines/42/runs?api-version=7.1",
        json={"templateParameters": {"environment": "prod", "settings": {"regions": ["east", "west"]}},
              "resources": {"repositories": {"self": {"refName": "refs/heads/feature/deploy"}}}},
        auth=("", "fake-azure-pat"), timeout=30, allow_redirects=False,
    )
    with _ctx(app).session_factory() as session:
        job = session.get(Job, job_id)
        run = session.query(Run).filter_by(job_id=job_id).one()
        assert (run.trigger, run.status, run.http_status, run.message) == (
            "manual", "succeeded", 200, "Pipeline dispatched")
        assert run.started_at == job.last_run_at
        assert run.finished_at is not None


def test_azure_form_defaults_and_disabled_job_can_run_manually(app, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "fake-default-pat")
    client, csrf = _logged_in_client(app)
    form = _azure_form(csrf, ref="", token_env="", inputs_json="", enabled="")
    assert client.post("/jobs/new", data=form).status_code == 302
    with _ctx(app).session_factory() as session:
        job = session.query(Job).filter_by(name="azure job").one()
        job_id = job.id
        assert (job.ref, job.token_env, job.inputs_json, job.next_run_at) == ("", "AZURE_DEVOPS_PAT", "{}", None)
        assert job.enabled is False
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = 200
        assert client.post(f"/jobs/{job_id}/run", data={"csrf_token": csrf}).status_code == 302
    assert post.call_args.kwargs["json"] == {"templateParameters": {}}


@pytest.mark.parametrize("field,value,error", [
    ("provider", "other", "Select a valid provider"),
    ("owner", "../org", "Azure DevOps organization is invalid"),
    ("owner", "org%2fother", "Azure DevOps organization is invalid"),
    ("repo", "project/other", "Azure DevOps project is invalid"),
    ("repo", "..", "Azure DevOps project is invalid"),
    ("workflow", "0", "Pipeline ID must be a positive numeric ID"),
    ("workflow", "abc.yml", "Pipeline ID must be a positive numeric ID"),
    ("workflow", "1" * 256, "Pipeline ID must be a positive numeric ID"),
    ("ref", "feature//bad", "Ref must be an optional valid branch name or full ref"),
    ("ref", "feature/bad.lock", "Ref must be an optional valid branch name or full ref"),
    ("ref", "feature with spaces", "Ref must be an optional valid branch name or full ref"),
    ("token_env", "GITHUB_TOKEN", "Token variable must be an ASCII environment variable"),
    ("token_env", "AZURE_DEVOPS_PAT_é", "Token variable must be an ASCII environment variable"),
    ("token_env", "AZURE_DEVOPS_PAT_" + "x" * 100, "Token variable must be an ASCII environment variable"),
    ("inputs_json", "[]", "Template parameters must be a JSON object"),
    ("inputs_json", '{"bad": NaN}', "Template parameters must be a JSON object"),
    ("inputs_json", '{"bad": Infinity}', "Template parameters must be a JSON object"),
    ("inputs_json", "{bad", "Template parameters must be a JSON object"),
    ("cron", "not cron", "Cron expression is invalid"),
    ("timezone", "Not/A_Timezone", "Unknown timezone"),
])
def test_azure_form_rejects_invalid_values_without_saving(app, field, value, error):
    client, csrf = _logged_in_client(app)
    response = client.post("/jobs/new", data=_azure_form(csrf, **{field: value}))
    assert response.status_code == 200
    assert error.encode() in response.data
    with _ctx(app).session_factory() as session:
        assert session.query(Job).count() == 0


def test_provider_tampering_and_csrf_rejected(app):
    client, csrf = _logged_in_client(app)
    job_id = _add_azure_job(app)
    assert client.post(f"/jobs/{job_id}/run").status_code == 400
    assert client.post(f"/jobs/{job_id}/edit", data=_azure_form(csrf, provider="github")).status_code == 400
    fields = MultiDict(_azure_form(csrf))
    fields.add("provider", "github")
    assert client.post(f"/jobs/{job_id}/edit", data=fields).status_code == 400
    response = client.post("/jobs/new", data=fields)
    assert b"Select a valid provider" in response.data
    with _ctx(app).session_factory() as session:
        assert session.get(Job, job_id).provider == "azure_devops"
        assert session.query(Job).count() == 1


def test_legacy_github_form_defaults_and_validation(app):
    client, csrf = _logged_in_client(app)
    form = dict(name="github legacy", cron="0 9 * * 1", timezone="UTC", owner="octo",
                repo="demo", workflow="build.yml", ref="", inputs_json="{}",
                token_env="", enabled="on", csrf_token=csrf)
    assert client.post("/jobs/new", data=form).status_code == 302
    with _ctx(app).session_factory() as session:
        job = session.query(Job).filter_by(name="github legacy").one()
        assert (job.provider, job.ref, job.token_env) == ("github", "main", "GITHUB_TOKEN")
    invalid = dict(form, name="nested github", inputs_json='{"nested": {"x": 1}}')
    assert b"Inputs must be a JSON object of simple values" in client.post("/jobs/new", data=invalid).data
    with _ctx(app).session_factory() as session:
        assert session.query(Job).count() == 1


def test_azure_edit_toggle_and_delete_preserve_scheduling_and_history(app, monkeypatch):
    client, csrf = _logged_in_client(app)
    job_id = _add_azure_job(app, datetime(2026, 10, 12, 9))
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-pat")
    with mock.patch("cronweb.runner.dispatch_pipeline", return_value=DispatchResult(True, 200, "ok")):
        assert client.post(f"/jobs/{job_id}/run", data={"csrf_token": csrf}).status_code == 302
    with mock.patch("cronweb.web.utcnow", return_value=datetime(2026, 10, 12, 8)):
        assert client.post(f"/jobs/{job_id}/edit", data=_azure_form(csrf, cron="0 10 * * 1")).status_code == 302
    with _ctx(app).session_factory() as session:
        assert session.get(Job, job_id).next_run_at == datetime(2026, 10, 12, 10)
    assert client.post(f"/jobs/{job_id}/toggle", data={"csrf_token": csrf}).status_code == 302
    with _ctx(app).session_factory() as session:
        assert session.get(Job, job_id).next_run_at is None
    with mock.patch("cronweb.web.utcnow", return_value=datetime(2026, 10, 12, 8)):
        assert client.post(f"/jobs/{job_id}/toggle", data={"csrf_token": csrf}).status_code == 302
    with _ctx(app).session_factory() as session:
        assert session.get(Job, job_id).next_run_at == datetime(2026, 10, 12, 10)
        assert session.query(Run).filter_by(job_id=job_id).count() == 1
    assert client.post(f"/jobs/{job_id}/delete", data={"csrf_token": csrf}).status_code == 302
    with _ctx(app).session_factory() as session:
        assert session.get(Job, job_id) is None
        assert session.query(Run).filter_by(job_id=job_id).count() == 0


@pytest.mark.parametrize("changes,message", [
    ({"provider": "unknown"}, "Unknown job provider"),
    ({"token_env": "GITHUB_TOKEN"}, "Token environment variable is not allowed for this provider"),
    ({"token_env": "AZURE_DEVOPS_PAT-bad"}, "Token environment variable is not allowed for this provider"),
    ({"token_env": "AZURE_DEVOPS_PAT_MISSING"}, "Token environment variable is not set"),
    ({"inputs_json": "not json"}, "Inputs must be a valid JSON object"),
    ({"inputs_json": "[]"}, "Inputs must be a valid JSON object"),
    ({"inputs_json": '{"v": NaN}'}, "Inputs must be a valid JSON object"),
    ({"inputs_json": '{"v": -Infinity}'}, "Inputs must be a valid JSON object"),
])
def test_azure_runner_rejects_invalid_stored_jobs(app, monkeypatch, changes, message):
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-pat")
    monkeypatch.delenv("AZURE_DEVOPS_PAT_MISSING", raising=False)
    job_id = _add_azure_job(app, **changes)
    with mock.patch("cronweb.runner.dispatch_pipeline") as azure, \
            mock.patch("cronweb.runner.dispatch_workflow") as github:
        run = _ctx(app).runner.run(job_id, "manual")
    azure.assert_not_called()
    github.assert_not_called()
    assert (run.status, run.http_status, run.message) == ("failed", None, message)
    with _ctx(app).session_factory() as session:
        assert session.get(Run, run.id).message == message
        assert session.get(Job, job_id).last_run_at == run.started_at


def test_azure_scheduled_dispatch_and_misfire_boundary(app, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-pat")
    now = datetime(2026, 10, 12, 9)
    grace = _ctx(app).scheduler.misfire_grace_seconds
    due = _add_azure_job(app, now - timedelta(seconds=grace), name="due")
    late = _add_azure_job(app, now - timedelta(seconds=grace + 1), name="late")
    future = _add_azure_job(app, now + timedelta(minutes=1), name="future")
    disabled = _add_azure_job(app, now, name="disabled", enabled=False)
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = 200
        assert _ctx(app).scheduler.tick(now) == [due]
        assert _ctx(app).scheduler.tick(now) == []
    post.assert_called_once()
    assert post.call_args.kwargs["auth"] == ("", "fake-pat")
    assert post.call_args.kwargs["json"]["resources"]["repositories"]["self"]["refName"] == "refs/heads/main"
    with _ctx(app).session_factory() as session:
        assert session.get(Job, due).next_run_at == datetime(2026, 10, 19, 9)
        assert session.get(Job, late).next_run_at == datetime(2026, 10, 19, 9)
        assert session.get(Job, future).next_run_at == now + timedelta(minutes=1)
        assert session.get(Job, disabled).next_run_at == now
        assert [(r.trigger, r.status, r.http_status) for r in session.query(Run).filter_by(job_id=due)] == [("schedule", "succeeded", 200)]
        assert [(r.trigger, r.status) for r in session.query(Run).filter_by(job_id=late)] == [("schedule", "skipped")]
        assert session.get(Job, late).last_run_at is None


def test_scheduled_azure_http_failure_is_recorded_and_not_retried(app, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-pat")
    now = datetime(2026, 10, 12, 9)
    job_id = _add_azure_job(app, now)
    with mock.patch("cronweb.azure_devops.requests.post") as post:
        post.return_value.status_code = 503
        assert _ctx(app).scheduler.tick(now) == [job_id]
        assert _ctx(app).scheduler.tick(now) == []
    post.assert_called_once()
    with _ctx(app).session_factory() as session:
        run = session.query(Run).filter_by(job_id=job_id).one()
        assert (run.trigger, run.status, run.http_status, run.message) == (
            "schedule", "failed", 503, "Azure DevOps request failed (HTTP 503)")
        assert session.get(Job, job_id).next_run_at == datetime(2026, 10, 19, 9)


def test_azure_job_disabled_during_claim_does_not_dispatch(app):
    now = datetime(2026, 10, 12, 9)
    job_id = _add_azure_job(app, now)
    real_compute = compute_next_run

    def disable_before_claim(*args):
        with _ctx(app).session_factory() as session:
            session.get(Job, job_id).enabled = False
            session.commit()
        return real_compute(*args)

    with mock.patch("cronweb.scheduler.compute_next_run", side_effect=disable_before_claim), \
            mock.patch("cronweb.runner.dispatch_pipeline") as azure:
        assert _ctx(app).scheduler.tick(now) == []
    azure.assert_not_called()
    with _ctx(app).session_factory() as session:
        assert session.get(Job, job_id).next_run_at == now
        assert session.query(Run).filter_by(job_id=job_id).count() == 0


def test_runner_uses_configured_azure_url_and_does_not_dispatch_missing_job(tmp_path, monkeypatch):
    cfg = Config(secret_key="test", admin_username="admin", admin_password="pw",
                 database_url=f"sqlite:///{tmp_path / 'custom.db'}",
                 github_api_url="https://github.example", azure_devops_api_url="https://ado.example/tfs")
    app = create_app(cfg, start_scheduler=False)
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-pat")
    job_id = _add_azure_job(app, ref="refs/tags/v1")
    with mock.patch("cronweb.runner.dispatch_pipeline", return_value=DispatchResult(True, 200, "ok")) as azure, \
            mock.patch("cronweb.runner.dispatch_workflow") as github:
        assert _ctx(app).runner.run(9999, "manual") is None
        run = _ctx(app).runner.run(job_id, "manual")
    github.assert_not_called()
    azure.assert_called_once_with(api_url="https://ado.example/tfs", token="fake-pat",
                                  owner="Example Org", repo="My Project", workflow="42",
                                  ref="refs/tags/v1", inputs={"settings": {"regions": ["east", "west"]}})
    assert (run.status, run.http_status) == ("succeeded", 200)
    _ctx(app).session_factory.kw["bind"].dispose()


def test_rendered_azure_job_and_run_data_are_html_escaped(app, monkeypatch):
    client, csrf = _logged_in_client(app)
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", "fake-pat")
    job_id = _add_azure_job(app, name="<script>alert(1)</script>",
                            inputs_json='{"value":"<img src=x onerror=alert(1)>"}')
    with mock.patch("cronweb.runner.dispatch_pipeline",
                    return_value=DispatchResult(False, 400, "<script>alert(2)</script>")):
        client.post(f"/jobs/{job_id}/run", data={"csrf_token": csrf})
    for path in ("/", f"/jobs/{job_id}", f"/jobs/{job_id}/edit"):
        content = client.get(path).data
        assert b"<script>alert(1)</script>" not in content
        assert b"<img src=x onerror=alert(1)>" not in content
        assert b"<script>alert(2)</script>" not in content
    detail = client.get(f"/jobs/{job_id}").data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in detail
    assert b"&lt;script&gt;alert(2)&lt;/script&gt;" in detail


@pytest.mark.parametrize("failure", ["response", "exception"])
def test_azure_secret_not_in_failure_history_logs_or_web(app, monkeypatch, caplog, failure):
    secret = "fake-azure-secret-never-display"
    monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", secret)
    client, csrf = _logged_in_client(app)
    job_id = _add_azure_job(app)
    with caplog.at_level(logging.INFO), mock.patch("cronweb.azure_devops.requests.post") as post:
        if failure == "response":
            post.return_value.status_code = 401
            post.return_value.text = "server reflected " + secret
        else:
            post.side_effect = requests.ConnectionError("request contained " + secret)
        response = client.post(f"/jobs/{job_id}/run", data={"csrf_token": csrf}, follow_redirects=True)
    assert response.status_code == 200
    pages = [response.data, client.get("/").data, client.get(f"/jobs/{job_id}").data,
             client.get(f"/jobs/{job_id}/edit").data]
    assert all(secret.encode() not in page for page in pages)
    assert secret not in caplog.text
    with _ctx(app).session_factory() as session:
        run = session.query(Run).filter_by(job_id=job_id).one()
        assert run.status == "failed"
        assert secret not in run.message
        assert run.http_status == (401 if failure == "response" else None)
        assert session.get(Job, job_id).token_env == "AZURE_DEVOPS_PAT_TEAM"


@pytest.mark.parametrize("provider", ["github", "azure_devops"])
def test_runner_redacts_exact_token_in_messages_database_logs_and_flash(app, monkeypatch, caplog, provider):
    secret = "fake-token-for-redaction"
    client, csrf = _logged_in_client(app)
    if provider == "github":
        monkeypatch.setenv("GITHUB_TOKEN", secret)
        job_id = _add_job(app, None)
        target = "cronweb.runner.dispatch_workflow"
    else:
        monkeypatch.setenv("AZURE_DEVOPS_PAT_TEAM", secret)
        job_id = _add_azure_job(app)
        target = "cronweb.runner.dispatch_pipeline"
    with caplog.at_level(logging.INFO), mock.patch(target, return_value=DispatchResult(False, 403, f"{secret} / {secret}")):
        response = client.post(f"/jobs/{job_id}/run", data={"csrf_token": csrf}, follow_redirects=True)
    assert secret.encode() not in response.data
    assert secret.encode() not in client.get(f"/jobs/{job_id}").data
    assert secret not in caplog.text
    with _ctx(app).session_factory() as session:
        run = session.query(Run).filter_by(job_id=job_id).one()
        assert run.message == "[redacted] / [redacted]"
        assert (run.status, run.http_status) == ("failed", 403)
