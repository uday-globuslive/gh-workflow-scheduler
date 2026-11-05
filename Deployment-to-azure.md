# GH Workflow Scheduler Deployment on Azure Container Apps with Azure PostgreSQL

This document describes how to deploy GH Workflow Scheduler to Azure Container Apps using Azure Database for PostgreSQL Flexible Server instead of SQLite.

---

# Architecture

```text
Internet
    |
Azure Container Apps
    |
    +-- gh-workflow-scheduler container
    |
Azure Database for PostgreSQL Flexible Server
```

Benefits:

- No VM management
- Automatic HTTPS
- Managed PostgreSQL database
- Persistent job storage
- Serverless hosting model
- Easy scaling and monitoring

---

# Prerequisites

- Azure Subscription
- Docker Hub image
- Azure CLI
- Azure PostgreSQL Flexible Server
- Azure Container Apps

---

# Update Application Dependencies

The default application supports SQLite.

For PostgreSQL support, add the PostgreSQL driver to `requirements.txt`.

## requirements.txt

```txt
Flask>=3.0,<4
SQLAlchemy>=2.0,<3
croniter>=2.0
requests>=2.31
waitress>=3.0
python-dotenv>=1.0
tzdata>=2024.1
psycopg[binary]>=3.1
greenlet<3.2.5; python_version < "3.10"
```

Without `psycopg`, the application fails with:

```text
ModuleNotFoundError: No module named 'psycopg'
```

---

# Rebuild Container Image

```bash
docker build -t udayglobuslive/gh-workflow-scheduler:latest .
```

Push image:

```bash
docker push udayglobuslive/gh-workflow-scheduler:latest
```

---

# Create PostgreSQL Flexible Server

Azure Portal:

```text
Create Resource
    -> Azure Database for PostgreSQL Flexible Server
```

Recommended development configuration:

```text
SKU: B1ms
vCPU: 1
Memory: 2 GB

Version: PostgreSQL 16
```

Example:

```text
Server Name:
cronweb-postgres

Database:
cronweb

Admin User:
cronadmin
```

---

# Configure Networking

Enable:

```text
Allow Azure Services and Resources to access this server
```

or configure a Private Endpoint if required.

---

# Create Database

Connect using Query Editor or pgAdmin.

```sql
CREATE DATABASE cronweb;
```

---

# Verify Database

```sql
SELECT current_database();
```

Expected:

```text
cronweb
```

---

# PostgreSQL Connection String

Format:

```text
postgresql+psycopg://username:password@server.postgres.database.azure.com:5432/database?sslmode=require
```

Example:

```text
postgresql+psycopg://cronadmin:Password123!@cronweb-postgres.postgres.database.azure.com:5432/cronweb?sslmode=require
```

---

# Create Resource Group

```bash
az group create \
  --name rg-cronweb \
  --location centralindia
```

---

# Create Container Apps Environment

```bash
az containerapp env create \
  --name cronweb-env \
  --resource-group rg-cronweb \
  --location centralindia
```

---

# Deploy Container App

```bash
az containerapp create \
  --name cronweb \
  --resource-group rg-cronweb \
  --environment cronweb-env \
  --image udayglobuslive/gh-workflow-scheduler:latest \
  --target-port 3001 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 1 \
  --env-vars \
    SECRET_KEY="replace-with-secure-random-string" \
    ADMIN_USERNAME="admin" \
    ADMIN_PASSWORD="admin123" \
    GITHUB_TOKEN="xxxxxxxx" \
    AZURE_DEVOPS_PAT="xxxxxxxx" \
    DATABASE_URL="postgresql+psycopg://cronadmin:PASSWORD@cronweb-postgres.postgres.database.azure.com:5432/cronweb?sslmode=require" \
    DEFAULT_TIMEZONE="UTC" \
    HOST="0.0.0.0" \
    PORT="3001" \
    SCHEDULER_ENABLED="true" \
    TICK_SECONDS="15" \
    MISFIRE_GRACE_SECONDS="3600" \
    SECURE_COOKIES="false"
```

---

# Important Scheduler Configuration

Since APScheduler runs inside the container:

```text
min-replicas = 1
max-replicas = 1
```

Do not scale to multiple replicas unless leader election is implemented.

Otherwise scheduled jobs may execute multiple times.

---

# Retrieve Application URL

```bash
az containerapp show \
  --name cronweb \
  --resource-group rg-cronweb \
  --query properties.configuration.ingress.fqdn
```

Example:

```text
https://cronweb.azurecontainerapps.io
```

---

# Verify Application

Browse:

```text
https://<container-app-url>
```

Login:

```text
Username: admin
Password: admin123
```

Create a test workflow and confirm execution.

---

# Monitor Logs

View live logs:

```bash
az containerapp logs show \
  --name cronweb \
  --resource-group rg-cronweb \
  --follow
```

---

# pgAdmin Verification

Register server:

```text
Host:
cronweb-postgres.postgres.database.azure.com

Port:
5432

Database:
cronweb

Username:
cronadmin

SSL Mode:
Require
```

---

# Verify Tables

List all tables:

```sql
SELECT schemaname, tablename
FROM pg_tables
ORDER BY schemaname, tablename;
```

List only application tables:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;
```

---

# Common Issue

## psycopg Missing

Error:

```text
ModuleNotFoundError: No module named 'psycopg'
```

Cause:

```text
PostgreSQL driver not installed in container image.
```

Fix:

Add to requirements.txt:

```txt
psycopg[binary]>=3.1
```

Rebuild and redeploy image.

---

# Updating Existing Container App

Push updated image:

```bash
docker push udayglobuslive/gh-workflow-scheduler:latest
```

Update Container App:

```bash
az containerapp update \
  --name cronweb \
  --resource-group rg-cronweb \
  --image udayglobuslive/gh-workflow-scheduler:latest
```

---

# Security Recommendations

Do not store:

```text
GITHUB_TOKEN
AZURE_DEVOPS_PAT
DATABASE PASSWORD
```

directly in source code.

Use:

```text
Azure Key Vault
```

and reference secrets from Container Apps.

---

# Cost Estimate

| Service | Approx Monthly Cost |
|----------|--------------------|
| Azure Container Apps | $5 - $15 |
| Azure PostgreSQL Flexible Server B1ms | $12 - $20 |
| Total | ~$20 - $35 |

Actual charges depend on region, uptime, storage, and usage.

---

# Deployment Summary

1. Create PostgreSQL Flexible Server.
2. Create `cronweb` database.
3. Add `psycopg[binary]` dependency.
4. Rebuild Docker image.
5. Push image to Docker Hub.
6. Create Container Apps Environment.
7. Deploy Container App.
8. Configure PostgreSQL `DATABASE_URL`.
9. Verify startup logs.
10. Verify tables in PostgreSQL.
11. Create scheduler jobs and validate execution.
