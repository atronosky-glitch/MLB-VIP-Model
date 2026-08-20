# AI_CONTEXT.md

## Purpose

This repository is a multi-league sportsbook market-analysis platform.
MLB is the only league in active production. NFL has a verified, tested
market registry and settlement adapter but has not yet been run in
production. WNBA has a verified, live-working game-market registry
(moneyline/spread/total) plus 8 live-verified player-prop markets
(points/rebounds/assists/threes/PRA/pts+reb/pts+ast/reb+ast, gated behind
team-scoped player-identity resolution) via The Odds API — a different
provider than MLB/NFL use, since SportsGameOdds does not offer WNBA at any
tier — and a working settlement adapter for both, but still no production
schedule (see `docs/MARKET_CAPABILITY.md`). Game-level settlement
(moneyline/spread/total) is now shared across all three leagues via
`src/game_settlement.py`. See "Multi-League Architecture" below.

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
- Never fake support for a market or league. Verify a live API response
  (real oddIDs, real `/markets` catalog liquidity) before registering
  anything in a `src/sports/<league>.py` registry.

## Multi-League Architecture

`src/sports/` is the per-league adapter package (added 2026-08-19). Core
analysis logic (`market_analysis.py`, `player_prop_analysis.py`,
`official_picks.py`, `grading.py`, `model_scoring.py`, `market_quality.py`,
`confidence.py`) was already sport-agnostic before this existed — it
operates on parsed odds rows and `market_type` strings generically, with
one shared set of statistical thresholds across every league (there was no
strong reason found to vary EV/quality thresholds per sport; that remains
a well-isolated future change if evidence ever supports it).

```text
src/sports/base.py    — MarketConfig dataclass + match_ou_market/match_yn_market,
                         parameterized by an explicit registry list. Fully
                         sport-agnostic; every field describes a pattern in the
                         SportsGameOdds oddID grammar, not sport-specific rules.
src/sports/__init__.py — get_league(league), supported_leagues(),
                          available_leagues(), market_capability_report().
                          Lazy submodule imports (see the docstring for why —
                          avoids a circular import through prop_config.py).
src/sports/mlb.py     — wraps the existing prop_config.MARKET_REGISTRY and
                         mlb_results.py. Zero new logic.
src/sports/nfl.py     — 11-market registry built from a verified live catalog.
                         get_settlement_module() -> src/nfl_results.py.
src/sports/wnba.py    — AVAILABLE = True, 3-market registry (moneyline/
                         spread/total), ODDS_PROVIDER = "the_odds_api"
                         (different provider than MLB/NFL — see below).
                         get_settlement_module() -> None (not built yet).
```

Each adapter exposes: `LEAGUE_ID`, `SPORT`, `AVAILABLE`,
`UNAVAILABLE_REASON`, `get_market_registry()`, `get_settlement_module()`
(a module with `ingest_results_for_recommendations(conn, recommendations)`,
or `None`). A league may also expose `ODDS_PROVIDER` (default implicitly
`"sportsgameodds"`) and, if not the default, a `fetch_and_parse(event_id=None)
-> (odds_rows, audit_rows, normalized_events, from_cache)` entry point —
this is how WNBA plugs in a completely different data provider (The Odds
API, not SportsGameOdds) while the rest of the pipeline stays generic;
`player_prop_scanner.run_scan()` and `daily_pipeline._stage_fetch_events`
both branch on it.

Every genuinely MLB-coupled call site now takes an optional `league`/
`registry` parameter defaulting to MLB, so no existing caller's behavior
changed: `player_prop_parser.parse_player_props(event, registry=None)`,
`player_prop_scanner.run_scan(..., league="MLB")`,
`daily_pipeline.PipelineConfig.league` (+ `--league` CLI flag),
`src/worker.py::_run_catchup_grading` (dispatches unresolved
recommendations to each row's own league's settlement module).

