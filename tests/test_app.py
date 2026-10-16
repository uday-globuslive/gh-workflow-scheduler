from datetime import datetime, timedelta
from unittest import mock

import pytest

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
