# Cron Scheduler for GitHub Actions and Azure DevOps Pipelines

A small web app that replaces `crontab` + `curl` shell scripts. You define cron schedules in a browser, and a built-in Python scheduler (no system cron, no Windows Task Scheduler) triggers GitHub Actions `workflow_dispatch` events or queues Azure DevOps Services pipelines. It runs the same way on Windows and Linux.

## Features
- Create, edit, enable/disable, and delete jobs, or click **Run now**
- Standard 5-field cron expressions with a timezone for each job, plus a live "next runs" preview. To build or check an expression, use [crontab.guru](https://crontab.guru/#30_23_*_*_*); for example, `30 23 * * *` runs every day at 23:30.
- Run history for each job (succeeded, failed, or skipped) with the provider HTTP status and message
- Tokens are read from environment variables and never stored in the database
- Login, CSRF protection, and HttpOnly session cookies
- Can run as several instances against one shared database; scheduled occurrences are claimed with a conditional database update

## Setup

```bash
python -m venv .venv
# Linux:   source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env` and set `SECRET_KEY` and `ADMIN_PASSWORD`, plus the token for each provider you intend to use. Supply the same required token variables to every instance that may dispatch jobs, including UI-only instances used for **Run now**.

### GitHub Actions

1. Set `GITHUB_TOKEN` to a fine-grained PAT with **Actions: Read and write** on the target repositories, or a classic PAT with the `repo` scope. For multiple tokens, use names beginning with `GITHUB_TOKEN`, such as `GITHUB_TOKEN_OTHERORG`.
2. Ensure the target workflow contains `on: workflow_dispatch:`.
3. Create a GitHub job with the owner, repository, workflow file name or numeric workflow ID, branch or tag, and token **environment variable name**. The branch defaults to `main`. Supply workflow inputs as a JSON object of simple values, for example `{"environment": "production"}`.

### Azure DevOps Services pipelines

This integration targets Azure DevOps Services YAML pipelines using the Pipelines Runs API. Classic pipelines and Azure DevOps Server are outside the verified scope; do not assume they are supported.

1. Create a PAT for an account that can access the target organization and project. Grant the PAT **Build: Read & execute**, and ensure the account has permission to queue builds for the target pipeline (including any applicable pipeline-specific permissions). Store the PAT in `AZURE_DEVOPS_PAT` in the app environment, not in the job form. For additional tokens, use names beginning with `AZURE_DEVOPS_PAT`, such as `AZURE_DEVOPS_PAT_OTHERORG`.
2. Create a job and select **Azure DevOps Services**. Enter the organization name, project name, and the pipeline's positive numeric ID, not its name. For example, for `https://dev.azure.com/contoso/Platform`, use `contoso` and `Platform`, then enter an ID such as `42`.
3. Optionally enter a branch or full ref. A branch such as `main` is sent as `refs/heads/main`; a full ref such as `refs/heads/release` is sent as entered. Leave the field empty to use the pipeline default. Set the token environment variable name to `AZURE_DEVOPS_PAT` or the appropriate suffixed name; leave it empty to use the default.
4. Enter a JSON object for template parameters, for example `{"environment": "production", "options": {"deploy": true}}`, or `{}` for none. These values are sent as `templateParameters`, not pipeline variables; the pipeline must define and accept the corresponding parameters. Configure pipeline variables and secrets in Azure DevOps instead: this app does not set pipeline variables through this field.

The provider is fixed when a job is created; create a new job to use another provider. Inputs and template parameters are stored in the database, so **never put secrets in them**.

### Triggering and run status

Both scheduled jobs and **Run now** send a dispatch request to the selected provider. A `succeeded` entry means the request was accepted (GitHub HTTP 200/204 or Azure DevOps HTTP 200), not that the workflow or pipeline finished successfully. The app does not track the eventual workflow or pipeline result; check it in GitHub or Azure DevOps. A `failed` entry means the app did not confirm acceptance; a network failure can leave the remote outcome unknown. A `skipped` entry records a scheduled occurrence that exceeded the misfire grace period.

For Azure DevOps failures, a JSON error message returned by the service is shown in run history and logs, subject to a length limit. A message reflecting the PAT or its Basic authentication value is withheld. Provider messages may still contain other sensitive operational information; restrict access to the UI, database, and logs, and review messages before sharing them.

### Upgrading an existing installation

1. Stop **all old-version replicas**, including UI-only replicas, before starting any new-version replica against the shared database. Do not mix old and new versions in a rolling deployment: old code does not understand Azure jobs and can misinterpret or dispatch them as GitHub jobs.
2. Back up the database before upgrading, using a consistent database backup appropriate for your engine. For SQLite, account for its WAL files or use SQLite's backup facility.
3. Start the new version. At startup it adds the `provider` column if missing, with `github` as the default for existing jobs; the database account must have permission to alter the jobs table. A migration failure prevents successful startup.
4. Start the remaining replicas on the new version, and verify existing GitHub jobs and configuration before creating Azure jobs.

Rolling back to old code cannot safely process Azure jobs, even though existing GitHub jobs remain compatible with the new schema. Stop all replicas and use a separately planned rollback with a suitable backup or data handling; do not run old and new versions together. Database drivers are not included for every database: install the driver matching your `DATABASE_URL` in the deployed environment or image (for example, `psycopg[binary]` for `postgresql+psycopg`), and verify migration support with your database and driver before deploying.

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
- For high availability, point every instance at the same PostgreSQL/MySQL database with `DATABASE_URL` and install the matching driver, such as `psycopg[binary]`. Instances use a conditional `UPDATE` to claim due occurrences. This is not an exactly-once delivery guarantee: a process can fail after claiming an occurrence, and a request's remote outcome can be uncertain. Manual runs are separate requests.
- To run extra UI-only replicas, set `SCHEDULER_ENABLED=false` on them.
- If the scheduler was down, runs that were missed by more than `MISFIRE_GRACE_SECONDS` are recorded as `skipped`. They are not replayed.

## Security notes
- Serve the app behind HTTPS, using a reverse proxy such as nginx, IIS, or Caddy, and set `SECURE_COOKIES=true`.
- GitHub jobs can reference only environment variables whose names start with `GITHUB_TOKEN`; Azure DevOps jobs can reference only names starting with `AZURE_DEVOPS_PAT`.
- Workflow inputs and pipeline template parameters are stored in the database; keep secrets in the provider's secret store, not in those fields.
- Anyone who can log in can trigger workflows or pipelines, so share the login only with trusted users or limit access with a VPN or an IP allow-list.

## Tests

```bash
pip install pytest
pytest
```