`database/db_manager.py::init_db` adds `league` (and, on
`historical_recommendations`/`official_picks`, `sport`) columns to every
table that carries per-event or per-recommendation data, default
`'MLB'`/`'baseball'` — additive, idempotent, tested against a simulated
pre-migration database (`tests/test_multi_league_migration.py`).

See `CHANGELOG.md` → 2026-08-19 entries for the full narrative, and
`docs/MARKET_CAPABILITY.md` for the per-league market/liquidity audit.

`production_canary.py` is now multi-league (`--league` flag,
`_validate_market_mappings` resolves the right registry via
`src.sports.get_league`); `live_readiness.py` was left as-is since its
checks are infrastructure-level (API key, DB, disk, timezone), not
league-specific market data.

WNBA odds ingestion (`src/odds_api_client.py`, `src/wnba_odds_parser.py`)
is a genuinely separate wire format from SportsGameOdds — no oddID
grammar, nested `bookmakers[].markets[].outcomes[]` objects, no stable
player ID for props. The parser's job is entirely to normalize this into
the *same* generic odds-row schema (`event_id`/`sportsbook`/`player_id`/
`player_name`/`market_type`/`side`/`line`/`price`/`decimal_odds`/
`market_group_key`/`validation_status`/...) that `player_prop_parser.py`
already produces, so `player_prop_scanner.py`'s grouping and
`player_prop_analysis.py`'s LOO consensus/EV/market-quality logic need
zero changes to work with WNBA rows. Verified end-to-end against real
live data (5 games, 210 approved rows, 25 ranked opportunities).

As of 2026-08-20: `src/customer_view.py` (customer-facing UI) is now
multi-league (MLB/NFL/WNBA), and `src/wnba_results.py` (settlement) and
WNBA player props (gated on `src/player_identity.py`) are built — see
`docs/SESSION_HANDOFF.md` for the full account. Still not touched: full
`control_panel.py` redesign (only the run-button league selector and
picks-query columns were added — this is the internal admin dashboard,
separate from `customer_view.py`), Pinnacle sharp-reference pricing for
NFL (unverified), and NFL/WNBA production scheduling cadence (both are
built and tested but nothing runs either on a cron/schedule yet).

## Data Provider / Cost Policy

Operator directive (2026-08-19): keep costs low, but data quality matters
more than avoiding every expense. Priority order before recommending
anything paid: (1) can an existing API/source cover it for free, (2)
search for legitimate free APIs/tiers, (3) can multiple free sources
combine safely, (4) if a paid provider would materially help, present it
with full details and let the operator decide — never subscribe
automatically.

Applied to the WNBA gap: confirmed SportsGameOdds has no WNBA at any tier
(checked their own pricing page, not just our account). ESPN's free public
API covers WNBA results/boxscores (same pattern as MLB StatsAPI and ESPN
NFL). The operator provided a free The Odds API key
(`THE_ODDS_API_KEY` in `.env`, gitignored, never logged/printed/committed)
— confirmed live: WNBA game odds (moneyline/spread/total, 9 books) and
player props (8 markets registered as of 2026-08-20, 3-4 books) both work
on the free tier. A sustained *daily* game-markets-only cadence fits
comfortably in the free 500-credits/month budget (~90/month); daily
game+props for every game would exceed it, so the $30/month tier will
likely be needed for a scheduled props cadence — not needed yet since
`fetch_and_parse_props()` is opt-in, not run on any schedule. Nothing has
been subscribed to; see `docs/MARKET_CAPABILITY.md` for the exact math.

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

This diagram is MLB's concrete production path (still the only league
live in production). NFL runs through the identical modules with a
different `league`/`registry` argument — see "Multi-League Architecture"
above rather than assuming this diagram is MLB-exclusive by design.

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

Verified by direct count against `MARKET_REGISTRY` (2026-08-19): the registry
currently contains **24** entries. `PROJECT_STATUS.md` and `TODO.md` agree with
this count. Earlier drafts of this file listed 10 (and other documentation
referenced 8 or 21) — those were stale snapshots from before later expansion
phases, not a real discrepancy in the code.

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

