# AI_CONTEXT.md

## Purpose

This repository is an MLB sportsbook market-analysis platform.

It identifies:

- Sportsbook pricing outliers
- Positive expected-value opportunities
- Closing-line-value opportunities
- Slow-moving sportsbooks
- Market disagreement

It is not primarily a game-winner prediction model. It does not place bets automatically.

## Required Onboarding

Before modifying code, read these files in order:

1. `AI_CONTEXT.md`
2. `PROJECT_STATUS.md`
3. `docs/SESSION_HANDOFF.md`
4. `TODO.md`

Then inspect the relevant implementation and tests.

Do not modify model logic without explicit approval. Do not remove existing documentation. Do not commit changes unless explicitly requested.

## Non-Negotiable Rules

- Never guess API fields. Inspect raw responses first.
- Use stable entity IDs: `statEntityID` for teams and `playerID` for props.
- Never infer participants from array order, price sign, or favorite status.
- Store suspicious records for audit, but exclude them from calculations.
- Only `VALID`, `CONFIRMED`, and `VERIFIED` rows may enter analysis.
- Never automatically swap participant mappings.
- Compare only identical events, players, markets, lines, sides, periods, and alt-line states.
- Use leave-one-sportsbook-out consensus when evaluating a sportsbook.
- EV is `fair_probability * decimal_odds - 1`.
- Keep `market_quality_status` separate from `bet_status`.
- Never call a zero- or negative-EV result a recommendation.
- Never manufacture positive-EV live output.
- Never place bets automatically.
- Preserve existing data during migrations.
- Tests must use isolated databases.
- Never run tests against production `mlb_model.db`.
- Add tests for every feature.
- Never remove a validation gate to make a test pass.
- Run targeted tests, then the full suite.
- Update project memory files after approved implementation work.

## System Architecture

```text
SportsGameOdds API v2
  -> src/api_client.py
  -> src/odds_parser.py / src/player_prop_parser.py
  -> validation and audit records
  -> database/db_manager.py
  -> src/market_analysis.py / src/player_prop_analysis.py
  -> src/player_prop_scanner.py
  -> model scoring and market-quality scoring
  -> official-pick qualification
  -> historical recommendation snapshots
  -> reports and Streamlit dashboard
```

Production automation adds:

```text
src/daily_pipeline.py
  -> src/production_jobs.py
  -> src/worker.py
  -> Render web service and worker
```

## Main Components

### API

`src/api_client.py`

- SportsGameOdds v2 HTTP client
- Uses `SPORTSODDS_API_KEY`
- Sends the key through `x-api-key`
- Caches successful responses as JSON
- Supports cache age limits
- Enforces request spacing
- Retries transient connection, timeout, 429, and 5xx failures
- Fails fast on authentication failures, including auth errors returned as HTTP 500
- Importing this module requires an API key in the environment

`src/pinnacle_feed.py`

- Optional external Pinnacle feed through Pinnapi
- Controlled by `PINNAPI_API_KEY`
- Supplies sharp O/U reference prices when available
- Is separate from SportsGameOdds quota accounting

### Parsing

`src/odds_parser.py`

- Parses team moneyline, spread, and totals markets
- Uses stable team entity identifiers
- Performs participant mapping validation
- Preserves alternate lines
- Produces approved odds rows and mapping-audit rows
- Never auto-corrects suspicious mappings

`src/player_prop_parser.py`

- Parses registry-defined player props
- Uses `playerID`
- Supports O/U and Y/N market formats
- Groups exact lines using stable `market_group_key` values
- Keeps alternate lines separate
- Stores excluded rows in audit output
- Player team enrichment is currently not resolved

`src/validation_constants.py`

- Central source for approved statuses, excluded statuses, confidence values, and validation reasons

### Market Configuration

`src/prop_config.py` contains analysis thresholds, freshness limits, Pinnacle settings, confidence weights, `MarketConfig`, and `MARKET_REGISTRY`.

The current code registry contains 10 active entries:

- `strikeouts`
- `hits_allowed`
- `walks_allowed`
- `outs`
- `earned_runs`
- `pitching_win`
- `batter_hits`
- `total_bases`
- `home_runs`
- `stolen_bases`

Project documentation commonly states that only 8 markets are active. This discrepancy must be resolved before changing market coverage.

Pinnacle settings currently include:

- `USE_PINNACLE_VALUE_MODEL = True`
- `REQUIRE_PINNACLE_FOR_OFFICIAL = True`
- `PINNACLE_FALLBACK_TO_MARKET_MEDIAN = True`
- `MIN_PINNACLE_EV = 0.04`
- `MIN_PINNACLE_PROB_EDGE = 0.025`

