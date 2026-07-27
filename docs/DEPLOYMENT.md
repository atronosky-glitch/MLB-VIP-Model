# DEPLOYMENT.md — Cloud Deployment Guide

## Platform: Render

**Why Render**: Native Streamlit support, managed PostgreSQL, background workers, environment secrets, automatic restarts.

## Architecture

```
Render PostgreSQL (Starter $7/mo)
  ↕ DATABASE_URL (auto-wired)
Web Service (Streamlit dashboard) — Starter $7/mo
Worker Service (background jobs) — Starter $7/mo
Persistent Disk (cache, output, backups) — 1GB included
```

- **Production**: PostgreSQL via `DATABASE_URL` env var (auto-provided by Render)
- **Local/Tests**: SQLite via `MLB_DB_PATH` or default `database/mlb_model.db`
- The `DB` wrapper in `database/connection.py` auto-detects and converts SQL

## Prerequisites

1. GitHub account with the repository pushed
2. Render account (https://dashboard.render.com)
3. SportsGameOdds API key

## Account Setup

1. Sign up at https://render.com
2. Connect your GitHub account
3. Create a new **Blueprint** from the repository

## Environment Variables

Set these in Render Dashboard → Environment:

| Variable | Required | Description |
|----------|----------|-------------|
| `SPORTSODDS_API_KEY` | Yes | SportsGameOdds API key |
| `DATABASE_URL` | Auto | PostgreSQL connection string (auto-provided by Render) |
| `MLB_CACHE_PATH` | No | Cache path (default: `/data/cache`) |
| `MLB_OUTPUT_DIR` | No | Output path (default: `/data/output`) |
| `MLB_BACKUP_DIR` | No | Backup path (default: `/data/backups`) |
| `MLB_TIMEZONE` | No | Timezone (default: `America/New_York`) |
| `MLB_ENVIRONMENT` | No | Environment name (default: `local`) |
| `MLB_SCHEDULER_ENABLED` | No | Enable scheduler (default: `true`) |
| `MLB_SHADOW_MODE` | No | Shadow mode (default: `true`) |
| `MLB_LOG_LEVEL` | No | Log level (default: `INFO`) |
| `MLB_LOG_FORMAT` | No | Log format (default: `json`) |

## Persistent Storage

Render provides a 1GB persistent disk mounted at `/data`. This stores:
- Database (`/data/mlb_model.db`)
- API cache (`/data/cache`)
- Pipeline output (`/data/output`)
- Backups (`/data/backups`)

## Services

### PostgreSQL Database
- **Plan**: Starter ($7/mo)
- **Auto-provided**: `DATABASE_URL` env var wired to web and worker
- **Database**: `mlb_model`

### Web Service (Dashboard)
- **Command**: `streamlit run src/control_panel.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
- **Port**: Auto-assigned by Render
- **Plan**: Starter ($7/mo)

### Background Worker
- **Command**: `python -m src.worker`
- **Plan**: Starter ($7/mo)
- **Schedule**: Daily 8:30 AM ET base (worker handles sub-daily scheduling internally)

## First Deployment

1. Push repository to GitHub
2. In Render Dashboard, click **New** → **Blueprint**
3. Select the repository
4. Render reads `render.yaml` and creates all 3 services (web, worker, PostgreSQL)
5. Set environment variables in the Dashboard (SPORTSODDS_API_KEY)
6. Deploy
7. On first deploy, run migration to populate PostgreSQL:
   ```bash
   # SSH into the web service or run locally with DATABASE_URL set
   python scripts/migrate_sqlite_to_postgres.py
   ```

## Health Verification

After deployment:
1. Open the dashboard URL
2. Go to **🏥 System Health** tab
3. Verify all checks pass:
   - Database: OK
   - Worker Heartbeat: ACTIVE
   - Persistent Storage: Detected
   - API Key: Configured
   - Scheduler: Enabled
   - Timezone: America/New_York

## Mobile Access

The dashboard is accessible at the public Render URL on any phone browser.
- Page loads responsively
- Tabs render correctly
- Tables are scrollable
- Buttons are touch-friendly

## Rollback

1. In Render Dashboard, go to the service
2. Click **Manual Deploy** → **Deploy previous build**
3. Select the last working version

## Database Restore

```python
from src.backup_database import backup_database, restore_database
restore_database("backups/mlb_backup_YYYYMMDD.db", "/data/mlb_model.db", confirm=True)
```

## Expected Monthly Cost

| Service | Plan | Cost |
|---------|------|------|
| Web (Streamlit) | Starter | $7/mo |
| Worker | Starter | $7/mo |
| PostgreSQL | Starter | $7/mo |
| Persistent Disk | 1GB | Included |
| **Total** | | **~$21/mo** |

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Copy environment variables
cp .env.example .env
# Edit .env with your API key

# Run dashboard locally
python -m streamlit run src/control_panel.py

# Run worker in one-shot mode
python -m src.worker --run-once

# Run tests
python -m pytest tests/ -v
```
