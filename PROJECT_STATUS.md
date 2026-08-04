# PROJECT_STATUS.md — Current project snapshot

## Completed

- **Production health checks and schedule-aware freshness (2026-08-04)** — PostgreSQL now reports managed storage and externally managed backups as healthy without inspecting local SQLite paths or exposing connection details. Missing local backup directories are optional and safely created without becoming system errors. Health now includes failed scheduled jobs and compares freshness with pending/overdue pregame scans and the next expected morning run. Worker pregame jobs now execute the pipeline and record `scan_runs` instead of only rescheduling jobs. Targeted health/dashboard/worker tests: **250 passed**. Full suite: **1427 passed, 1 pre-existing failure** (`tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count`, expects 21 while the current registry contains 24).

- **Dashboard database factory signature fix (2026-08-04)** — `database.db_manager.get_connection()` accepts only an optional `db_path`; `database.connection.get_connection()` accepts `url` and `db_path`. Corrected `src/control_panel.py` and `src/health_check.py` to call the shared db-manager factory without the unsupported `url=` keyword, allowing it to honor `DATABASE_URL` while retaining SQLite fallback behavior. Audited all repository `get_connection(` call sites and found no remaining signature mismatch. Dashboard/health/PostgreSQL tests: **199 passed**. Full suite: **1421 passed, 1 pre-existing failure** (market registry test expects 21 while the current registry contains 24).

- **Render worker database-layer audit (2026-08-04)** — Confirmed `src/worker.py` imports `get_connection` from `database.db_manager`, the shared PostgreSQL/SQLite connection layer, and uses it in persistent worker startup. Replaced remaining SQLite-specific connection type annotations with the shared `DB` wrapper; SQLite PRAGMAs remain explicitly dialect-gated. Added a regression test for the import and absence of raw `sqlite3.connect()` usage. Worker tests: **81 passed**. Full suite: **1421 passed, 1 pre-existing failure** (`tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count`, expects 21 while the current registry contains 24).

- **PostgreSQL dashboard query audit (2026-08-04)** — Reapplied the intended dashboard production-database fixes onto current `main` without reverting newer dashboard work. `src/control_panel.py` now opens the active `DATABASE_URL` connection for every dashboard query, keeps SQLite path checks only for local mode, uses completed `scan_runs` for latest-run and freshness metrics, reads market intelligence from canonical `odds`, uses named row access, and labels the production database PostgreSQL. `src/health_check.py` now discovers PostgreSQL tables through `information_schema`, checks freshness from completed `scan_runs`, and preserves SQLite compatibility. Targeted dashboard/health/PostgreSQL tests: **199 passed**. Full suite: **1420 passed, 0 failed**.

