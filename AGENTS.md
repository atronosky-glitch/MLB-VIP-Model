# AGENTS.md — Permanent instructions for every OpenCode session

## Project purpose

This project is an MLB sportsbook market-analysis platform that identifies:

- sportsbook pricing outliers
- positive expected-value opportunities
- closing-line-value opportunities
- slow-moving sportsbooks
- market disagreement

It is not primarily a game-winner prediction model.

## Current architecture

```
SportsGameOdds API (v2)
  → src/api_client.py          (fetch + cache + retry + rate limiting)
  → src/odds_parser.py         (MLB event odds, validation, audit)
  → src/player_prop_parser.py  (player prop parser — O/U + YN for 21 market types)
  → src/validation_constants.py (shared status codes)
  → database/db_manager.py     (SQLite storage, migrations, recommendations, grading)
  → src/market_analysis.py     (consensus, EV, CLV, slow-book)
  → src/player_prop_analysis.py (LOO consensus, dual-status, YN price comparison)
  → src/prop_config.py         (centralised thresholds, MarketConfig registry)
  → src/player_prop_scanner.py (generic scanner pipeline — market/form resolution, filtering, display)
  → src/strikeout_scanner.py   (backward-compat wrapper around player_prop_scanner)
  → src/grading.py             (O/U grading, YN grading, CLV, performance summaries)
  → src/grade_recommendations.py (grading CLI)
  → src/analytics.py           (historical analytics — ROI, CLV, hit rate by market/sportsbook/EV/odds)
  → src/calibration.py         (recommendation calibration — threshold analysis by EV bucket)
  → src/bookmaker_scores.py    (bookmaker quality rankings — CLV, ROI, disagreement)
  → src/confidence.py          (confidence scoring — weighted measurable components)
  → src/reports.py             (CSV report generation — 5 report types)
  → src/daily_pipeline.py      (9-stage production pipeline — validate, fetch, ingest, scan, freeze, report)
  → src/production_config.py   (production configuration — env vars, secrets, validation)
  → src/structured_logging.py  (JSON + human log formatters, job context injection)
  → src/production_jobs.py     (job orchestration CLI — morning-run, pregame-run, backup, calibrate, etc.)
  → src/scheduler.py           (cron/Windows Task Scheduler/GitHub Actions config generator)
  → src/health_check.py        (production health monitoring — DB, disk, freshness, integrations)
  → src/message_formatter.py   (recommendation messages — Discord/Slack formatting, chunking)
  → src/export_sheets.py       (Google Sheets export — batch updates, fingerprint dedup)
  → src/discord_delivery.py    (Discord webhook delivery — retry, rate limiting, chunking)
  → src/backup_database.py     (SQLite online backup API, compression, retention, restore)
  → src/shadow_mode.py         (shadow mode config — default ON, delivery blocking)
  → src/api_usage.py           (API usage accounting — per-request tracking, quota warnings)
  → src/data_quality.py        (data-quality monitoring — 15 checks, critical detection)
  → src/audit_trail.py         (recommendation traceability — lifecycle recorders, secret redaction)
  → src/live_readiness.py      (live readiness — 18 checks, acknowledgement, exit codes)
  → src/production_canary.py   (canary test — minimal live test, schema validation)
  → src/delivery_gate.py       (delivery safety — 6-factor gate, enable/disable)
  → src/shadow_dashboard.py    (shadow dashboard — aggregated summary)
  → src/promotion.py           (promotion criteria — 7 criteria, YN review tracking)
  → src/manual_checklist.py    (pre-live checklist — 18 verification items)
  → src/control_panel.py       (Streamlit local UI — one-click run, recommendations, status, safety, market intelligence, adaptive learning tabs)
  → src/adaptive_learning.py   (adaptive learning engine — grade analysis, score calibration, learning recommendations, champion/challenger, versioning, safety)
  → src/worker.py              (background worker — persistent/one-shot/specific-job, heartbeat, scheduling, stale-job recovery)
  → main.py                    (CLI entry point)
  → tests/                     (1367 tests, isolated in-memory DB)
```

Phase 17 is complete — cloud deployment on Render with persistent automation.
Future stages: alt-line scanning, website, multi-league support.

## Non-negotiable engineering rules

- Never guess API fields. Inspect raw responses before adding a market.
- Never identify participants from array order, price sign, or favorite status.
- Use stable entity IDs (statEntityID for teams, playerID for player props).
- Suspicious records stored for audit but excluded from calculations.
- Only centralised approved validation statuses (VALID, CONFIRMED, VERIFIED) may enter analysis.
- Do not automatically swap participant mappings based on consensus.
- Compare only identical markets: exact event, player, line, side, period, alt-line status.
- Use leave-one-sportsbook-out (LOO) consensus when evaluating a sportsbook.
- EV = fair_probability * decimal_odds - 1
- A valid market does NOT automatically mean a valid bet.
- Keep `market_quality_status` separate from `bet_status`.
- Never call something a recommendation if EV is zero or negative.
- Do not add multiple market types at once. Add one end-to-end, verify, then continue.
- All migrations must preserve existing data.
- Tests must use isolated temporary databases (conftest.py in-memory fixture).
- Never run tests against the production database (mlb_model.db).
- Never claim a stage is complete while tests are failing.
- Never add a feature without tests.
- Never remove a validation gate to make a test pass.
- Never manufacture positive-EV examples in live output.
- Never place bets automatically.

## Configuration

Threshold configuration lives in `src/prop_config.py`.  The analysis module
`src/player_prop_analysis.py` imports the config module as `cfg` and accesses
values at runtime via `cfg.STRONG_EDGE_THRESHOLD`, `cfg.POSITIVE_EDGE_THRESHOLD`,
etc.  Mutating `prop_config` at runtime propagates immediately to analysis
functions.  Every test restores original threshold values in a `finally` block.

## Pipeline

The daily production pipeline (`src/daily_pipeline.py`) runs 9 stages:
1. Validate configuration (API key, DB writability, registry integrity)
2. Create pipeline run (UUID run ID)
3. Fetch events (API or cache)
4. Ingest odds (parse, save, log)
5. Validate data (approved rows, freshness)
6. Scan markets (generic scanner)
7. Freeze recommendations (persist, dedup)
8. Produce reports (CSV, JSON, text)
9. Print terminal summary

Exit codes: 0=success, 1=success_no_recs, 2=config_failure, 3=db_failure, 4=api_failure, 5=validation_failure, 6=unexpected_failure

## Required workflow for every task

1. Read `AGENTS.md`.
2. Read `PROJECT_STATUS.md`.
3. Read `TODO.md`.
4. Read `docs/SESSION_HANDOFF.md`.
5. Inspect the relevant code.
6. Make the smallest safe change.
7. Run targeted tests.
8. Run the full suite (`python -m pytest tests/ -v`).
9. Update project memory files.
10. Write a new session handoff before stopping.

## Definition of done

A task is complete only when:

- implementation is finished
- targeted tests pass
- full suite passes
- live verification is performed when relevant
- documentation is updated
- `PROJECT_STATUS.md` is current
- `TODO.md` is current
- `docs/SESSION_HANDOFF.md` is updated

## Automatic maintenance rule

Before ending any future coding session, OpenCode must update:

- `PROJECT_STATUS.md`
- `TODO.md`
- `docs/DECISIONS.md` if an architecture decision changed
- `docs/SESSION_HANDOFF.md`

A session is not considered complete until those files reflect the current repository.