Verified 2026-08-19: `_write_completion_flag()` correctly uses `state.pipeline_run_id`. The `state.run_id` mismatch this file previously flagged was already fixed by an earlier session.

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
- Some dashboard SQL remains SQLite-oriented.
- Market Intelligence may be empty because normal player-prop scans do not persist raw prop observations.

Verified 2026-08-19 and no longer accurate — earlier drafts of this file
flagged a missing `run_data_quality_checks` function and a `capture_closing_prices`
import from `src.grading`; both were already corrected by an earlier session
(the dashboard imports `get_critical_findings`/`init_findings_table` from
`src.data_quality`, and `capture_closing_prices` is imported from
`database.db_manager`). Confirmed by direct import.

## Worker

`src/worker.py` supports persistent, one-shot, and specific-job modes.

```bash
python -m src.worker
python -m src.worker --run-once
python -m src.worker --job morning-run
```

It manages heartbeats, job locking, stale-job recovery, morning pipeline execution, pregame scheduling, grading, backups, health checks, API quota warnings, and adaptive-learning collection.

Known risks:

- ~~Job lock acquisition is check-then-insert and may race under
  concurrency.~~ Fixed 2026-08-19: `database/db_manager.py::init_db` now
  creates a partial unique index (`idx_sj_running_lock ON
  scheduled_jobs(job_type) WHERE status='running' AND metadata='worker-lock'`)
  after a `_dedupe_running_worker_locks` helper resolves any duplicates a
  pre-existing database already held. `_acquire_lock`'s existing
  `except Exception: return None` already turns the resulting constraint
  violation into a clean "lock not acquired" — no logic change was needed
  there, only the constraint. See
  `tests/test_phase19a_startup.py::test_scheduled_jobs_running_lock_unique_index_blocks_concurrent_insert`.
- Persistent scheduling relies on polling and narrow time windows.
- Several annotations and assumptions still refer to raw SQLite connections.
- Backup implementation (`src/backup_database.py`) is still SQLite-only, but
  `src/worker.py::_run_backup` now checks `get_database_url()` first and skips
  with `status="skipped", reason="postgresql_managed_externally"` instead of
  silently creating an empty SQLite file at the stale default `database_path`
  and reporting false success (fixed 2026-08-19; see
  `tests/test_automation_fixes.py::TestBackupSkipsUnderPostgres`).

## Safety and Delivery

Relevant modules include `shadow_mode.py`, `live_readiness.py`, `production_canary.py`, `delivery_gate.py`, `promotion.py`, `manual_checklist.py`, `data_quality.py`, `audit_trail.py`, and `shadow_dashboard.py`.

Shadow mode is enabled by default. Public delivery is blocked. No bets are placed. Live delivery requires explicit operator action and multiple checks.

Fixed 2026-08-19: `live_readiness.py` and `production_canary.py` previously targeted `api.sportsdata.io` with Bearer auth and a completely different response schema (`EventId`/`HomeTeam`/`PregameOdds`/...) — a different provider than production actually uses. Both now call the real `SportsGameOddsClient` (SportsGameOdds v2, `x-api-key` auth) and validate the real event/odds schema (`eventID`/`teams`/`odds`/`byBookmaker`/...), verified field-for-field against a cached production response. `production_canary.py::_validate_market_mappings` also had a live bug — it called `.values()` on `MARKET_REGISTRY`, which is a `list`, so it would raise `AttributeError` the first time it processed a nonempty market list; it now uses the existing `match_ou_market`/`match_yn_market` helpers from `src.prop_config`. Both modules' database checks are now dialect-aware (PostgreSQL via `information_schema` / SQLite via `sqlite_master`), matching the pattern already used in `health_check.py`. See `tests/test_phase11_readiness.py`.

## Render Deployment

`render.yaml` defines:

- `mlb-vip-dashboard` web service
- `mlb-vip-worker` worker service
- `mlb-postgres` PostgreSQL database