- **Permanent AI onboarding context (2026-08-02)** — Added `AI_CONTEXT.md` with verified architecture, data flow, database, dashboard, worker, Render, testing caveats, risks, and priorities. Updated `AGENTS.md` to require the onboarding files in the order `AI_CONTEXT.md` → `PROJECT_STATUS.md` → `docs/SESSION_HANDOFF.md` → `TODO.md` before code changes. No runtime or model logic changed.
- **API auth fail-fast + Render-verified Pinnacle diagnostics (2026-08-01, commit `72423c7`)** — `src/api_client.py` treats invalid API keys as fatal: `_is_auth_failure(status, text)` returns True for HTTP 401/403 or any ≥400 response whose body carries an auth marker (the real case was HTTP 500 + `"Internal Server Error: Invalid API key"`, previously retried ~6×), → `logger.critical` + `raise APIKeyError` instead of retrying; correct env var `SPORTSODDS_API_KEY` (key via `x-api-key`), masked-key import-time diagnostics. Retries preserved for 429/502/503/504/plain-500/timeouts. Default CLI log level `WARNING→INFO` in `src/daily_pipeline.py` + `src/player_prop_scanner.py` so `PINNACLE_SUMMARY` is visible without `--debug`. `fallback_lean` summary counter now excludes insufficient-book groups (counts groups that reached the lean stage). Live Render run verified: `PINNACLE_SUMMARY total_groups=2575 exact_match=0 reference_used=0 pinnacle_missing=779 line_mismatch=0 one_side=0 model_disabled=0 insufficient_books=1796 ev_threshold_failed=0 prob_edge_threshold_failed=0 no_positive_edge=0 fallback_lean=2121 official_approved=0`, 50 recs (25 O/U + 25 YN), 0 errors, 190.5s. Pinnacle absent from feed → `official_approved=0` is correct Gate 9 behavior. Full suite **1372 passed, 0 failed** (was 1345).
- **Pinnacle + alt-line diagnostics only (Phase 18C)** — observability layer, NO logic/threshold changes. `src/player_prop_analysis.py` adds `_rejection_reason`, `_build_group_diagnostics`, `_fmt_diag`, `_log_group_diagnostics`, `_empty_diagnostics`; every `analyze_prop_group` result dict (main + `_empty_result`) gains a `"diagnostics"` dict (player, market, line, side, total/comparison books, Pinnacle present/books/both-sides/odds/fair probs, best non-Pinnacle book+odds per side, best EV/odds/pinnacle_ev/prob_edge, `official_approved`, `rejection_reason` ∈ approved|missing_pinnacle|pinnacle_line_mismatch|pinnacle_missing_opposite_side|pinnacle_model_disabled|insufficient_comparison_books|ev_threshold_failed|prob_edge_threshold_failed|no_positive_edge) plus one **DEBUG** `PINNACLE_GROUP` log per group. `src/player_prop_scanner.py` adds `_new/_accumulate/_log_pinnacle_summary` (one **INFO** `PINNACLE_SUMMARY` line per run), `_log_line_fragmentation` (**DEBUG** `LINE_FRAGMENTATION` per player+market), a `pinnacle_diagnostics` result key (both normal and cache/empty paths), and a `--debug` CLI flag wired to `logging.basicConfig`. `src/daily_pipeline.py` `_build_run_summary` includes `pinnacle_diagnostics` in `run_summary.json`. Production logging stays quiet by default (DEBUG gated behind `--debug`). 13 new tests (`tests/test_pinnacle_value_model.py` `TestPinnacleDiagnostics` ×7 + `TestScannerPinnacleDiagnostics` ×5 + 1 integration). Full suite **1345 passed, 0 failed** (was 1332).
- **Pinnacle required for official picks (Phase 18B)** — `REQUIRE_PINNACLE_FOR_OFFICIAL` now defaults to `True` in `src/prop_config.py`. `analyze_prop_group` (`src/player_prop_analysis.py`) adds per-book `is_official` (True only when Pinnacle-approved under the REQUIRE flag), group-level `pinnacle_found`/`pinnacle_reference_used`/`pinnacle_book`/`pinnacle_over_price`/`pinnacle_under_price`/`official_count`, a same-line guard that logs `PINNACLE_LINE_FRAGMENTATION` (never merges across lines), and `PINNACLE_CHECK` + `OFFICIAL_BLOCKED_REQUIRE_PINNACLE` debug logs. Fallback/market-median opportunities are STILL displayed (best_ev kept, recommendation=BET) but are always `is_official=False` — previously strict mode suppressed them entirely. `src/player_prop_scanner.py` propagates `is_official` onto opportunities; `src/daily_pipeline.py` `_stage_freeze` passes `pinnacle_approved`/`is_official` into the rec dict; `classify_recommendation` (`src/official_picks.py`) adds a Pinnacle gate (Gate 9, O/U only) so a book without Pinnacle approval can never be OFFICIAL_TRACKED (falls to DISCOVERY/RESEARCH). 11 new tests (`tests/test_pinnacle_value_model.py` 5 required cases + `tests/test_phase15_official_picks.py` 6 gate tests); existing rec helpers updated with `pinnacle_approved=True`. Full suite **1332 passed, 0 failed**. Pinnacle still not in the feed → production runs fallback, so official picks will be rare until a `pinnacle` key appears.
- **Pinnacle-first sharp value model (Phase 18A)** — `analyze_prop_group` (`src/player_prop_analysis.py`) uses Pinnacle no-vig probabilities as the fair reference whenever a `pinnacle` book has BOTH Over and Under at the exact same line. New helpers in `src/player_prop_analysis.py`: `is_pinnacle_book`, `american_to_implied_prob`, `american_to_decimal`, `calculate_no_vig_probs`, `calculate_ev`. Config flags in `src/prop_config.py`: `USE_PINNACLE_VALUE_MODEL=True`, `REQUIRE_PINNACLE_FOR_OFFICIAL=True` (flipped in 18B), `PINNACLE_FALLBACK_TO_MARKET_MEDIAN=True`, `MIN_PINNACLE_EV=0.04` (4%), `MIN_PINNACLE_PROB_EDGE=0.025` (2.5%). Per-book fields: `pinnacle_fair_prob`, `pinnacle_ev`, `pinnacle_prob_edge`, `pinnacle_approved`, `is_official`. Pinnacle's own rows are never targets. Scanner shows a `Pin` column (Y/N/`-`), verbose output prints approval + ref prob + EV + edge, and the results header names the reference source. Pinnacle is still NOT in the SportsGameOdds feed (`betmgm, bovada, caesars, draftkings, espnbet, fanduel, pointsbet, unibet, williamhill`), so production runs the market-median fallback — the Pinnacle path is live-tested and activates automatically if a `pinnacle` key ever appears.
- **O/U opportunities fixed (was 0, now 25)** — `analyze_prop_group` (`src/player_prop_analysis.py`) reworked: consensus computed per-side from ALL books (`set(over_prices) | set(under_prices)`), single-side books contribute to LOO consensus, scanner no longer skips single-side groups. Confirmed live on Render: 25 O/U + 8 YN opportunities, 0 errors, 1797 markets scanned.
- **Pinnacle reference investigation** — Pinnacle is NOT in the SportsGameOdds feed. Live `byBookmaker` keys confirmed via Render shell: `betmgm, bovada, caesars, draftkings, espnbet, fanduel, pointsbet, unibet, williamhill` (9 books). LOO market median is the production reference strategy; the Pinnacle-first branch is implemented, tested, and activates automatically if Pinnacle data appears.
- **Scanner book listing** — `run_scan` prints the distinct sportsbooks found in approved rows (`Books in approved O/U+YN rows (9): ...`).
- **Confirmed `player_prop_odds` stays empty by design** — scanner fetches props directly from the live API (does not persist them); recommendations land in `historical_recommendations`. Same Postgres serves both dashboard and worker (`mlb-postgres`).

