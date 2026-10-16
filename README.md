# Cron Scheduler for GitHub Actions

A small web app that replaces `crontab` + `curl` shell scripts. You define cron schedules in a browser, and a built-in Python scheduler (no system cron, no Windows Task Scheduler) triggers GitHub Actions `workflow_dispatch` events. It runs the same way on Windows and Linux.

## Features
- Create, edit, enable/disable, and delete jobs, or click **Run now**
- Standard 5-field cron expressions with a timezone for each job, plus a live "next runs" preview. To build or check an expression, use [crontab.guru](https://crontab.guru/#30_23_*_*_*); for example, `30 23 * * *` runs every day at 23:30.
- Run history for each job (succeeded, failed, or skipped) with the GitHub HTTP status and error message
- Tokens are read from environment variables and never stored in the database
- Login, CSRF protection, and HttpOnly session cookies
- Can run as several instances against one shared database. Each run is claimed atomically, so it fires only once.

## Setup

```bash
python -m venv .venv
# Linux:   source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env` and set at least `SECRET_KEY`, `ADMIN_PASSWORD` and `GITHUB_TOKEN`.
The token must be a fine-grained PAT that has **Actions: Read and write** on the target repos, or a classic PAT with the `repo` scope.
The target workflow must contain `on: workflow_dispatch:`.

## Run

```bash
python -m cronweb
```

Open `http://<server>:8080`. To check the server, call `GET /healthz`.

## Docker

```bash
docker run -d --name cronweb -p 8080:8080 \
  --env-file .env -e DATABASE_URL=sqlite:////data/cronweb.db \
  -v cronweb-data:/data --restart unless-stopped \
  <dockerhub-user>/gh-workflow-scheduler:latest
```

The SQLite database is stored in the `/data` volume.

The workflow in `.github/workflows/docker.yml` runs the tests and then builds images for both amd64 and arm64.
- On every push to `main`, it pushes the image to Docker Hub tagged `latest` and `sha-xxxx`.
- On `v*.*.*` tags, it adds version tags such as `1.2.3` and `1.2`.
- On pull requests, it builds the image without pushing it.

It needs these repository secrets:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token (Account settings → Personal access tokens, *Read & Write*) |

## Migrating a crontab entry

| Old crontab / curl                                          | Job field          |
|-------------------------------------------------------------|--------------------|
| `0 9 * * 1 /path/script.sh`                                 | Cron: `0 9 * * 1`  |
| `.../repos/OWNER/REPO/actions/workflows/FILE/dispatches`    | Owner, Repo, Workflow |
| `-d '{"ref":"main","inputs":{...}}'`                        | Branch, Inputs     |

## Running as a service

**Linux (systemd)**, `/etc/systemd/system/cronweb.service`:

```ini
[Unit]
Description=Cron Scheduler
After=network-online.target

[Service]
WorkingDirectory=/opt/cronweb
ExecStart=/opt/cronweb/.venv/bin/python -m cronweb
Restart=always
User=cronweb

[Install]
WantedBy=multi-user.target
```

Then run `sudo systemctl enable --now cronweb`.

**Windows**: use [NSSM](https://nssm.cc/) (`nssm install cronweb E:\cronjob\.venv\Scripts\python.exe -m cronweb`, with the app directory set to the project folder). You can also use a Task Scheduler task that runs "At startup".

## Avoiding a single point of failure
- With the default SQLite file, run **one** instance.
- For high availability, point every instance at the same PostgreSQL/MySQL database with `DATABASE_URL` and install the matching driver, such as `psycopg[binary]`. Each run claims a due job with a conditional `UPDATE`, so only one instance fires each occurrence.
- To run extra UI-only replicas, set `SCHEDULER_ENABLED=false` on them.
- If the scheduler was down, runs that were missed by more than `MISFIRE_GRACE_SECONDS` are recorded as `skipped`. They are not replayed.

## Security notes
- Serve the app behind HTTPS, using a reverse proxy such as nginx, IIS, or Caddy, and set `SECURE_COOKIES=true`.
- Jobs can reference only environment variables whose names start with `GITHUB_TOKEN`.
- Anyone who can log in can trigger workflows, so share the login only with trusted users or limit access with a VPN or an IP allow-list.

## Tests

```bash
pip install pytest
pytest
```