Production uses `DATABASE_URL`, `SPORTSODDS_API_KEY`, optional `PINNAPI_API_KEY`, persistent `/data/cache`, `/data/output`, and `/data/backups`, `America/New_York`, JSON/INFO logging, and shadow mode enabled.

Expected cost is approximately $21/month.

## Testing

Tests are intended to use deterministic fixtures and isolated SQLite databases. Important groups cover parsing, mapping, props, scanners, pipeline, grading, markets, automation, shadow/readiness, dashboard, PostgreSQL conversion, adaptive learning, Pinnacle, and API authentication.

Verified 2026-08-20 by running `python -m pytest tests/ -q` directly, checked repeatedly (after nearly every edit) through the player-identity/settlement/CLV/website work, the NFL/WNBA scheduling work, and two rounds of real end-to-end deployment/data-source validation: **1766 passed, 0 failed** (was 1568 at the start of the day). One flaky test was found (two WNBA scheduling tests computed a synthetic tip-off time from the real wall clock without mocking `now`) and fixed by mocking `_now_local()`. Separately, running the REAL pipeline against real live data (not just tests) found real bugs the test suite's fixtures never exercised, including — most importantly — that `src/api_client.py::get_events()` was sending a `date` query parameter that **doesn't exist** on the SportsGameOdds API (the real filters are `startsAfter`/`startsBefore`, confirmed against the live API reference docs), and `_parse_status()` was looking for a `"state"` key the real API response never has. An **initial** live-data pass mischaracterized this as an account/tier limitation ("the key only returns 2024 demo data") — a rigorous follow-up investigation (real API docs, systematically varying the exact call shape) found the real production call (`odds_available=True`) was unaffected all along, and root-caused + fixed the two actual bugs instead. See `docs/SESSION_HANDOFF.md` → "SportsGameOdds investigation" for the full corrected account — read that entry, not the one immediately before it, if the two seem to disagree. See `CHANGELOG.md` for exactly which tests were added when. Do not claim the suite is green without rerunning it yourself — environment-sensitive failures (missing `.env`, missing `logs/`) are a real failure mode elsewhere (e.g. CI or a fresh clone) even though this checkout is currently clean. **Passing tests are necessary but not sufficient, and neither is a first live-data pass** — verify the exact call shape/parameters against real API docs before concluding an account or key is the bottleneck.

## Current Git State

Latest known HEAD:

```text
e60449e Implement Sportsbook Ticker theme styling
```

Recent work includes dashboard redesign/theme styling, Pinnacle feed integration, Pinnacle diagnostics, API authentication fail-fast behavior, and worker scheduling fixes.

Do not assume project-memory files fully reflect the current branch.

## Known Unfinished Work

- ~~Operator confirmation needed: local `.env` key returns stale 2024 data~~ **Resolved 2026-08-20**: this was `src/api_client.py::get_events()` sending a nonexistent `date` param plus a `_parse_status()` bug, not the account/key. Fixed and live-verified — MLB/NFL both confirmed generating real current recommendations end-to-end. See `docs/SESSION_HANDOFF.md` → "SportsGameOdds investigation".
- Alt-line scanning
- Y/N settlement (still requires a verified numeric fact; still not automatic for every YN market)
- Full PostgreSQL production verification
- Portfolio optimization and correlation analysis
- ~~Multi-league support~~ Architecture done 2026-08-19 (NFL added, WNBA unblocked). ~~Automated post-game settlement~~ done 2026-08-20, shared across MLB/NFL/WNBA (`src/game_settlement.py`). ~~Reliable CLV observation scheduling~~ line-movement-aware CLV done 2026-08-20. ~~Website~~ multi-sport pick lifecycle done 2026-08-20 (`src/customer_view.py`). ~~NFL/WNBA scheduling logic~~ done 2026-08-20 (`src/league_schedule.py`, `src/odds_api_credits.py`, worker.py wiring, per-league health) — remaining: actually deploy it (wire into the Render worker service; nothing runs on Render for NFL/WNBA yet), full `control_panel.py` multi-league pass beyond the new health tab.
- Long-term intelligence architecture (independent predictive models, champion/challenger, line-movement prediction, BET NOW vs WAIT, middle detection) — deliberately deferred per operator's 2026-08-20 "architecture readiness, not build now" instruction