- **Phase 17C: Market rationalization** — MARKET_REGISTRY cut from 21 to 8 high-signal markets:
  - **Kept (8)**: pitcher_strikeouts, pitcher_hits_allowed, pitcher_walks_allowed (O/U-only), pitcher_wins (YN-only), batter_hits, batter_total_bases (O/U-only), batter_home_runs, batter_stolen_bases
  - **Dropped (13)**: pitcher_outs, pitcher_earned_runs, pitcher_pitches_thrown, batter_hits_runs_rbi, batter_rbi, batter_runs, batter_runs_rbi, batter_singles, batter_doubles, batter_triples, batter_walks, batter_strikeouts, batter_first_hr
- **Variable Kelly staking** — `compute_variable_stake()` = 25% fractional Kelly × score multiplier [0.25, 2.0] units; 1 unit = 1% bankroll
- **Pipeline completion indicator** — `_write_completion_flag()` in `daily_pipeline.py` writes `.pipeline_completed` JSON; dashboard reads it for green success banner
- **Worker crash fix** — added `from database.db_manager import get_connection` to `src/worker.py:42` (missing import caused Render crash loop)
- **Render PostgreSQL cleanup** — deleted 64 dropped-market `historical_recommendations` + 2 `official_picks` via psql
- **Cleanup script** — `scripts/render_cleanup.py` for future use

