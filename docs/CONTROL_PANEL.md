# Control Panel — Local One-Click UI

## Overview

The MLB VIP Model Local Control Panel is a Streamlit-based web interface that provides a one-click "RUN TODAY'S MLB MODEL" button, recommendation table, status dashboard, and safety controls — all without requiring command-line usage.

## Architecture

```
User browser → Streamlit (localhost:8501) → src/control_panel.py → pipeline commands (subprocess)
                                                         ↓
                                            src/production_config.py (env vars)
                                            src/shadow_mode.py (delivery blocking)
                                            src/health_check.py (system health)
                                            src/shadow_dashboard.py (summary)
                                            src/backup_database.py (backup/restore)
```

## Files

| File | Purpose |
|------|---------|
| `src/control_panel.py` | Streamlit app — the main control panel |
| `launch_mlb_model.bat` | Windows launcher — activates venv, checks dependencies, starts Streamlit, opens browser |
| `setup_local_app.bat` | First-time setup — checks Python, creates venv, installs deps, verifies Streamlit, creates dirs |
| `create_desktop_shortcut.ps1` | Creates a Windows desktop shortcut to `launch_mlb_model.bat` |
| `.env.example` | Template for required environment variables |
| `tests/test_phase12_control_panel.py` | 67 tests covering all control panel functionality |

## Quick Start

### First-Time Setup

1. Run `setup_local_app.bat` as Administrator (or normal user)
2. Edit `.env` and set `SPORTSODDS_API_KEY=<your_api_key>`
3. Run `launch_mlb_model.bat` (or double-click desktop shortcut)
4. Browser opens to `http://localhost:8501`

### Daily Usage

1. Double-click the "MLB VIP Model" desktop shortcut (or run `launch_mlb_model.bat`)
2. Click **"RUN TODAY'S MLB MODEL"**
3. View recommendations, filter, sort, export CSV

## UI Sections

### Header
- Title: "MLB VIP Model"
- Shadow mode badge (SHADOW / LIVE)
- Footer: "No wagers are placed. No deliveries are sent."

### Status Cards (6 columns)
1. **Shadow Mode**: Status badge (SHADOW=red, LIVE=green)
2. **Delivery**: Blocked/Active based on shadow mode
3. **Readiness**: Last readiness check status
4. **Health**: Last health check status
5. **Data Quality**: DQ findings count
6. **API Usage**: Quota used percentage

### Pipeline Runner
- **"RUN TODAY'S MLB MODEL"** button (primary)
- Progress bar with stage labels
- Status messages during execution
- Result: success (green), failure (red), running (blue)
- Technical details expandable section (exit code, log lines)
- Automatic recommendation table refresh after success

### Recommendation Table
- Columns: Player, Market, Side, Line, Book, Odds, EV%, YN Advantage, Status, Quality
- Filter by: Market type (multiselect)
- Sort by: EV%, YN Advantage, Player name
- Status badges: BET (green), LEAN (orange), MONITOR (blue), other (gray)
- Download CSV button (filtered data)

### Status Dashboard (collapsible)
- Health check summary with status indicators
- Shadow dashboard summary (recommendations, delivery, DQ findings)

### Backup (collapsible)
- Backup Database button (creates backup in output/backups/)
- Backup list with timestamp, size, compressed status

### Advanced Controls (collapsible)
- Health Check button
- Shadow Dashboard button
- Canary Test button (`--no-write` dry run)
- Pregame Refresh button
- Closing Prices Capture button
- Grade Recommendations button
- View Recommendation Traces button

## Shadow Mode Safety

- **Default**: Shadow mode ON, delivery blocked
- Live delivery requires ALL:
  - `SHADOW_MODE=false`
  - `LIVE_DELIVERY_ACKNOWLEDGED=true`
  - Recent passing readiness check
  - No critical health check failures
  - No critical data-quality findings
  - Valid production config
- Control panel shows delivery status prominently
- No "Enable Live Delivery" button in the UI

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SPORTSODDS_API_KEY` | Yes | — | API key for sportsgameodds.com |
| `MLB_DB_PATH` | No | `database/mlb_model.db` | SQLite database path |
| `MLB_OUTPUT_DIR` | No | `output/` | Report output directory |
| `MLB_LOG_LEVEL` | No | `INFO` | Log level |
| `MLB_LOG_FORMAT` | No | `human` | `human` or `json` |
| `MLB_TIMEZONE` | No | `America/New_York` | Default timezone |
| `DISCORD_WEBHOOK_URL` | No | — | Discord webhook URL |
| `DISCORD_WEBHOOK_URL_VIP` | No | — | VIP Discord webhook URL |
| `SHEETS_CREDENTIALS_FILE` | No | — | Google Sheets credentials path |
| `SHEETS_SPREADSHEET_ID` | No | — | Google Sheets spreadsheet ID |
| `SHADOW_MODE` | No | `true` | Shadow mode (true/false) |
| `LIVE_DELIVERY_ACKNOWLEDGED` | No | `false` | Live delivery acknowledgement |
| `LIVE_DATA_ACKNOWLEDGED` | No | `false` | Live data acknowledgement |
| `MLB_MIN_EV_THRESHOLD` | No | `0.02` | Minimum EV threshold |
| `MLB_MIN_CONFIDENCE` | No | `40` | Minimum confidence score |
| `MLB_FRESHNESS_THRESHOLD` | No | `3600` | Data freshness threshold (seconds) |
| `MLB_CANARY_ENABLED` | No | `false` | Canary test enabled |

## API Quota Impact

Commands that consume live API quota:
- `python -m src.daily_pipeline` — full 9-stage pipeline (~3-5 API calls)
- `python -m src.player_prop_scanner` — scan without ingestion (~1 API call)
- `python -m src.production_canary` — uses separate API (sportsdata.io)
- `python -m src.strikeout_scanner` — scan without ingestion (~1 API call)
- `python -m src.production_jobs morning-run` — runs pipeline + delivery
- `python -m src.production_jobs pregame-run` — runs pipeline + delivery
- `python -m src.production_jobs full-daily` — runs morning + pregame + backup

## Troubleshooting

### Streamlit won't start
- Ensure Python 3.10+ is installed
- Ensure Streamlit is installed: `pip install streamlit`
- Check `launcher.log` for errors

### API key not found
- Ensure `.env` file exists (copy from `.env.example`)
- Ensure `SPORTSODDS_API_KEY` is set in `.env`
- Restart control panel after changing `.env`

### Recommendations table is empty
- Run the pipeline first (click RUN button)
- Check database path in status cards
- Check health check for errors

### Browser doesn't open
- Navigate manually to `http://localhost:8501`
- Check if another Streamlit instance is running on port 8501

## Testing

```bash
# Run Phase 12 control panel tests
python -m pytest tests/test_phase12_control_panel.py -v

# Run full test suite (1021 tests)
python -m pytest tests/ -v
```

## Technical Notes

- **Subprocess execution**: Pipeline runs in a background thread via `subprocess.Popen` to avoid blocking the Streamlit event loop
- **Session state**: `run_active`, `run_log`, `run_result`, `last_run_time`, `last_run_steps` are stored in `st.session_state`
- **Rerun guard**: Minimum 15-minute gap between pipeline runs (prevents accidental double-clicks)
- **Module imports**: Control panel uses lazy imports (after page config) to avoid Streamlit module-level issues
- **Database access**: Read-only queries for table display; write operations only via subprocess pipeline commands