### Analysis

`src/market_analysis.py` handles generic two-way market analysis, including odds conversion, implied probability, vig removal, consensus, EV, CLV, and slow-book detection.

`src/player_prop_analysis.py` handles exact-line O/U grouping, Pinnacle no-vig reference, LOO market-median fallback, per-book EV, market quality, bet status, Y/N price advantage, and Pinnacle diagnostics.

O/U EV uses:

```text
fair_probability * decimal_odds - 1
```

Y/N markets generally use implied probability advantage, relative payout advantage, and decimal odds advantage rather than true EV.

### Scanner

`src/player_prop_scanner.py` is the generic registry-driven scanner. It resolves market/form combinations, fetches and parses events, filters approved rows, supports sportsbook/player/game filters, supports all/positive-only/actionable-only modes, deduplicates and sorts opportunities, tracks scan runs, and emits Pinnacle diagnostics.

It does not normally persist raw player-prop observations to `player_prop_odds`; it persists recommendations instead.

`src/strikeout_scanner.py` is a backward-compatible wrapper around the generic scanner.

## Database

### Connection Layer

`database/connection.py` provides a `DB` wrapper around SQLite or PostgreSQL.

- PostgreSQL is selected when `DATABASE_URL` exists.
- SQLite is selected otherwise.
- SQLite defaults to `database/mlb_model.db`.
- `MLB_DB_PATH` overrides the SQLite path.
- The wrapper performs limited SQL conversion between dialects.
- Full PostgreSQL compatibility is not guaranteed for every module.

`database/db_manager.py` handles database initialization, schema migrations, game and odds persistence, player-prop persistence, run tracking, recommendation persistence, grading, closing-price capture, and adaptive-learning persistence.

### Main Tables

Ingestion:

- `games`
- `odds`
- `raw_responses`
- `data_pulls`
- `player_prop_odds`
- `odds_mapping_audit`
- `player_prop_mapping_audit`
- `scan_runs`
- `ingestion_log`

Recommendations and grading:

- `historical_recommendations`
- `official_picks`
- `event_results`
- `player_stat_results`
- `market_settlements`
- `bet_units`
- `closing_prices`
- `manual_override_audit`
- `pick_observations`

Operations and learning:

- `scheduled_jobs`
- `job_runs`
- `worker_heartbeat`
- `api_usage`
- `data_quality_findings`
- `recommendation_traces`
- `experiments`
- `config_versions`
- `learning_recommendations`

Schema evolution uses repeated `ALTER TABLE ADD COLUMN` attempts rather than versioned migrations.

Important persistence gap: the pipeline computes `pinnacle_approved` and `is_official`, but `save_recommendation()` does not persist all Pinnacle approval/reference fields in `historical_recommendations`.

## Daily Pipeline

`src/daily_pipeline.py` runs nine stages:

1. Validate configuration
2. Create pipeline run
3. Fetch events
4. Ingest odds
5. Validate data
6. Scan markets
7. Freeze recommendations
8. Produce reports
9. Print summary

Outputs:

- `recommendations.csv`
- `recommendations.json`
- `run_summary.json`
- `pipeline_report.txt`

The pipeline filters live/completed games, computes model and market-quality scores, classifies recommendation tiers, deduplicates snapshots, captures closing prices, and writes Pinnacle diagnostics.

Known issue: `_write_completion_flag()` references `state.run_id`, while the state field is `pipeline_run_id`. The exception is swallowed, so the dashboard completion indicator may not update.

## Recommendation Tiers

`src/official_picks.py` defines:

- `OFFICIAL_TRACKED`
- `DISCOVERY_TRACKED`
- `RESEARCH_ONLY`

Official defaults include model score at least 7.0, O/U EV at least 3%, Y/N price advantage at least 3 percentage points, at least 4 contributing books, valid qualification status, maximum 3 picks per day, maximum 1 per game, and Pinnacle approval for O/U official status.

Fallback consensus opportunities remain visible but are not official when Pinnacle approval is required.

Potential issue: official-pick sorting uses `applicable_edge_threshold`, which is a threshold rather than the actual opportunity edge.

## Dashboard

`src/control_panel.py` is a Streamlit application launched with:

```bash
python -m streamlit run src/control_panel.py
```

Current tabs:

1. Today’s Picks
2. Official Picks
3. Research
4. Line Movement
5. Performance
6. Market Intelligence
7. Run & Operations
8. Adaptive Learning

The dashboard uses subprocess-based pipeline execution, database-backed recommendation tables, report files, health/backup/readiness controls, shadow-mode visibility, CSV export, and adaptive-learning views.

Recent commits primarily redesigned the dashboard: `f237e3e`, `e75c841`, `0bb4c48`, and `e60449e`.