- SportsGameOdds API v2 client (`src/api_client.py`) with local JSON caching
- MLB event ingestion (`src/odds_parser.py`)
- SQLite storage (`database/db_manager.py`) — games, odds, raw_responses, data_pulls, bet_results, player_prop_odds, odds_mapping_audit, player_prop_mapping_audit, scan_runs, ingestion_log
- Participant-mapping validation: entityID → team/player, cross-verified by marketName
- Validation statuses (VALID, CONFIRMED, VERIFIED, POSSIBLE_MAPPING_ERROR, INVALID_MAPPING, etc.) persisted per odds row
- Audit records stored separately (provenance per odd_id × sportsbook)
- Approved-status SQL filtering + defense-in-depth analysis filtering (`_filter_approved`)
- Moneyline consensus + EV analysis (`analyze_two_way_market` in `src/market_analysis.py`)
- Pitcher strikeout Over/Under parsing (`src/player_prop_parser.py`)
- playerID-based mapping (no statEntityID for player props; player name extracted from marketName)
- Exact-line grouping with alt-line separation (market_group_key)
- Leave-one-sportsbook-out (LOO) consensus for EV (`src/player_prop_analysis.py`)
- Dual-status model: `market_quality_status` separate from `bet_status`
- Centralised edge thresholds (`src/prop_config.py`): STRONG >= 5%, POSITIVE >= 2%, MARGINAL > 0%
- Isolated in-memory database tests (`tests/conftest.py`)
- **Config refactored** — analysis module imports `prop_config` as a module, not individual names; runtime changes propagate immediately (no longer technical debt)
- **Raised strikeout scanner** (`src/strikeout_scanner.py`) — fetches, parses, stores, analyzes, and displays ranked strikeout opportunities
- **Scanner modes**: all markets, positive-only, actionable-only (with configurable threshold)
- **Configurable freshness** — stale-data warning when odds are older than configurable threshold (default 1 hour)
- **Deduplication** — identical observations for same (event, player, line, side, book) are deduplicated
- **Deterministic sorting** — EV descending, then market quality, n_books, start time, pitcher name, book
- **Market registry** (`MarketConfig` dataclass in `src/prop_config.py`) — `PITCHER_STRIKEOUTS` and `PITCHER_OUTS` defined; parser and scanner dispatch via registry lookup instead of hardcoded filters; backward-compatible constants preserved
- **Daily production pipeline** (`src/daily_pipeline.py`) — 9-stage pipeline from config validation through report generation, with dry-run mode, CLI flags, exit codes, and structured output

## Current test status

Full suite: **1372 passing, 0 failing**. All Pinnacle tests pass (`tests/test_pinnacle_value_model.py` — 42 tests incl. 5 Phase 18B required cases + 14 Phase 18C diagnostics tests; `tests/test_phase15_official_picks.py` — 21 tests incl. 6 Pinnacle-gate tests; `tests/test_api_client.py` — 26 offline/mocked API-auth tests).

Breakdown:
- `tests/test_stage1.py` — 7 tests
- `tests/test_stage2.py` — 15 tests
- `tests/test_stage3.py` — 40 tests
- `tests/test_participant_swap.py` — 27 tests
- `tests/test_player_props.py` — 92 tests
- `tests/test_additional_props.py` — 69 tests
- `tests/test_strikeout_scanner.py` — 25 tests
- `tests/test_player_prop_scanner.py` — 87 tests
- `tests/test_phase5_integrity.py` — 21 tests
- `tests/test_phase6_grading.py` — 77 tests
- `tests/test_daily_pipeline.py` — 74 tests
- `tests/test_phase8_markets.py` — 99 tests
- `tests/test_phase9_intelligence.py` — 41 tests
- `tests/test_phase10_config.py` — 22 tests
- `tests/test_phase10_formatting.py` — 25 tests
- `tests/test_phase10_health.py` — 20 tests
- `tests/test_phase10_backup.py` — 12 tests
- `tests/test_phase10_discord.py` — 11 tests
- `tests/test_phase10_scheduler.py` — 13 tests
- `tests/test_phase10_jobs.py` — 13 tests
- `tests/test_phase10_sheets.py` — 13 tests
- `tests/test_phase11_shadow.py` — 55 tests
- `tests/test_phase11_readiness.py` — 47 tests
- `tests/test_phase12_control_panel.py` — 67 tests
- `tests/test_phase13_dashboard.py` — 47 tests
- `tests/test_phase14_scoring.py` — 50 tests
- `tests/test_phase15_official_picks.py` — 21 tests
- `tests/test_phase16_comprehensive.py` — 55 tests
- `tests/test_phase16b_adaptive_learning.py` — 79 tests
- `tests/test_phase17_cloud.py` — 56 tests
- `tests/test_pinnacle_value_model.py` — 42 tests
- `tests/test_api_client.py` — 26 tests

All tests are **deterministic** — none depend on live API responses or mutable cache data.

## Current supported markets

### Active markets (8 high-signal keepers after Phase 17C rationalization)
- MLB pitcher strikeouts Over/Under + Yes/No
- MLB pitcher hits allowed Over/Under (O/U only)
- MLB pitcher walks allowed Over/Under (O/U only)
- MLB pitcher win Yes/No (YN only)
- MLB batter hits Over/Under + Yes/No
- MLB batter total bases Over/Under (O/U only)
- MLB batter home runs Over/Under + Yes/No
- MLB batter stolen bases Over/Under + Yes/No

