# TODO.md — Prioritised task list

## Completed

- [x] Refactor threshold configuration to use module import (no longer importing by name)
- [x] Add regression test (`test_config_changes_do_not_leak`) proving config changes propagate
- [x] Raise strikeout scanner with all, positive-only, actionable-only modes
- [x] Add scanner tests (22 tests)
- [x] Live verification confirmed: NO_EDGE in all mode, excluded in positive mode, record counts correct
- [x] Run full suite — 166/166 passing
- [x] Deterministic fixtures replace cache-dependent tests (169/169 passing)
- [x] Pitcher strikeout Yes/No — discovery complete
- [x] Pitcher strikeout Yes/No — parser implementation (`_is_pitching_k_yn`, YN extraction, `_process_yn_market`)
- [x] Pitcher strikeout Yes/No — analysis implementation (`analyze_yn_group` — LOO median reference, price advantage metrics, no true EV)
- [x] Pitcher strikeout Yes/No — scanner integration (separate output section, `--market` flag)
- [x] Pitcher strikeout Yes/No — 20 new deterministic tests (194/194 passing)
- [x] YN semantic audit — renamed `price_difference_cents` → `decimal_odds_advantage`, added 5 unit tests, clarified threshold units (194/194 passing)
- [x] Phase 1: Market registry refactor — `MarketConfig` dataclass, parser/scanner dispatch via registry, backward-compatible constants preserved (206/206 passing)
- [x] Phase 1: 12 regression tests — registry detection, group key format, backward-compat imports, analysis unaffected
- [x] Phase 2: Pitcher outs O/U — registry dispatch verified, parser recognizes outs automatically, `_extract_player_name_from_market` handles "Outs Recorded" suffix, 49 new tests (255/255 passing)
- [x] Phase 2: Outs O/U tests — comprehensive tests covering A-K requirements (valid market, alt lines, missing side, insufficient books, duplicates, malformed line, invalid mapping, positive/negative EV, freshness, cross-market isolation, regression)
- [x] Phase 3: Added 3 new MarketConfig entries — `PITCHER_HITS_ALLOWED` (pitching_hits, O/U only), `PITCHER_WALKS_ALLOWED` (pitching_basesOnBalls, O/U + YN), `PITCHER_EARNED_RUNS` (pitching_earnedRuns, O/U + YN)
- [x] Phase 3: Updated `_extract_player_name_from_market()` suffixes for hits, walks, earned runs
- [x] Phase 3: Added synthetic fixtures (`hits_event`, `walks_event`, `earned_runs_event`)
- [x] Phase 3: Added 69 tests in `tests/test_additional_props.py` — 6 markets with full O/U + YN coverage, cross-market isolation, stale cache, registry completeness (324/324 passing)
- [x] Phase 3: Discovery — `pitching_homeRunsAllowed` confirmed NOT a market (only live game stat), cannot implement
- [x] Phase 4: Generic scanner — created `src/player_prop_scanner.py` with market/form resolution, registry-driven titles, sportsbook/player/game filtering
- [x] Phase 4: Backward-compat wrapper — `src/strikeout_scanner.py` delegates to generic scanner, all old flags preserved
- [x] Phase 4: Added `scanner_title` field to `MarketConfig` (registry-driven scanner headers)
- [x] Phase 4: Added 87 tests in `tests/test_player_prop_scanner.py` — market/form resolution, filtering, backward compat, cross-market isolation, YN output, freshness, output structure (411/411 passing)
- [x] Phase 4: Removed all hardcoded scanner wording (strikeout-specific titles, empty-state messages)
- [x] Phase 4 audit: Reject --min-ev for YN with nonzero exit code
- [x] Phase 4 audit: Improve --game filtering to match team names first, require 4+ chars for event-ID
- [x] Phase 4 audit: Print clear no-data hint when --market all --market-form yn returns empty
- [x] Phase 5.1: Run identity & auditability — scan_runs table, UUID run IDs, ingestion_log
- [x] Phase 5.2: Database integrity — scan_runs/ingestion_log schemas, foreign keys
- [x] Phase 5.3: Idempotent ingestion — log_ingestion per-event tracking
- [x] Phase 5.4: API hardening — retry with exponential backoff (3 retries, 1s/2s/4s), retry on 429/5xx
- [x] Phase 5.5: Rate limiting — MIN_API_INTERVAL=1s between live API calls
- [x] Phase 5.6: Cache integrity — max_cache_age, clear_stale_cache, get_cache_info
- [x] Phase 5.7: Freshness enforcement — --require-fresh flag exits nonzero on stale data
- [x] Phase 5.8: Structured logging — logging already present; added cache/rate-limit debug logs
- [x] Phase 5.9: Error persistence — persist_scan_error stores errors in ingestion_log
- [x] Phase 5.10: Config validation at startup — validate_config() checks threshold ordering, registry consistency
- [x] Phase 5.11: Exit codes — --min-ev YN, --require-fresh, config errors all exit nonzero
- [x] Phase 5.12: Concurrency safety — WAL mode + foreign keys already enabled
- [x] Phase 5.13: 21 new tests in tests/test_phase5_integrity.py (432/432 passing)
- [x] Phase 6.1: Recommendation persistence — `historical_recommendations` table, SHA-256 fingerprint, `save_recommendation()`, `compute_fingerprint()`, deduplication via UNIQUE index
- [x] Phase 6.2: O/U grading — `grade_ou()` with win/loss/push rules, whole-line push, half-line no-push
- [x] Phase 6.3: YN grading — `grade_yn()` returns UNRESOLVED (no automated settlement)
- [x] Phase 6.4: Units tracking — `compute_units()` for American odds profit/loss (positive odds: odds/100, negative: 100/|odds|, loss: -1, push/void/cancelled: 0)
- [x] Phase 6.5: CLV calculation — `calculate_clv()` with probability CLV (positive = favorable), line-change detection, same-line vs line-changed
- [x] Phase 6.6: Performance summaries — `performance_summary()` and `breakdown_by_field()` with bucket definitions (EV, odds, N_books, YN advantage)
- [x] Phase 6.7: Manual overrides — `apply_manual_override()` with audit trail in `manual_override_audit` table
- [x] Phase 6.8: Player stat results — `save_player_stat_result()` for final stat ingestion, idempotent upsert
- [x] Phase 6.9: Event results — `save_event_result()` for game outcomes, idempotent upsert
- [x] Phase 6.10: Database schema — 7 new tables with indexes, migration-safe `init_db()`
- [x] Phase 6.11: CLI — `src/grade_recommendations.py` for grading, settlement, and performance reporting
- [x] Phase 6.12: 77 new tests in tests/test_phase6_grading.py (509/509 passing)
- [x] Phase 7: Daily production pipeline — `src/daily_pipeline.py` with 9 stages, CLI, exit codes, dry-run, report generation (583/583 passing)
- [x] Phase 8: Complete MLB market coverage — added 14 new MarketConfig entries (10 batter + 4 pitcher/composite), expanded registry from 5 to 20 markets, parser name extraction updated for 40+ suffixes, 99 new tests (682/682 passing)
- [x] Phase 9: Intelligence Layer — closing line capture, analytics engine (10 queries), calibration analyzer, bookmaker quality scores, confidence scoring (configurable weights), 5 CSV report types, 41 new tests (723/723 passing)
- [x] Phase 10: Production automation — production config (env vars, secrets, validation), structured logging (JSON/human, job context), job orchestration CLI (8 job types), platform-neutral scheduler (cron/Windows/GitHub Actions/Cloud), health monitoring, message formatting (Discord/Slack chunking), Google Sheets export (batch, fingerprint dedup), Discord delivery (retry, rate limiting), database backup (online API, compression, retention), 129 new tests (852/852 passing)
- [x] Phase 11: Shadow production validation — shadow mode config, API usage accounting, data-quality monitoring (15 checks), recommendation traceability (9 lifecycle recorders), live readiness (18 checks, CLI, exit codes), production canary, delivery safety gate (6 checks), shadow dashboard, promotion criteria (7 criteria), manual verification checklist (18 items), 102 new tests (954/954 passing)
- [x] Phase 12: One-click local control panel — Streamlit UI with RUN button, pipeline execution, recommendation table (filter/sort/CSV), status cards, shadow mode display, health check, dashboard, backup, advanced controls; Windows launcher, first-time setup script, desktop shortcut, .env.example template; 67 new tests (1021/1021 passing)
- [x] Phase 13: Dashboard & export improvements — live-game filtering (status + start time checks), matchup builder, skipped-games tracking, pipeline validation (fail on live-game recs), run summary with game metrics; control panel: latest-run filtering, schedule summary, safety warnings, game detail columns (Matchup/Start Time/Event Status/Run ID); 47 new tests (1068/1068 passing)
- [x] Phase 14: Model Score V1 — 6 weighted components (EV magnitude, odds quality, book consensus, market quality, freshness, YN advantage), score 0-10, versioned, dashboard integration; EV display fix, confidence unit fix, analytics fix; 50 new tests (1137/1137 passing)
- [x] Phase 15: Official Pick Qualification — OfficialPickConfig with configurable thresholds, QualificationResult, classify_recommendation() for OFFICIAL_TRACKED vs RESEARCH_ONLY tier; DB migration (10 columns), pipeline integration, dashboard Official Picks section, tier filter; 15 new tests (1162/1162 passing)
- [x] Phase 16A: MLB Market Expansion and Score Diagnostics — BATTER_RUNS market (21 total), 3-tier classification (OFFICIAL/DISCOVERY/RESEARCH), score diagnostics (points_to_7, price_outlier_capped, true_ev_unavailable, one_sided_market, insufficient_books_failure), Market Quality Score (6 components), Market Intelligence tab, System Health cleanup, _load_recs resilience; 64 new tests (1232/1232 passing)
- [x] Phase 16B: Adaptive Learning and Model Calibration — grade analysis, score calibration, learning recommendations (INSUFFICIENT_DATA/OBSERVE/CANDIDATE/VALIDATED/REJECTED/APPROVED), champion/challenger holdout, config versioning, safety rules (MIN_GRADED_OVERALL=100, MIN_BETTING_DAYS=5), chronological splits (60/20/20), 3-tier DB tables (experiments, config_versions, learning_recommendations), dashboard tab (9th), 79 new tests (1311/1311 passing)
- [x] Phase 17: Cloud Deployment — Render platform (web + worker + persistent disk), background worker (persistent/one-shot/specific-job modes), production schedule (America/New_York), database persistence (WAL, busy timeout, env-var path), secrets management, dashboard automation controls (worker heartbeat, job metrics, manual triggers with confirmation), System Health (11 checks: database, disk, freshness, API key, output, worker heartbeat, persistent storage, deployment env, timezone, scheduler, backup), deployment documentation, 56 new tests (1367/1367 passing)
- [x] Phase 17B: PostgreSQL Migration — dialect-aware DB wrapper (connection.py), dual-mode db_manager, all src/*.py files migrated to get_connection(), Render Blueprint with PostgreSQL service, psycopg2-binary, SQLite→PostgreSQL migration script, 22 new tests (1389/1389 passing)

- [x] Phase 17C: Market rationalization (21→8 markets), variable Kelly staking, pipeline completion indicator, worker crash fix (get_connection import), Render PostgreSQL cleanup

- [x] O/U opportunities fix — single-side books now contribute to per-side LOO consensus (`analyze_prop_group`), scanner no longer skips single-side groups; live-verified: 25 O/U + 8 YN opportunities
- [x] Pinnacle reference investigation — confirmed Pinnacle NOT in feed (9 books: betmgm, bovada, caesars, draftkings, espnbet, fanduel, pointsbet, unibet, williamhill); kept LOO consensus as reference strategy
- [x] Phase 18A: Pinnacle-first sharp value model — `is_pinnacle_book`, odds/prob helpers, `calculate_no_vig_probs`, `calculate_ev`; Pinnacle no-vig reference when both sides present; per-book `pinnacle_fair_prob`/`pinnacle_ev`/`pinnacle_prob_edge`/`pinnacle_approved`; config flags `USE_PINNACLE_VALUE_MODEL`, `REQUIRE_PINNACLE_FOR_OFFICIAL`, `PINNACLE_FALLBACK_TO_MARKET_MEDIAN`, `MIN_PINNACLE_EV`, `MIN_PINNACLE_PROB_EDGE`; strict mode suppresses LOO official picks; scanner `Pin` column + verbose Pinnacle block + reference-source header; 23 new tests (`tests/test_pinnacle_value_model.py`); pre-existing `--help` crash fixed (bare `%` in `--min-ev` help); Pinnacle branch dormant
- [x] Phase 18B: Pinnacle required for official picks — `REQUIRE_PINNACLE_FOR_OFFICIAL=True` default; per-book `is_official` + group `pinnacle_found`/`pinnacle_reference_used`/`official_count`; same-line guard (logs `PINNACLE_LINE_FRAGMENTATION`, never merges); `PINNACLE_CHECK`/`OFFICIAL_BLOCKED_REQUIRE_PINNACLE` debug logs; fallback opportunities still displayed but never official; scanner propagates `is_official`; `_stage_freeze` passes `pinnacle_approved`/`is_official` into rec dict; Pinnacle gate in `classify_recommendation` (Gate 9, O/U only); 5 required analysis tests + 6 classification-gate tests; full suite **1332 passed, 0 failed**
- [x] Phase 18C: Pinnacle + alt-line diagnostics ONLY — per-group `diagnostics` dict + `PINNACLE_GROUP` DEBUG log (`_rejection_reason`/`_build_group_diagnostics`/`_empty_diagnostics` in `src/player_prop_analysis.py`); `LINE_FRAGMENTATION` DEBUG log, `PINNACLE_SUMMARY` INFO line, `pinnacle_diagnostics` result key, `--debug` CLI flag in `src/player_prop_scanner.py`; `pinnacle_diagnostics` in `run_summary.json` (`src/daily_pipeline.py`). NO pick-logic/threshold changes. 13 new tests; full suite **1345 passed, 0 failed**

## Next feature stage

- [ ] Run morning pipeline on Render to verify 8-market picks land on dashboard
- [ ] Alt-line scanning (currently preserved but not included in scanner output)
- [ ] Website (market visualisation dashboard)
- [ ] Multi-league support (NBA, NFL, NHL props)
- [ ] Advanced analytics (correlation analysis, portfolio optimization)
