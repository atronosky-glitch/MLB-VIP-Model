# Live Readiness

The live-readiness command validates all prerequisites before enabling live production mode.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Ready |
| 1 | Ready with warnings |
| 2 | Not ready |
| 3 | Config failure |
| 4 | Network failure |
| 5 | DB failure |

## Checks performed

1. **api_credentials** — API key configured and valid length
2. **api_connectivity** — API reachable (skippable with `--skip-network`)
3. **database** — Required tables exist
4. **database_writable** — DB file writable
5. **cache_writable** — Cache directory writable
6. **output_writable** — Output directory writable
7. **timezone** — Valid timezone string
8. **system_clock** — Clock within reasonable range
9. **disk_space** — Sufficient disk space
10. **shadow_mode** — Shadow mode status
11. **live_acknowledgement** — Live data acknowledged
12. **scheduler** — Scheduler configured
13. **backup_config** — Backup retention configured
14. **health_thresholds** — Freshness threshold configured
15. **integrations** — Discord/Sheets status
16. **discord_config** — Discord webhook validation
17. **sheets_config** — Google Sheets credential validation
18. **last_jobs** — Recent job run status

## CLI

```bash
# Basic readiness check
python -m src.live_readiness

# JSON output
python -m src.live_readiness --json

# Skip network checks
python -m src.live_readiness --skip-network

# Strict mode (warnings become failures)
python -m src.live_readiness --strict

# Persist live-data acknowledgement
python -m src.live_readiness --acknowledge-live-data
```

## First-run acknowledgement

Before enabling live data, run:

```bash
python -m src.live_readiness --acknowledge-live-data
```

This persists a signed acknowledgement to `data/.live_acknowledgement.json`.
