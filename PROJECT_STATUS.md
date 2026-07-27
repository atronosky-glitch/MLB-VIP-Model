# PROJECT_STATUS.md — Current project snapshot

## Completed

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

Full suite: **1367/1367 passing, 0 skipped, 0 failed** (last full run 2026-07-27)

Breakdown:
- `tests/test_stage1.py` — 7 tests (project structure, env, DB init, games, raw responses, API structure)
- `tests/test_stage2.py` — 15 tests (odd ID parsing, event odds, spreads, totals, alt lines, bulk insert, price sanity, required columns)
- `tests/test_stage3.py` — 40 tests (conversions, vig, EV, consensus, analysis, CLV, slow-book, validation filtering, side analysis, approved statuses)
- `tests/test_participant_swap.py` — 27 tests (mapping, validation, BetMGM flagging, DB round-trip, exclusion)
- `tests/test_player_props.py` — 92 tests (parsing, grouping, thresholds, exclusion, LOO, pipeline, config override, regression, YN parsing/analysis, decimal_odds_advantage unit tests, market registry regression tests)
- `tests/test_pitcher_outs.py` — 49 tests (outs parsing, alt lines, missing side, insufficient books, duplicates, malformed line, invalid mapping, positive/negative EV, freshness, cross-market isolation, regression, registry, field completeness, scanner grouping)
- `tests/test_additional_props.py` — 69 tests (hits allowed, walks allowed, earned runs O/U + YN parsing/analysis, cross-market isolation, stale cache, registry completeness, regression)
- `tests/test_strikeout_scanner.py` — 25 tests (backward-compat wrapper, filtering, ranking, dedup, freshness, validation, output, CLI)
- `tests/test_player_prop_scanner.py` — 87 tests (market/form resolution, filtering, backward compat, cross-market, YN output, freshness, output structure, single implementation proof)
- `tests/test_phase5_integrity.py` — 21 tests (run tracking, config validation, error persistence, DB schema, --min-ev YN rejection, --require-fresh, game filtering, no-data hint)
- `tests/test_phase6_grading.py` — 77 tests (recommendation persistence, fingerprint, O/U grading, YN grading, units, CLV, buckets, settlement, manual overrides, performance summary, database schema, player stat results, event results, CLV storage, migration safety)
- `tests/test_daily_pipeline.py` — 74 tests (CLI, config, state, exit codes, stages, reports, dry run, API failure, config failure, empty slate, summary, timings, full pipeline dry-run)
- `tests/test_phase8_markets.py` — 99 tests (registry, O/U dispatch, YN dispatch, CLI lookup, type lookup, parser, name extraction, cross-market isolation, supports flags, group keys, validation, pitcher regression, edge cases)
- `tests/test_phase9_intelligence.py` — 41 tests (CLV capture, analytics queries, confidence scoring, calibration, bookmaker scores, report generation, bucket calculations, compute units)
- `tests/test_phase10_config.py` — 22 tests (production config, env vars, validation, secrets, load/save)
- `tests/test_phase10_formatting.py` — 25 tests (structured logging, message formatting, chunking, confidence labels)
- `tests/test_phase10_health.py` — 20 tests (health checks, report structure, DB/disk/freshness checks)
- `tests/test_phase10_backup.py` — 12 tests (backup, restore, compression, pruning, listing)
- `tests/test_phase10_discord.py` — 11 tests (webhook delivery, retry, rate limiting, filtering)
- `tests/test_phase10_scheduler.py` — 13 tests (cron, Windows, GitHub Actions, cloud config)
- `tests/test_phase10_jobs.py` — 13 tests (job orchestration, handlers, persistence, CLI)
- `tests/test_phase10_sheets.py` — 13 tests (Sheets export, fingerprints, early returns)
- `tests/test_phase11_shadow.py` — 55 tests (shadow mode, API usage, data quality, audit trail)
- `tests/test_phase11_readiness.py` — 47 tests (live readiness, canary, delivery gate, dashboard, promotion, checklist)
- `tests/test_phase12_control_panel.py` — 67 tests (control panel, launcher, setup, Streamlit config, pipeline states, recommendation table, CSV export, backup, advanced controls)
- `tests/test_phase13_dashboard.py` — 47 tests (live-game filtering, matchup builder, latest-run filtering, game detail columns, pipeline state fields, run summary, control panel helpers, live-game warnings, source structure)
- `tests/test_phase14_scoring.py` — 50 tests (model score computation, weighted components, score caps, versioning, historical scores, dashboard integration, EV display fix, confidence unit fix, analytics fix)
- `tests/test_phase15_official_picks.py` — 15 tests (qualification config, tier classification, OU/YN rules, edge metric tracking, pipeline integration, tier constants, immutability)
- `tests/test_phase16_comprehensive.py` — 55 tests (market quality score, 3-tier classification, score diagnostics, market intelligence, qualification, discovery, config, grading, observations, automation, DB schema, pipeline, export validation)
- `tests/test_phase16b_adaptive_learning.py` — 79 tests (grade analysis, score calibration, learning recommendations, champion/challenger, config versioning, safety rules, DB storage, dashboard integration)
- `tests/test_phase17_cloud.py` — 56 tests (environment loading, DB path, scheduler, worker heartbeat, duplicate-job prevention, timezone scheduling, persistent storage, secret redaction, backup/restore, web/worker separation, health checks, stale job recovery)

All tests are **deterministic** — none depend on live API responses or mutable cache data.
Cache-dependent fixtures were replaced with synthetic inline fixtures in `tests/fixture_data.py`.

## Current supported markets

### Pitcher markets (implemented Phase 1-3)
- MLB pitcher strikeouts Over/Under (full scanner pipeline)
- MLB pitcher strikeouts Yes/No (single-sided price comparison, full scanner pipeline) — **approved for regular use**
- MLB pitcher outs recorded Over/Under (full scanner pipeline via generic O/U engine)
- MLB pitcher hits allowed Over/Under (generic O/U engine, no YN variant)
- MLB pitcher walks allowed Over/Under (generic O/U engine) + Yes/No (single-sided price comparison)
- MLB pitcher earned runs Over/Under (generic O/U engine) + Yes/No (single-sided price comparison)
- MLB pitcher pitches thrown Over/Under (generic O/U engine, no YN variant) — **Phase 8, low API coverage**
- MLB pitching win Yes/No (single-sided price comparison, no O/U) — **Phase 8, low API coverage**

### Batter markets (implemented Phase 8)
- MLB batter hits Over/Under + Yes/No
- MLB total bases Over/Under + Yes/No
- MLB hits + runs + RBI Over/Under + Yes/No
- MLB home runs Over/Under + Yes/No
- MLB runs batted in (RBI) Over/Under + Yes/No
- MLB runs + RBI Over/Under + Yes/No
- MLB singles Over/Under + Yes/No
- MLB doubles Over/Under + Yes/No
- MLB batter walks Over/Under + Yes/No
- MLB stolen bases Over/Under + Yes/No
- MLB triples Over/Under + Yes/No
- MLB batter strikeouts Over/Under + Yes/No
- MLB batter runs Over/Under + Yes/No — **Phase 16A**
- MLB first home run Yes/No (no O/U) — **low API coverage**

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

**Completed**: Phase 17 — Cloud Deployment, Phone Access, and Production Automation. 1367/1367 passing.

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

- **Alt-line scanning** (currently preserved but not included in scanner output)
- **Cloud deployment**: serverless daily run (AWS Lambda, GitHub Actions)
- **Website**: market visualisation dashboard