### Dropped markets (Phase 17C — removed due to free-tier API limits)
- pitcher_outs, pitcher_earned_runs, pitcher_pitches_thrown, batter_hits_runs_rbi, batter_rbi, batter_runs, batter_runs_rbi, batter_singles, batter_doubles, batter_triples, batter_walks, batter_strikeouts, batter_first_hr (O/U + YN variants) — can be re-enabled by adding MarketConfig entries back

## Unscheduled markets (not implemented)
- Team totals (displayed but not EV-analysed in main.py)
- First five innings (F5)
- Automated scheduling / snapshots
- Google Sheets dashboard
- Discord alerts
- Website

## Known limitations

- Team info not resolved for player props (enrichment would require a league roster lookup)
- CLV tracking requires historical snapshots (not yet scheduled)
- Results grading requires post-game settlement (not yet implemented)
- Freshness check is non-functional for cached data: `captured_at` is set at parse time (always "now"), never from the API `observationTimestamp`. A cache hit always shows age ~0s and never triggers the stale-data warning
- Alt lines are preserved but not scanned by default (only main lines appear in output)

## Current stage

**Completed**: API auth fail-fast (`72423c7`) + live Render verification of Pinnacle diagnostics. `PINNACLE_SUMMARY` INFO emitted by default (log level now INFO), `fallback_lean` counter fixed to exclude insufficient-book groups. Live run: `total_groups=2575 exact_match=0 reference_used=0 pinnacle_missing=779 line_mismatch=0 one_side=0 model_disabled=0 insufficient_books=1796 fallback_lean=2121 official_approved=0`, 50 recs (25 O/U + 25 YN), 0 errors — Pinnacle absent → fallback is production path; official picks require a `pinnacle` key in the feed. Full suite **1372 passed, 0 failed**.

Phase 17B deliverables:
- **Database Connection Layer** (`database/connection.py`): Dialect-aware `DB` wrapper class, auto SQL conversion (`?`→`%s`, `datetime('now')`→`NOW()`, `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`, `AUTOINCREMENT`→`SERIAL`, `sqlite_master`→`information_schema`, `GROUP_CONCAT`→`STRING_AGG`), uniform `DBResult` cursor
- **Dual-mode db_manager** (`database/db_manager.py`): `get_connection()` auto-detects PostgreSQL (`DATABASE_URL`) vs SQLite; all `INSERT OR IGNORE/REPLACE`→`ON CONFLICT`; `sqlite3.IntegrityError`→`Exception`; all type hints updated to `DB`
- **Src file migration** (15 files): All `sqlite3.connect()` calls replaced with `get_connection()` from db_manager across `control_panel.py`, `worker.py`, `production_jobs.py`, `health_check.py`, `promotion.py`, `delivery_gate.py`, `discord_delivery.py`, `export_sheets.py`, `live_readiness.py`, `production_canary.py`, `shadow_dashboard.py`
- **Render Blueprint** (`render.yaml`): Added PostgreSQL database service (`mlb-postgres`, Starter $7/mo); `DATABASE_URL` auto-wired to web+worker via `fromDatabase`; persistent disk retained for cache/output/backups
- **Requirements** (`requirements.txt`): Added `psycopg2-binary>=2.9.9`
- **Migration Script** (`scripts/migrate_sqlite_to_postgres.py`): One-time SQLite→PostgreSQL data migration with `--dry-run`, `--drop-existing`, batch inserts (500 rows), auto table discovery
- **Tests**: 22 new tests in `tests/test_phase17b_postgres.py` (SQL conversion, DB wrapper, dual-mode, idempotent operations, migration script import)
- **Cost**: $21/mo total (web $7 + worker $7 + PostgreSQL $7)

Phase 17 deliverables:
- **Background Worker** (`src/worker.py`, ~350 lines): Persistent mode (signal handling, heartbeat, stale-job recovery, sub-daily scheduling), one-shot mode (cron), specific-job mode. Handles morning scan, pregame checks, grading, backup, adaptive learning, health checks. Job locking with idempotency keys, timezone-aware scheduling.
- **Database Path** (`database/db_manager.py`): Now respects `MLB_DB_PATH` env var (was hardcoded)
- **Production Config** (`src/production_config.py`): Added `backup_dir`, `environment`, `scheduler_enabled`, `shadow_mode` fields + env vars
- **Health Checks** (`src/health_check.py`): 6 new checks (worker_heartbeat, persistent_storage, deployment_environment, timezone, scheduler, backup_directory) — 11 total
- **Dashboard** (`src/control_panel.py`): Enhanced Automation tab with deployment status, worker heartbeat, job metrics, database/storage status, manual triggers with confirmation, production schedule display
- **Deployment Files**: `render.yaml` (Render Blueprint), `Dockerfile`, `Procfile`, `streamlit_config/config.toml`
- **Documentation**: `docs/DEPLOYMENT.md` — complete deployment guide
- **Tests**: 56 new tests across 11 test classes (1367 total)

