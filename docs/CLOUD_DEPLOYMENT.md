# Cloud Deployment Guide

## Overview

The MLB VIP Model can be deployed to a cloud server (AWS EC2, DigitalOcean Droplet, etc.) for 24/7 automated operation with phone access via the Streamlit control panel.

## Prerequisites

- Python 3.11+
- SQLite (bundled with Python)
- API key: `SPORTSODDS_API_KEY`
- Streamlit installed: `pip install streamlit`

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url> mlb_model
cd mlb_model
pip install -r requirements.txt
```

### 2. Set environment

```bash
export SPORTSODDS_API_KEY="your_key_here"
```

### 3. Initialize database

```bash
python -c "from database.db_manager import init_db; init_db()"
```

### 4. Run initial pipeline

```bash
python -m src.daily_pipeline --output-dir output
```

### 5. Launch Streamlit (phone-accessible)

```bash
python -m streamlit run src/control_panel.py --server.port 8501 --server.address 0.0.0.0
```

Access from phone: `http://<server-ip>:8501`

## Automated Scheduling

### Option A: Python worker (recommended)

```bash
# Run as a persistent background service
nohup python -m src.scheduler --daemon > scheduler.log 2>&1 &
```

The scheduler runs:
- **Morning job**: 9:00 AM ET daily — full pipeline scan
- **Pregame jobs**: 60 minutes before each game start time — refresh odds for that game
- **Postgame grading**: 30 minutes after each game final — grade completed picks

### Option B: Windows Task Scheduler

Use the scheduler module to generate XML:

```python
from src.scheduler import generate_windows_task
generate_windows_task()
# Outputs: output/tasks/mlb_morning.xml
```

Import into Windows Task Scheduler.

### Option C: GitHub Actions

Use the scheduler module to generate workflow YAML:

```python
from src.scheduler import generate_github_actions
generate_github_actions()
# Outputs: output/workflows/mlb_morning.yml
```

## Firewall / Port Access

To access Streamlit from your phone on the same network:

```bash
# Windows (run as Administrator)
netsh advfirewall firewall add rule name="Streamlit" dir=in action=allow protocol=TCP localport=8501
```

Or open port 8501 in Windows Defender Firewall > Advanced Settings.

## Production Checklist

- [ ] API key set and valid
- [ ] Database initialized
- [ ] First pipeline run successful
- [ ] Streamlit accessible on local network
- [ ] Scheduler running (persistent worker or cron)
- [ ] Shadow mode enabled (no real deliveries)
- [ ] Backup schedule configured
- [ ] Health checks passing

## File Structure on Server

```
mlb_model/
├── database/
│   └── mlb_model.db          # SQLite database (auto-created)
├── output/
│   ├── backups/              # Automated backups
│   ├── recommendations.*     # Daily reports
│   └── run_summary.json      # Latest run metadata
├── src/
│   ├── control_panel.py      # Streamlit UI (phone access)
│   ├── scheduler.py          # Automated scheduling
│   ├── automation.py         # Job management service
│   ├── daily_pipeline.py     # Main pipeline
│   ├── official_picks.py     # Pick qualification
│   ├── tracker.py            # P/L tracking
│   └── observations.py       # Odds observations
└── .env                      # Environment variables
```

## Monitoring

- Open Streamlit at `http://<server-ip>:8501`
- Check Automation tab for job status
- Check System Health tab for diagnostics
- Backups created automatically in `output/backups/`

## Security Notes

- Never commit `.env` or `mlb_model.db` to version control
- Use HTTPS proxy (nginx/caddy) for public-facing deployments
- Restrict firewall to trusted IPs when possible
- Shadow mode blocks all external deliveries by default