## Recommended Priorities

Items 1-8 were verified/fixed 2026-08-19, in two sessions that day: a
debt-fixing pass, then (per explicit operator directive, ahead of the
original "only after production stability" ordering below) the
multi-league architecture and NFL work. See `docs/SESSION_HANDOFF.md` and
`CHANGELOG.md` for the full narrative. Remaining:

1. ~~Establish a reliable green test baseline.~~ Done — 1568 passed, 0 failed.
2. ~~Reconcile active-market documentation with `MARKET_REGISTRY`.~~ Done — 24 confirmed for MLB.
3. ~~Fix the production canary and readiness API mismatch.~~ Done (MLB only — not extended to NFL).
4. Verify the Render PostgreSQL dashboard and worker end to end (external Render access required — not verifiable from this checkout).
5. ~~Fix the completion-flag bug.~~ Already fixed by an earlier session; confirmed.
6. ~~Fix dashboard references to missing or misplaced functions.~~ Already fixed by an earlier session; confirmed.
7. Persist Pinnacle approval and reference metadata.
8. ~~Fix the job-lock race condition in `src/worker.py::_acquire_lock`.~~ Done 2026-08-19.
9. **Operator action required, still open**: confirm/complete Pinnacle API key rotation on Render (flagged since 2026-08-06 after a key was pasted into chat; operator confirmed 2026-08-19 this is still unresolved).
10. ~~Consider multi-league expansion.~~ Done 2026-08-19/2026-08-20 (NFL added, WNBA unblocked including player props) — see "Multi-League Architecture" above and `docs/MARKET_CAPABILITY.md`.
11. ~~Implement scheduled snapshots and settlement (MLB).~~ Settlement is done and shared across all 3 leagues (`src/game_settlement.py`, 2026-08-20); scheduled snapshots for CLV are also working (line-movement-aware as of 2026-08-20).
12. Implement alt-line scanning.
13. ~~**Operator decision needed**: WNBA data access.~~ Resolved 2026-08-19 (The Odds API, free tier). **New operator decision, only if a sustained props cadence is wanted**: the $30/mo paid tier — a busy multi-game WNBA day can consume 80-120+ credits against the 500/month free budget even with per-event dedup (`src/sports/wnba.py::_recently_captured_prop_event_ids`); live-verified 436/500 remaining as of 2026-08-20. See `docs/MARKET_CAPABILITY.md` → WNBA cost note. Nothing purchased.
14. ~~Design NFL/WNBA production scheduling.~~ Done 2026-08-20 — `src/league_schedule.py` (kickoff-time-driven NFL, credit-aware WNBA), `src/odds_api_credits.py`, worker.py wiring (per-league job types, per-league locks, resilient grading), `src/league_health.py`. **Still not deployed** — nothing runs any of this on Render yet; wiring the worker service's actual cron/persistent loop to include the new checks is the concrete next step, not a design question.
15. ~~Customer-facing multi-league UI (`src/customer_view.py`).~~ Done 2026-08-20 — MLB/NFL/WNBA, filters, fair odds/confidence/CLV fields, Performance Dashboard with breakdowns. A full `control_panel.py` (internal admin) multi-league pass beyond the new Multi-League Health tab is still open.
16. Deploy NFL/WNBA scheduling to Render (see item 14) — this is what actually starts NFL/WNBA accumulating a live track record.
17. Long-term intelligence architecture (independent predictive models, champion/challenger, calibration, line-movement prediction, BET NOW vs WAIT, middle detection) — deliberately not built yet; the underlying data is now being captured (`historical_recommendations` carries confidence/features/raw_line/CLV/line-movement-direction for every recommendation).
16. Website (full market-visualization site, beyond the existing Streamlit admin dashboard and customer Render service) — the original long-term ask, now unblocked by a league-agnostic data layer.

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