Phase 16B deliverables:
- **Adaptive Learning Engine** (`src/adaptive_learning.py`, ~1400 lines): Grade analysis, score calibration, learning recommendations, champion/challenger holdout, config versioning, safety rules, chronological splits, adaptive thresholds, bucket calibration, per-market and per-sportsbook analysis
- **Database migration** (`database/db_manager.py`): 3 new tables (`adaptive_experiments`, `config_versions`, `learning_recommendations`) + 10 helper functions + 7 new columns on `historical_recommendations` (calibration_bucket, grade_timestamp, is_high_variance_market, etc.)
- **Dashboard** (`src/control_panel.py`): 9th tab "🧠 Adaptive Learning" with 6 sections — system status gate, data readiness, score calibration bucket analysis, performance by tier, learning recommendations, champion vs challenger holdout, experiments list
- **Test coverage**: 79 new tests across 9 test classes covering grade analysis, calibration, recommendations, champion/challenger, versioning, safety rules, DB storage, dashboard integration
- **Safety**: System is gate-only — all learning recommendations require manual approval before any production config changes

Phase 16A deliverables:
- **Market registry expanded** (`src/prop_config.py`): 21 markets (was 20), added `BATTER_RUNS` with O/U + YN support, `odd_id_stat_prefix='pitching_batterRuns'`
- **3-tier classification** (`src/official_picks.py`): OFFICIAL_TRACKED (score>=7.0, 4+ books, EV>=3%), DISCOVERY_TRACKED (score>=6.0, 3+ books, private research only), RESEARCH_ONLY (everything else); `RULES_VERSION="official_pick_rules_v2"`, new `TIER_DISCOVERY` constant
- **Score diagnostics** (`src/model_scoring.py`): `ScoreResult` expanded with `points_to_7`, `price_outlier_capped`, `true_ev_unavailable`, `one_sided_market`, `insufficient_books_failure`, `contributing_book_count`; `compute_model_score()` computes all diagnostic fields
- **Market Quality Score** (`src/market_quality.py`): NEW module — `compute_market_quality_score()` with 6 weighted components (book_count=0.30, two_sided=0.20, freshness=0.15, mapping_confidence=0.10, price_consistency=0.15, sportsbook_diversity=0.10), 0-10 range
- **Pipeline integration** (`src/daily_pipeline.py`): `_stage_freeze()` now computes MQS + score diagnostics per recommendation
- **Dashboard** (`src/control_panel.py`): Market Intelligence tab (tab 5) with market inventory + MQS rankings; System Health with auto-refresh and specific failure reasons; "Why No Official Picks Today" section; Research tab shows discovery tier separately; `_load_recs` resilient to missing columns (try/except fallback to SELECT *)
- **Database migration** (`database/db_manager.py`): 6 new columns (points_to_7, price_outlier_capped, true_ev_unavailable, one_sided_market, insufficient_books_failure, market_quality_score)

Phase 15 deliverables:
- **Official Pick Qualification** (`src/official_picks.py`): `OfficialPickConfig` dataclass with configurable thresholds (model_score >= 7.0, OU EV >= 3%, YN price advantage >= 3%, min 4 books, QUALIFIED status only); `QualificationResult` dataclass; `classify_recommendation()` function producing 3 tiers
- **Database schema** (`database/db_manager.py`): 10 new columns on `historical_recommendations` — recommendation_tier, qualification_passed, qualification_reasons, disqualification_reasons, contributing_book_count, contributing_books, applicable_edge_metric, applicable_edge_threshold, model_score_threshold, qualification_rules_version; migration-safe via ALTER TABLE
- **Pipeline integration** (`src/daily_pipeline.py`): `_stage_freeze()` calls `classify_recommendation()` after `compute_model_score()`, merges qualification dict into rec before `save_recommendation()`, fallback defaults on exception
- **Dashboard** (`src/control_panel.py`): Official Picks section with tier metrics, official picks table, research-only expander, tier filter dropdown, Tier column in main table, updated disclaimer