Known dashboard risks:

- The app performs substantial work at import time.
- Some tests inspect source rather than importing the full app.
- It references `run_data_quality_checks`, which is not currently defined in `src/data_quality.py`.
- It references `capture_closing_prices` from `src.grading`, while the implementation is in `database.db_manager`.
- Some dashboard SQL remains SQLite-oriented.
- Market Intelligence may be empty because normal player-prop scans do not persist raw prop observations.
- Backup-directory handling is not fully consistent across modules.

## Worker

`src/worker.py` supports persistent, one-shot, and specific-job modes.

```bash
python -m src.worker
python -m src.worker --run-once
python -m src.worker --job morning-run
```

It manages heartbeats, job locking, stale-job recovery, morning pipeline execution, pregame scheduling, grading, backups, health checks, API quota warnings, and adaptive-learning collection.

Known risks:

- Job lock acquisition is check-then-insert and may race under concurrency.
- Persistent scheduling relies on polling and narrow time windows.
- Several annotations and assumptions still refer to raw SQLite connections.
- Backup implementation is SQLite-only while production uses PostgreSQL.

## Safety and Delivery

Relevant modules include `shadow_mode.py`, `live_readiness.py`, `production_canary.py`, `delivery_gate.py`, `promotion.py`, `manual_checklist.py`, `data_quality.py`, `audit_trail.py`, and `shadow_dashboard.py`.

Shadow mode is enabled by default. Public delivery is blocked. No bets are placed. Live delivery requires explicit operator action and multiple checks.

Critical concern: `live_readiness.py` and `production_canary.py` currently target SportsData.io endpoints and schemas, not the actual SportsGameOdds v2 production API. They should not be treated as reliable validation of the live pipeline until corrected.

## Render Deployment

`render.yaml` defines:

- `mlb-vip-dashboard` web service
- `mlb-vip-worker` worker service
- `mlb-postgres` PostgreSQL database

Production uses `DATABASE_URL`, `SPORTSODDS_API_KEY`, optional `PINNAPI_API_KEY`, persistent `/data/cache`, `/data/output`, and `/data/backups`, `America/New_York`, JSON/INFO logging, and shadow mode enabled.

Expected cost is approximately $21/month.

## Testing

Tests are intended to use deterministic fixtures and isolated SQLite databases. Important groups cover parsing, mapping, props, scanners, pipeline, grading, markets, automation, shadow/readiness, dashboard, PostgreSQL conversion, adaptive learning, Pinnacle, and API authentication.

Documented test counts are inconsistent:

- `PROJECT_STATUS.md`: 1,372 passing
- `TODO.md`: 1,389 passing
- Current local run: 1,329 passed, 64 failed, 7 errors

Current local failures include missing environment/cache fixtures, missing `logs/`, missing `.env`, Streamlit bare-mode behavior, and PostgreSQL environment-sensitive tests. Do not claim the suite is green without rerunning and reconciling these failures.

## Current Git State

Latest known HEAD:

```text
e60449e Implement Sportsbook Ticker theme styling
```

Recent work includes dashboard redesign/theme styling, Pinnacle feed integration, Pinnacle diagnostics, API authentication fail-fast behavior, and worker scheduling fixes.

Do not assume project-memory files fully reflect the current branch.

## Known Unfinished Work

- Alt-line scanning
- Historical scheduled snapshots
- Reliable CLV observation scheduling
- Automated post-game settlement
- Y/N settlement
- Full PostgreSQL production verification
- Website
- Multi-league support
- Portfolio optimization and correlation analysis
- Documentation reconciliation

## Recommended Priorities

1. Establish a reliable green test baseline.
2. Reconcile active-market documentation with `MARKET_REGISTRY`.
3. Fix the production canary and readiness API mismatch.
4. Verify the Render PostgreSQL dashboard and worker end to end.
5. Fix the completion-flag bug.
6. Fix dashboard references to missing or misplaced functions.
7. Persist Pinnacle approval and reference metadata.
8. Complete PostgreSQL compatibility and backup support.
9. Implement scheduled snapshots and settlement.
10. Implement alt-line scanning.
11. Consider website and multi-league expansion only after production stability.

## Change Discipline

Before editing:

- Confirm the requested scope.
- Inspect current code and tests.
- Preserve unrelated user changes.
- Make the smallest safe change.
- Add or update tests.
- Run targeted tests.
- Run the full suite.
- Update `PROJECT_STATUS.md`, `TODO.md`, and `docs/SESSION_HANDOFF.md`.
- Update `docs/DECISIONS.md` only when architecture changes.
- Do not commit unless explicitly requested.