Phase 14 deliverables (completed same session):
- **Model Score V1** (`src/model_scoring.py`): 6 weighted components (EV magnitude, odds quality, book consensus, market quality, freshness, YN advantage), score 0-10, versioned with historical tracking
- **Dashboard integration**: Model Score column in recommendation table, score explanation in detail view

Phase 13 deliverables:
- **Live-game filtering** (`src/daily_pipeline.py`): `_is_game_skippable()` checks event status (live/completed/started) and scheduled start time; `_build_matchup()` creates human-readable matchup strings; `_stage_freeze()` filters live/completed games, builds matchup strings, populates `matchup`/`event_status` on each recommendation, tracks skipped games, validates no live-game recs exist
- **Pipeline validation** (`src/daily_pipeline.py`): Returns `EXIT_VALIDATION_FAILURE` if any recommendation belongs to a live/completed game
- **Pipeline state** (`src/daily_pipeline.py`): New fields: `skipped_games`, `n_games_analyzed`, `n_games_skipped`, `n_total_games`, `has_live_game_recs`
- **Run summary** (`src/daily_pipeline.py`): `_build_run_summary()` now includes game metrics and skipped_games list in `run_summary.json`
- **Control panel** (`src/control_panel.py`): `_load_recs()` with "latest"/"today"/"all" filter modes; `_get_latest_run_id()`, `_get_schedule_summary()`, `_get_live_game_warnings()`, `_load_latest_run_summary()` helpers; schedule summary row (Total/Upcoming/Live/Completed/Analyzed/Skipped); latest-run filter selector; Matchup/Start Time/Game Status/Run ID columns in recommendation table; safety warnings banner for live-game recs; skipped-games section
- **Database** (`database/db_manager.py`): `matchup` and `event_status` columns added via ALTER TABLE migration; `save_recommendation()` includes new columns
- **Test schemas updated**: conftest.py, test_phase6_grading.py, test_phase12_control_panel.py, test_phase13_dashboard.py, test_phase14_scoring.py, test_phase15_official_picks.py all updated with new columns
- **47 new tests** across 1 test file, covering live-game filtering, matchup building, latest-run filtering, game detail columns, pipeline state, run summary, control panel helpers, live-game warnings, source structure

Phase 11 deliverables:
- **Shadow Mode** (`src/shadow_mode.py`): `ShadowConfig` dataclass, delivery blocking, env overrides, file persistence
- **API Usage Accounting** (`src/api_usage.py`): Per-request tracking, summary queries, quota warnings, table persistence
- **Data Quality** (`src/data_quality.py`): 15 check functions (sportsbook/market drops, missing books, inverted odds, impossible prices, volume spikes, etc.), finding persistence, critical detection
- **Audit Trail** (`src/audit_trail.py`): Recommendation traceability (9 lifecycle recorders), secret redaction, full trace query
- **Live Readiness** (`src/live_readiness.py`): 18 readiness checks, live-data acknowledgement, CLI with exit codes 0-5
- **Production Canary** (`src/production_canary.py`): Minimal live test, schema validation, API response validation, dry-run analysis
- **Delivery Gate** (`src/delivery_gate.py`): Multi-factor delivery safety (6 checks), enable/disable with explicit confirmation phrase
- **Shadow Dashboard** (`src/shadow_dashboard.py`): Aggregated shadow-run summary (recommendations, delivery, DQ, API, health, readiness, promotion)
- **Promotion Criteria** (`src/promotion.py`): 7 criteria (consecutive days, success rate, DB integrity, backups, YN review, readiness, shadow duration)
- **Manual Checklist** (`src/manual_checklist.py`): 18 pre-live verification items across 5 categories, completion tracking
- **102 new tests** across 2 test files, all using mocks/fixtures — no real API calls or external service calls
- **Documentation**: SHADOW_MODE.md, LIVE_READINESS.md, FIRST_LIVE_DAY.md, PRODUCTION_CHECKLIST.md

## Next stage

- **Run morning pipeline on Render** to verify 8-market picks land on dashboard
- **Alt-line scanning** (currently preserved but not included in scanner output)
- **Website**: market visualisation dashboard
