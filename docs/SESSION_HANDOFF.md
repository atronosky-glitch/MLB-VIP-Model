# SESSION_HANDOFF.md — End-of-session handoff

> Future OpenCode session: read `AI_CONTEXT.md`, `PROJECT_STATUS.md`, `docs/SESSION_HANDOFF.md`, and `TODO.md` in that order before modifying code.

## Session: 2026-08-08 — Scoped pregame runtime and overlap protection

### What was found

Each scheduled pregame job invoked the full daily pipeline and full-slate scanner. The worker serialized jobs within one process, but there was no global pregame lock for multiple workers and no elapsed completion log, allowing pending work to pile up and obscuring runtime.

### What was changed

- Added `event_id` scoping to pipeline fetch, scanner fetch, and pregame worker execution.
- Added a `pregame-pipeline` global lock across worker instances.
- Added `PREGAME JOB START`, `PREGAME JOB COMPLETE`, elapsed, target, and exit-code logs.
- Existing job state transitions remain `pending -> running -> completed/failed`.

### Verification

- Targeted worker/pipeline tests: **237 passed**.
- Full suite: **1478 passed, 0 failed**.

### Expected production effect

Each pregame job now targets one event instead of all MLB events. The job should be materially shorter and cannot overlap another pregame pipeline in the same database. Pre-start closing capture remains bounded by scheduled start time.

## Session: 2026-08-08 — Customer product access boundary

### What was done

1. Reworked `src/customer_view.py` into a premium mobile-first public/subscriber experience without changing the admin dashboard.
2. Public requests query only non-sensitive upcoming lock fields: matchup, time, status, post timestamp, and rank. Player, side, line, odds, sportsbook, EV, and exact market fields are never queried for public unsettled picks.
3. Settled Official Picks are publicly revealed from canonical settlement records for both wins and losses, with original odds/line, result, units, and CLV where available.
4. Added a server-side staging entitlement adapter using `MLB_CUSTOMER_ACCESS_TOKEN`. This is an access boundary for staging, not a fake payment system; a billing provider can replace that adapter later.
5. Added real cumulative expected-units vs actual-units performance calculations, zero-pick messaging, data-error messaging, and a 24-market settlement coverage gate remains active.

### Verification

- Customer targeted tests: **41 passed**.
- Full suite: **1478 passed, 0 failed**.

### Deployment

The customer service definition is in `render.yaml` as `mlb-vip-customer`. Render deployment and URL verification require the external Render dashboard.

## Session: 2026-08-08 — Final settlement coverage and CLV safety pass

### What was done

1. Added `AUTO_SETTLEABLE_MARKET_TYPES` and a registry-aware qualification gate. Markets without verified MLB result fields remain Research-only rather than becoming ungradable customer picks.
2. Made pregame closing capture include existing recommendations for the scanned events even when all current opportunities are deduplicated.
3. Bounded closing-price lookup to quotes at or before the scheduled event start, preventing post-start prices from being labeled as closing evidence.

### Verification

- Targeted qualification/CLV/pipeline tests: **229 passed**.
- Full suite: **1477 passed, 0 failed**.

### Deployment state

These final safety changes are being committed and pushed after the previously deployed `be17da3` revision. Render service deployment and production catch-up grading still require external Render access/verification.

## Session: 2026-08-06 — MLB results ingestion and customer view

### What was done

1. Inspected a real final MLB StatsAPI game feed and verified exact JSON paths for final status, game/team/player IDs, batting stats, pitching stats, and pitcher decisions.
2. Added `src/mlb_results.py` with schedule/feed retrieval, exact team-pair/time matching, exact player-name matching within the matched game, event-result persistence, and player-stat persistence. Missing/ambiguous facts stay unresolved.
3. Extended Y/N grading to use verified numeric facts (`>=1` for YES, `0` for NO) while preserving unresolved behavior when facts are absent.
4. Wired result ingestion into worker catch-up grading. O/U and supported Y/N recommendations now flow through settlement, units, Official Pick projection, and lifecycle evidence idempotently.
5. Added read-only customer-facing `src/customer_view.py` with Official Picks, Research separation, honest performance charting, and awaiting-sample messaging. Added separate `mlb-vip-customer` Render web service; admin dashboard remains `mlb-vip-dashboard`.

### Verification

- Results/customer targeted tests: **83 passed**.
- Full suite: **1477 passed, 0 failed**.

### Remaining risks

- MLB StatsAPI name matching is intentionally conservative; provider/team aliases or missing player stats remain unresolved rather than guessed.
- Final pregame closing capture is improved by persisted prop observations, but a dedicated start-time-aware final-close job still deserves separate production verification.
- Phase 19 rolling metric persistence and human-reviewed proposals remain advisory work, not automatic strategy mutation.

## Session: 2026-08-06 — Automatic grading catch-up and production audit

### What was done

1. Added `src/automatic_grading.py`. It grades unresolved O/U recommendations only when a matching `player_stat_results` row has a final/available verified status. Y/N remains unresolved without an explicit binary result contract.
2. Worker persistent startup, one-shot mode, and grading jobs now run catch-up grading. Grading jobs are deduplicated across pending/running/completed states.
3. Settlement retries are idempotent: units reuse the existing settlement identity, and the `official_picks` projection is synchronized with lowercase dashboard outcomes and risk/profit units.
4. Corrected the default pipeline lifecycle snapshot from `final` to `morning`, persisted player-prop odds/audit observations for later CLV capture, and fixed side-insensitive closing-price lookup.
5. Audited Official qualification. The Pinnacle fallback remains bounded: exact absence can use LOO fallback, while one-sided/mismatched/threshold-failed Pinnacle remains blocked. No threshold was loosened because live evidence showed only two matched Pinnacle props and insufficient samples for a market-aware relaxation.

### Verification

- Targeted grading/worker tests: **104 passed**.
- Full suite: **1471 passed, 0 failed**.

### Deliberate boundary

The worker still needs a verified external MLB final-stat ingestion contract to automatically fetch missing player results. The implementation refuses to infer results or guess API fields; unresolved records remain safe and excluded from learning.

## Session: 2026-08-06 — Live Pinnacle verification passed

### Result

The deployed Render run completed successfully: `PINNACLE_FEED_PROPS parsed=2`, `PINNACLE_SUMMARY exact_match=2 reference_used=2 pinnacle_missing=227 insufficient_books=1420 official_approved=0`, `Errors=0`, and `EXIT_CODE=0`.

### Interpretation

The key, `pinnapi.com` endpoint, parser, name normalization, and exact matching path are working. The two matched lines did not produce a positive target edge, so no Pinnacle-approved Official O/U pick was correct. The remaining missing groups reflect coverage, line availability, or insufficient comparison books, not a failed Pinnacle connection.

## Session: 2026-08-06 — Pinnacle player-name matching fix

### What was found

The live Pinnapi diagnostic returned two valid props, but exact matching remained zero because provider labels included market suffixes: `Walker Buehler Total Strikeouts` and `Kohl Drake Total Strikeouts`. SportsGameOdds uses the stable player names without those suffixes.

### What was changed

- Added verified suffix removal for `Strikeouts`, `HitsAllowed`, `EarnedRuns`, `PitchingOuts`, `TotalBases`, and `HomeRuns` labels.
- Added regression coverage for `Walker Buehler Total Strikeouts` -> `Walker Buehler`.

### Verification

- Pinnacle tests: **63 passed**.
- Full suite: **1469 passed, 0 failed**.
- Pushed as commit `252ad84`.

### Next live check

Deploy `252ad84`, rerun the pipeline, and inspect `PINNACLE_SUMMARY`. Expected improvement is `exact_match > 0` when the Pinnacle and SportsGameOdds slate contains the same event/player/line.

## Session: 2026-08-06 — Line-less game-market display fix

### What was found

After the AWAY/HOME grouping fix, the scanner reached game-level moneyline groups and crashed while formatting a `None` line with a numeric width specifier.

### What was changed

- Game-level line display now renders a safe `?` when no numeric line exists.

### Verification

- Targeted scanner/pipeline tests: **158 passed**.
- Full suite: **1468 passed, 0 failed**.
- Pushed as commit `b683e07`.

### Next live check

Deploy `b683e07` to Render and rerun the pipeline. It should now proceed beyond group formation to the Pinnacle feed diagnostic.

## Session: 2026-08-06 — Game-side scanner crash fix

### What was found

The deployed scan traceback identified `src/player_prop_scanner.py` grouping a game-level `AWAY` side into `ou_groups[key]["away"]`, although the generic analyzer allocates only `over` and `under` slots. This came from the uncommitted game-level market registry additions.

### What was changed

- Added registry-aware `_group_side()` mapping: game `AWAY`/`HOME` map to internal `over`/`under` slots.
- Preserved `AWAY`/`HOME` labels on generated opportunities.
- Added regression tests and scan-stage traceback logging.

### Verification

- Targeted scanner/pipeline tests: **158 passed**.
- Full suite: **1468 passed, 0 failed**.
- Pushed as commit `8f247e8`.

### Next live check

Deploy `8f247e8` to Render, rerun the pipeline, and confirm stage 6 completes. Only after that should `PINNACLE_FEED_PROPS parsed=N` be evaluated.

## Session: 2026-08-06 — Pinnacle credential exposure and endpoint correction

### What was found

The operator supplied an account email identifying the credential as a `pinnapi.com` key and directing REST requests to `pinnapi.com`. This is stronger evidence for the current account than the separately supplied `pinnodds.com` documentation.

### What was changed

- Reverted `PINNACLE_FEED_BASE_URL` to `https://pinnapi.com`.
- Added a run-level `PINNACLE_FEED_PROPS parsed=N` diagnostic after feed parsing, including an explicit zero-result warning.
- The credential pasted into chat is considered compromised and must be rotated. It was not used by this session.

### Required operator action

Rotate/recover a new Pinnapi key through the provider dashboard, replace `PINNAPI_API_KEY` on both Render services, redeploy, and then rerun one scan. Never paste the replacement key into chat.

## Session: 2026-08-06 — Pinnacle endpoint correction

### What was found

The supplied Pinnodds documentation specifies `https://pinnodds.com` as the API base URL. The client was configured for `https://pinnapi.com`, so a key from the documented Pinnodds deployment could not reliably reach the intended API.

### What was changed

- Changed `PINNACLE_FEED_BASE_URL` to `https://pinnodds.com`.
- Confirmed the existing request path `/kit/v1/prematch/fixtures`, `sport_id=6` for baseball, and `include_specials=1` match the supplied documentation.

### Required deployment action

Redeploy the worker with the key from the Pinnodds account, then run a scan. Look for `Pinnacle reference injected` or a specific HTTP/error response. Do not share the key.

### Verification

- Pinnacle/value-feed tests: **62 passed**.

## Session: 2026-08-06 — Pinnacle worker configuration fix

### What was found

The live warnings were expected when Pinnacle is absent, but the deployment definition had `PINNAPI_API_KEY` on the dashboard and not reliably on the worker that executes `player_prop_scanner`. The SportsGameOdds feed itself does not include a `pinnacle` bookmaker, so the optional Pinnapi feed is required for reference injection.

### What was changed

- Added `PINNAPI_API_KEY` to the `mlb-vip-worker` environment in `render.yaml`.
- No Pinnacle fields or API schema were guessed or changed.

### Required deployment action

Set `PINNAPI_API_KEY` on `mlb-vip-worker` in Render and redeploy the worker. After a scan, logs should show Pinnacle reference injection or a clear Pinnapi request/parse error. Until then, LOO fallback and Official blocking are expected.

### Verification

- Pinnacle/cloud tests: **76 passed**.

## Session: 2026-08-06 — Profitability safeguards and green test baseline

### What was done

1. Added configurable reliable-EV bounds: maximum absolute EV of 20 percentage points and offered decimal odds range 1.05 to 10.0. Extreme outliers remain research-visible but cannot qualify as reliable Official EV.
2. Added `official_max_per_player=1` to daily selection, alongside the existing one-pick-per-game and daily limits, reducing repeated exposure to the same player.
3. Updated the stale market-count regression test from 21 to the active registry count of 24 after confirming all registry entries remain covered.
4. Confirmed the existing chronological walk-forward validation and segmented performance reporting remain advisory-only and do not mutate thresholds automatically.

### Verification

- Focused safeguards tests: **197 passed**.
- Full suite: **1466 passed, 0 failed**.

### Remaining external-data work

- Independent player projections, confirmed lineups, starting-pitcher/news checks, weather, park, and umpire inputs still require verified source contracts. No API fields were guessed or fabricated.

## Session: 2026-08-06 — Freshness, ranking, and segmented EV reliability

### What was done

1. Preserved parser-provided sportsbook `lastUpdatedAt` timestamps through `player_prop_scanner.py` group opportunities and `daily_pipeline.py` recommendation snapshots. Missing or invalid timestamps are stale rather than silently treated as current.
2. Added `_freshness_for_observation()` and used it for each frozen recommendation, so cached quotes cannot appear fresh merely because the scan ran recently.
3. Fixed official-pick ranking to sort by actual O/U EV or Y/N price advantage; `applicable_edge_threshold` is no longer incorrectly used as the ranking edge.
4. Added `summarize_realized_ev_segments()` to keep realized performance separate by market, sportsbook, and EV bucket, with independent minimum-sample gates. It is descriptive/advisory only.
5. Added `record_pipeline_observations()` and wired morning/pregame pipeline runs to attach later prices to frozen official picks by stable identity. Observation writes are idempotent per phase; final CLV remains in `closing_prices`.

### Verification

- Targeted observation/pipeline tests: **149 passed**.
- Full suite: **1463 passed, 1 failed**. The failure remains pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Remaining work

- Full rolling Phase 19 metric persistence is not yet integrated. Existing closing capture remains the authoritative CLV path.
- Independent player-projection inputs, lineup/news/weather feeds, and portfolio correlation require verified external data contracts before implementation.

## Session: 2026-08-06 — Reliable EV validation layer

### What was done

1. Added `src/reliable_ev.py` to validate O/U EV inputs without inventing a new probability model: fair-probability bounds, offered-odds validity, EV arithmetic consistency, minimum independent-book count, freshness, market quality, one-sided status, and true-EV availability.
2. Wired the production pipeline to compute and version reliability evidence before model scoring and official qualification. Failed checks remain visible for research but block Official O/U status; Y/N price advantage is not relabeled as EV.
3. Persisted reliability status, reasons, calculated EV, version, and check state in `historical_recommendations` through the existing evidence persistence path. Added advisory realized-EV summaries with an explicit minimum-sample status.
4. Corrected an indentation error in an existing uncommitted recommendation-evidence persistence change because it prevented `database.db_manager` from importing. No unrelated working-tree changes were reverted.

### Verification

- Targeted reliability/pipeline/qualification/grading tests: **193 passed**.
- Full suite: **1456 passed, 1 failed**. The failure is pre-existing and unrelated: `tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count` expects 21 while `MARKET_REGISTRY` contains 24.

### Remaining work

- Phase 19 still needs rolling market/book/edge/confidence metrics, proper probability calibration (Brier/log loss/reliability), CLV-linked validation, and human-reviewed proposals. No automatic threshold mutation was added.

## Session: 2026-08-05 — Exact-market LOO fallback for unavailable Pinnacle markets

### What was done

1. Added a bounded official fallback: O/U recommendations use the existing LOO market-median consensus for Official eligibility only when `pinnacle_found is False` and the existing `PINNACLE_FALLBACK_TO_MARKET_MEDIAN` setting is enabled.
2. Kept one-sided, line-mismatched, and Pinnacle-threshold-failed markets blocked. Y/N markets remain unaffected because they do not have a Pinnacle counterpart.
3. Preserved official-gate rejection reasons for Discovery rows and filtered official selection to persisted `OFFICIAL_TRACKED` rows with `qualification_passed`, preventing Discovery recommendations from being frozen as Official Picks.
4. Added tests for fallback qualification, strict Pinnacle cases, and tier-safe selection. The existing read-only production gate diagnostic remains available; the four Pinnacle files were not modified, staged, or committed.

### Verification

- Targeted qualification/Pinnacle/pipeline tests: **224 passed**.
- Full suite: **1450 passed, 1 failed**. The failure is pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit fallback gate/selection safety, tests, diagnostics, and memory documentation only. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-05 — Phase 19A lifecycle transaction rollback fix

### What was done

1. Traced the production control flow: `create_recommendation_lifecycle_table()` ran and returned, but `_safe_migrate_odds()`, `_safe_migrate_player_prop()`, and later additive migration loops attempted `ALTER TABLE ADD COLUMN` on already-existing PostgreSQL columns. `DB.execute()` rolled back on those expected errors; the migration handlers swallowed the errors, erasing the lifecycle DDL before final verification.
2. Replaced expected-error migration handling with catalog-based `_existing_columns()` / `_add_columns_if_missing()` logic. Existing columns are skipped without issuing failing DDL; genuinely missing columns are added and real errors propagate.
3. Added `lifecycle_table_diagnostic()` with PostgreSQL `to_regclass`, `information_schema`, and transaction-state logging immediately after lifecycle DDL. Added `scripts/debug_lifecycle_table_creation.py` to run the helper, verify before commit, commit, and verify after commit.
4. Added tests for actual lifecycle SQL/catalog behavior, rollback and stop-on-error, helper invocation, repeated initialization, and debug-script verification. The four Pinnacle files were not modified, staged, or committed.

### Verification

- Targeted lifecycle/startup/schema tests: **68 passed**.
- Full suite: **1447 passed, 1 failed**. The failure is pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit only the lifecycle transaction/migration fix, diagnostics/scripts, tests, and memory documentation. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-05 — Phase 19A lifecycle creation control-flow fix

### What was done

1. Traced `recommendation_lifecycle_events` and confirmed no helper existed; the DDL was only embedded in the large `init_db()` `executescript()` body. This made helper-spy coverage impossible and left the production failure dependent on script control flow.
2. Extracted `create_recommendation_lifecycle_table(conn)` with table and index DDL, removed the duplicate inline DDL, and call it exactly once after the main schema script and before required-table verification/commit.
3. `init_db()` now returns a safe diagnostic containing `lifecycle_helper_ran`; `scripts/init_and_verify_schema.py` reports init start, lifecycle helper completion, commit completion, and verification phases without credentials.
4. Added tests for helper invocation, fresh SQLite catalog creation, PostgreSQL-like initialization, rollback/stop behavior, missing-table verification, repeated initialization, and script behavior. The four Pinnacle files were not modified, staged, or committed.

### Verification

- Targeted lifecycle/startup/schema tests: **50 passed**.
- Full suite: **1446 passed, 1 failed**. The failure is pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit only lifecycle initialization control flow, schema diagnostics/script, tests, and memory documentation. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-05 — Production schema initialization fail-fast hardening

### What was done

1. Fixed `database.connection.DB.executescript()` for PostgreSQL: DDL exceptions now rollback and raise immediately instead of being swallowed while subsequent statements run in an aborted transaction.
2. Added `REQUIRED_SCHEMA_TABLES`, `schema_diagnostic()`, and `verify_required_schema()` in `database.db_manager`. `init_db()` verifies all required tables before commit, names missing tables in exceptions, and logs only safe dialect/database/schema metadata.
3. Added read-only-safe operational script `scripts/init_and_verify_schema.py` to initialize the complete idempotent schema and verify it using `information_schema` through the shared abstraction on PostgreSQL or `sqlite_master` on SQLite.
4. Added tests for helper invocation, PostgreSQL-like schema generation, rollback/stop-on-DDL-failure, missing-table failures, repeated SQLite initialization, and script exit behavior. The four Pinnacle files were not modified, staged, or committed.

### Verification

- Targeted startup/schema tests: **66 passed**.
- Full suite: **1445 passed, 1 failed**. The failure is pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit schema execution/verification, the init/verify script, tests, and memory documentation only. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Phase 19A production schema startup initialization

### What was done

1. Confirmed `database.db_manager.init_db()` is the complete current schema initializer and includes `recommendation_lifecycle_events`; it does not drop existing tables or data.
2. Added an optional SQLite path to `init_db()` while preserving `DATABASE_URL` PostgreSQL selection, then wired full initialization into worker persistent/one-shot/specific-job startup, Streamlit dashboard startup, and `run_pipeline()` before database activity.
3. Schema initialization failures are logged clearly and fail fast. Existing dashboard-only `official_picks` migration remains after the full initializer for compatibility.
4. Added fresh SQLite persistence/idempotency tests, PostgreSQL-like SQL generation tests, worker startup tests, and source-path checks. The four Pinnacle files were not modified, staged, or committed.

### Verification

- Startup/lifecycle/dashboard/PostgreSQL targeted tests: **192 passed**.
- Full suite: **1440 passed, 1 failed**. The failure is pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit startup initialization, regression tests, and project-memory documentation only. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Phase 19A production verification

### What was done

1. Added read-only `scripts/verify_phase19a_production.py` using `database.db_manager.get_connection()`, so it supports PostgreSQL through `DATABASE_URL` and SQLite through the existing fallback.
2. The verifier reports lifecycle schema presence, counts by event type, recent non-secret event summaries, duplicate event keys, closing/CLV availability, line-move types, orphaned recommendation IDs, and canonical probability-CLV integrity checks. It returns nonzero only for missing schema, unreadable required integrity data, duplicates, or contradictory lifecycle state.
3. Added verifier tests for clean data, missing schema, duplicate/orphan/invalid CLV failures, JSON output, and read-only behavior. No production lifecycle or Pinnacle files were modified.

### Verification

- Verifier and Phase 19A tests: **9 passed**.
- Full suite: **1436 passed, 1 failed**. The failure is pre-existing and unrelated: the market registry test expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit only the verifier, verifier tests, and project-memory documentation. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Phase 19A review hardening

### What was done

1. Review found that lifecycle closing events did not explicitly store line-move or availability state. Added `line_move_type`, `closing_available`, and `clv_available` to the lifecycle evidence schema and capture path.
2. Confirmed probability CLV remains canonical: `bet_implied_prob - closing_implied_prob`. `closing_american - bet_american` remains a secondary diagnostic and is not used as the canonical CLV metric.
3. Added direct line-change regression coverage. Same-line snapshots are CLV-available; line-changed and missing-close snapshots are explicitly unavailable without fabricated values.
4. No thresholds, classification, delivery, betting, or automatic learning behavior changed. The four Pinnacle files were not modified, staged, or committed.

### Verification

- Phase 19A tests: **5 passed**.
- The original `cc43712` commit was not pushed because this review found the evidence-state gap. A follow-up commit contains the fix.

## Session: 2026-08-04 — Phase 19A immutable lifecycle and CLV capture

### What was done

1. Added the append-only `recommendation_lifecycle_events` table with PostgreSQL/SQLite-compatible SQL, deterministic unique event keys, lifecycle indexes, evidence fields, closing/CLV fields, results, timestamps, and provenance JSON.
2. Recommendation freeze now records `RECOMMENDATION_CREATED` and creation `LINE_SNAPSHOT` events. The existing closing path records idempotent `CLOSING_SNAPSHOT` events for pregame and final snapshots; final snapshots also populate the canonical `closing_prices` table.
3. Settlement and grading record `SETTLEMENT` and `GRADING_COMPLETED` events. Pinnacle reference values are carried from analysis opportunities into lifecycle evidence when available. Missing closing data is recorded without fabricated CLV.
4. Added tests for SQLite, PostgreSQL placeholder conversion, append-only behavior, idempotent reruns, CLV formulas, missing closes, push/void results, and provenance. No thresholds, classification, delivery, betting, or automatic learning behavior changed.
5. The four restored Pinnacle files were not modified, staged, or committed.

### Verification

- Targeted Phase 19A/lifecycle/grading/pipeline/PostgreSQL tests: **234 passed**.
- Full suite: **1432 passed, 1 failed**. The failure is pre-existing and unrelated: `tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count` expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit Phase 19A runtime/schema/tests and required memory documentation only. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Phase 19 Adaptive Learning architecture

### What was done

1. Added `docs/PHASE19_ADAPTIVE_LEARNING_ARCHITECTURE.md`, defining lifecycle evidence from recommendation creation through settlement, rolling metrics, confidence/EV/Pinnacle calibration, statistical sample gates, proposal workflow, data-quality handling, implementation sequence, and acceptance criteria.
2. Recorded the architecture decision in `docs/DECISIONS.md`: Phase 19 is advisory-only and cannot place bets or automatically modify thresholds, weights, market eligibility, sportsbook selection, or delivery settings.
3. Updated project status and TODO to mark the architecture as planned, not implemented. Existing `src/adaptive_learning.py` remains unchanged.
4. The four restored Pinnacle files were not modified, staged, or committed.

### Verification

- Documentation-only change; no runtime tests were required.
- Existing working-tree modifications remain limited to the four Pinnacle files.

### Commit scope

No commit or push was performed. Implementation requires explicit approval of the Phase 19 architecture first.

## Session: 2026-08-04 — Production health and schedule-aware freshness

### What was done

1. `src/health_check.py` now treats configured PostgreSQL as managed persistent storage and treats local SQLite backup checks as not required in PostgreSQL mode. No database URL or credentials are included in health output.
2. Missing optional local backup directories are created safely and reported as optional/no-backup status; they no longer produce a system error. Health also reports unresolved failed scheduled jobs explicitly.
3. Freshness is schedule-aware: future pending pregame/morning jobs prevent a false stale failure, while overdue pending scans remain unhealthy. If no pending scan exists, the next expected 09:00 run is considered. The active scheduler, heartbeat, failed-job count, and scan history remain independent checks.
4. `src/worker.py` now executes a real pipeline for each `pregame-check` job, so completed pregame work records `scan_runs` and advances freshness. Dashboard, worker, production job, delivery-gate, and shadow health callers pass schedule configuration.
5. The four restored Pinnacle files were not modified or staged.

### Verification

- Targeted health/dashboard/worker tests: **250 passed**.
- Full suite: **1427 passed, 1 failed**. The failure is pre-existing and unrelated: `tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count` expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit only health, worker scheduling, related health callers, regression tests, and project-memory documentation. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Dashboard connection factory signature fix

### What was done

1. Inspected both factories: `database.db_manager.get_connection(db_path=None)` is the shared application factory and auto-selects PostgreSQL from `DATABASE_URL`; `database.connection.get_connection(url=None, db_path=None)` is the lower-level dialect factory.
2. Corrected the two invalid `url=` calls in `src/control_panel.py` and `src/health_check.py`. Both now call the db-manager factory with no keyword, so PostgreSQL production behavior and SQLite local/test fallback are preserved.
3. Audited every repository `get_connection(` occurrence and found no other signature mismatch. Updated the dashboard regression assertion. The four restored Pinnacle files were not modified or staged.

### Verification

- Dashboard/health/PostgreSQL tests: **199 passed**.
- Full suite: **1421 passed, 1 failed**. The failure is pre-existing and unrelated: `tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count` expects 21 while `MARKET_REGISTRY` contains 24.

### Commit scope

Commit only the two corrected production callers, the dashboard regression test, and project-memory documentation. Do not push. Preserve the four Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Render worker database-layer audit

### What was done

1. Audited the reported worker crash. Current `src/worker.py` already contains the required `from database.db_manager import get_connection` import from the earlier worker fix and uses it for persistent startup, preserving PostgreSQL selection through `DATABASE_URL` and SQLite selection for local/test mode.
2. Replaced remaining `sqlite3.Connection` annotations with the shared `DB` wrapper type. The worker contains no raw `sqlite3.connect()` calls; SQLite-specific WAL and busy-timeout PRAGMAs remain guarded by the runtime dialect check.
3. Added a regression test asserting the shared import and absence of SQLite connection assumptions. The restored Pinnacle files were not modified or staged.

### Verification

- Worker-related tests: **81 passed** (`test_automation_fixes.py`, `test_phase17_cloud.py`, `test_phase10_jobs.py`).
- Full suite: **1421 passed, 1 failed**. The failure is pre-existing and unrelated: `tests/test_phase8_markets.py::TestRegistryPhase8::test_total_market_count` expects 21 while the current `MARKET_REGISTRY` contains 24.

### Commit scope

Commit only the worker type-audit/regression test and project-memory documentation. Do not push this commit. Preserve the four restored Pinnacle files as uncommitted work.

## Session: 2026-08-04 — Integrated PostgreSQL dashboard fix

### What was done

1. Compared current `main` with backup commit `fe9b16b` and reapplied only its intended dashboard and health changes; the stashed Pinnacle work was not touched.
2. `src/control_panel.py` now uses `DATABASE_URL` for production connections, retains SQLite compatibility, removes PostgreSQL-blocking file guards, uses completed `scan_runs` for latest-run and freshness values, reads market intelligence from `odds`, supports named PostgreSQL rows, and displays PostgreSQL as the production database.
3. `src/health_check.py` now uses the active database connection, discovers PostgreSQL tables via `information_schema`, and evaluates freshness from completed `scan_runs` while retaining SQLite behavior.
4. Updated dashboard and health regression fixtures/tests without replacing newer `main` dashboard or documentation changes.

### Verification

- Targeted dashboard, health, and PostgreSQL tests: **199 passed**.
- Full suite: **1420 passed, 0 failed**.

### Commit scope

The local commit contains only the integrated dashboard/health fixes, their tests, and project-memory documentation. `stash@{0}` remains untouched and no push is performed.

## Session: 2026-08-02 — Permanent AI onboarding context

### What was done

1. Added `AI_CONTEXT.md` as the concise verified onboarding reference for future AI sessions.
2. Updated `AGENTS.md` to require this order before code changes: `AI_CONTEXT.md`, `PROJECT_STATUS.md`, `docs/SESSION_HANDOFF.md`, `TODO.md`.
3. Recorded current architecture, production risks, documentation discrepancies, test-status caveats, and recommended priorities.
4. No model logic, runtime behavior, or deployment behavior was modified. No commit or push was performed.

### Next steps

1. Use the ordered onboarding files before future implementation work.
2. Reconcile the documented test counts and current local test failures before claiming a green suite.
3. Address the production canary/readiness API mismatch and PostgreSQL integration risks before expanding features.

## Session: 2026-08-01 — API auth fail-fast + Render-verified Pinnacle diagnostics

### What was done

1. **API auth fail-fast fix (commit `72423c7`)** — SportsGameOdds reports invalid keys as HTTP 500 with body `"Internal Server Error: Invalid API key"`, which was inside the retry set, so the client retried ~6 times before failing (45s+ wasted). The old "retrying in 45s - attempt #6" text seen in Render logs exists nowhere in the repo — it was a stale log, not a code path. Now `src/api_client.py`:
   - `_ENV_VAR = "SPORTSODDS_API_KEY"` (the correct env var — "SPORTSGAMEODDS_API_KEY" was never used; key is sent via `x-api-key` header).
   - `_is_auth_failure(status, text)`: 401/403 always True; status < 400 always False; HTTP ≥ 400 whose body contains an auth marker (`invalid api key`, `unauthorized`, `authentication failed`, etc.) → True → `logger.critical` + `raise APIKeyError("Invalid SportsGameOdds API key. Check Render environment variable SPORTSODDS_API_KEY.")` — never retried.
   - Retries preserved for 429/502/503/504/plain-500/timeouts (backoff). Import-time `logger.info` key diagnostic via `_mask_key` (prefix+suffix only). Caveat: `api_client` may be imported before `logging.basicConfig`, so that import-time INFO may not appear in Render logs — not a blocker (fail-fast still works).
2. **Logging visibility**: default CLI log level `WARNING → INFO` in both `src/daily_pipeline.py` and `src/player_prop_scanner.py` so the `PINNACLE_SUMMARY` INFO line is visible without `--debug`.
3. **`fallback_lean` counter semantics fix** (this session, uncommitted at doc-write time): `_accumulate_pinnacle_summary` counts `fallback_lean` only when fallback was used AND `rejection_reason != "insufficient_comparison_books"` — groups that reached the lean stage, not every fallback-reference group.
4. **Live verification on Render** (`cd ~/project && python -m src.daily_pipeline --live`): Pipeline SUCCESS, 0 errors, 190.5s (ingest 184.7s), 10 events, 3122 markets, 9 books, 54220 approved rows, 50 recs (25 O/U + 25 YN).
   - `PINNACLE_SUMMARY total_groups=2575 exact_match=0 reference_used=0 pinnacle_missing=779 line_mismatch=0 one_side=0 model_disabled=0 insufficient_books=1796 ev_threshold_failed=0 prob_edge_threshold_failed=0 no_positive_edge=0 fallback_lean=2121 official_approved=0`
   - Interpretation: Pinnacle entirely absent from the feed (`exact_match=0`, `reference_used=0`, `line_mismatch=0`, `one_side=0`). `insufficient_books=1796` = 1342 full-analysis fallback groups below `MIN_COMPARISON_BOOKS` + 454 groups that never reached analysis (no prices / EXCLUDED). With the counter fix, `fallback_lean` ≈ 779 (= the `pinnacle_missing` count; groups that reached the lean stage). `official_approved=0` is correct gating.
   - `OFFICIAL_BLOCKED_REQUIRE_PINNACLE ... reason=missing_pinnacle` observed → Gate 9 works. YN: 547 groups → 25 opportunities.
5. **Tests**: full suite **1372 passed, 0 failed** (was 1371; +1 `test_accumulate_summary_fallback_lean_excludes_insufficient_books`). `tests/test_api_client.py` (26 offline tests) from `72423c7`.

### Next steps

1. Commit the `fallback_lean` fix + memory docs (this session) and push → Render auto-deploys.
2. Next live run should show `fallback_lean≈779` (25 O/U recs) instead of 2121.
3. If a `pinnacle` book is ever added to the feed: `exact_match>0`, threshold counters active, official picks possible (Gate 9).
4. Pending from earlier: alt-line scanning, website, multi-league.

---

## Session: 2026-07-31 — Phase 18C: Pinnacle + alt-line diagnostics only

### What was done

Added a Pinnacle/alt-line observability layer. **No pick logic, thresholds, book filtering, or fallback behavior changed** (explicitly frozen: `MIN_PINNACLE_EV`, `MIN_PINNACLE_PROB_EDGE`, `REQUIRE_PINNACLE_FOR_OFFICIAL`, `MIN_COMPARISON_BOOKS`, `YN_MIN_COMPARISON_BOOKS`). Full suite: **1345 passed, 0 failed** (was 1332; +13 new tests). Repo now at `C:\Users\atron\Dev\MLB_Model` (moved out of OneDrive).

**`src/player_prop_analysis.py`** (diagnostics only):
- New helpers: `_rejection_reason`, `_build_group_diagnostics`, `_fmt_diag`, `_log_group_diagnostics`, `_empty_diagnostics`.
- Every `analyze_prop_group` result (main path + `_empty_result`) gains `"diagnostics"`: player, market, line, side, `total_books`, `n_comparison_books`, `pinnacle_present`, `pinnacle_books`, `pinnacle_both_sides`, `pinnacle_reference_used`, `fallback_used`, `pinnacle_over_price`/`pinnacle_under_price`, `pinnacle_fair_over`/`pinnacle_fair_under`, best non-Pinnacle book+odds per side, `best_sportsbook`/`best_side`/`best_odds`/`best_ev_pct`/`best_pinnacle_ev`/`best_pinnacle_prob_edge`, `official_approved`, `rejection_reason`.
- One **DEBUG** log per group: `PINNACLE_GROUP player= market= line= side= total_books= comparison_books= pinnacle_present= pinnacle_books= pinnacle_both_sides= pinnacle_over_odds= pinnacle_under_odds= pinnacle_fair_over= pinnacle_fair_under= best_book_over= best_over_odds= best_book_under= best_under_odds= best_ev_book= best_ev_side= best_ev_odds= ev_pct= pinnacle_ev= prob_edge= official_approved= rejection_reason=`

**`src/player_prop_scanner.py`:**
- `_new_pinnacle_summary` / `_accumulate_pinnacle_summary` / `_log_pinnacle_summary`: one **INFO** line per run — `PINNACLE_SUMMARY total_groups= exact_match= reference_used= pinnacle_missing= line_mismatch= one_side= model_disabled= insufficient_books= ev_threshold_failed= prob_edge_threshold_failed= no_positive_edge= fallback_lean= official_approved=`.
- `_log_line_fragmentation`: **DEBUG** per (player_id, market_type) — `LINE_FRAGMENTATION player= market= line= books= book_names= pinnacle_on_line= pinnacle_other_lines= over_books= under_books=`.
- Result dicts (normal + cache/`_empty_result`) carry `pinnacle_diagnostics` (summary counters).
- New `--debug` CLI flag → `logging.basicConfig(level=DEBUG)` in `main()`.

**`src/daily_pipeline.py`:** `_build_run_summary` includes `"pinnacle_diagnostics": state.scan_result.get("pinnacle_diagnostics", {})` → shows up in `run_summary.json`.

**Tests (+13, all in `tests/test_pinnacle_value_model.py`, file now 41 tests):**
- `TestPinnacleDiagnostics` (7): diagnostics present + fields correct; every rejection reason exercised; model-disabled case; empty-result path.
- `TestScannerPinnacleDiagnostics` (5): summary accumulation (incl. resilience to a missing `diagnostics` key), `PINNACLE_SUMMARY` INFO emitted via caplog, `LINE_FRAGMENTATION` DEBUG emitted, `--debug` parser flag, full `run_scan` integration asserting summary counters + `pinnacle_diagnostics` in result.

### Live usage

- `python -m src.player_prop_scanner --debug` (or `python -m src.daily_pipeline ... --debug`) → per-group `PINNACLE_GROUP`, per-line `LINE_FRAGMENTATION`, and the run-level `PINNACLE_SUMMARY` INFO line. Without `--debug`, production output is unchanged (per-group logs are DEBUG-level; only the single INFO summary line appears).
- Production feed still has no `pinnacle` book → every group is `missing_pinnacle` (fallback), so `PINNACLE_SUMMARY` will show `exact_match=0 reference_used=0 pinnacle_missing=<n> fallback_lean=<n> official_approved=0` until a `pinnacle` key appears.

### Next steps

1. Commit Phase 18C and push → Render auto-deploys. Watch worker logs for the `PINNACLE_SUMMARY` INFO line.
2. If a `pinnacle` book is ever added to the feed: `PINNACLE_SUMMARY` will show `exact_match>0`, per-group logs show `pinnacle_approved`/EV/edge, and OFFICIAL picks can appear (Gate 9 in `classify_recommendation`).
3. Pending from earlier: alt-line scanning (now diagnosable via `LINE_FRAGMENTATION`), website, multi-league.

---

## Session: 2026-07-31 — Phase 18B: Pinnacle required for official picks

### What was done

Made Pinnacle approval a hard requirement for OFFICIAL_TRACKED picks while keeping fallback/market-median opportunities visible but non-official. `REQUIRE_PINNACLE_FOR_OFFICIAL` now defaults to `True`. Full suite: **1332 passed, 0 failed** (was 1316/5 prior session).

**`src/prop_config.py`:** `REQUIRE_PINNACLE_FOR_OFFICIAL = True` (was False).

**`src/player_prop_analysis.py`:**
- New `_is_official()`: with the REQUIRE flag, only a Pinnacle-approved book is official; legacy rule (any positive EV) when flag off.
- New `_group_key_meta()` parses `(player_id, market_type)` out of group keys for debug logs.
- Per-book `is_official` added to all three book-dict builders (Pinnacle-ref OVER/UNDER + fallback).
- Group-level results: `pinnacle_found`, `pinnacle_reference_used`, `pinnacle_book`, `pinnacle_over_price`, `pinnacle_under_price`, `official_count` (also added to `_empty_result`).
- Same-line guard: entries whose `line` differs from the resolved line log `PINNACLE_LINE_FRAGMENTATION` (warn) instead of being silently merged; Pinnacle detection is restricted to books `_at_resolved_line()`.
- **Behavior change vs 18A:** strict mode no longer sets `best_ev=None`/`NO_BET`. Fallback opportunities stay displayed; they are merely `is_official=False`. The old "REQUIRE_PINNACLE_FOR_OFFICIAL=True — no official picks" suppression print is gone.
- Debug logs: `PINNACLE_CHECK player= market= line= found= approved= ev= prob_edge=` (debug, per group) and `OFFICIAL_BLOCKED_REQUIRE_PINNACLE player= market= line= reason=missing_pinnacle|pinnacle_threshold_failed` (warning).

**`src/player_prop_scanner.py`:** O/U opportunities carry `is_official` (from book entry). Pinnacle fields were already propagated in 18A.

**`src/daily_pipeline.py`:** `_stage_freeze` adds `pinnacle_approved` + `is_official` to the rec dict before `classify_recommendation`/`save_recommendation` (safe: `save_recommendation` uses an explicit column INSERT and ignores extra keys).

**`src/official_picks.py`:** new Gate 9 — for O/U recs, when `cfg.REQUIRE_PINNACLE_FOR_OFFICIAL` is True and `rec["pinnacle_approved"]` is falsy, the official tier is disqualified (reason "Pinnacle approval required for official status") and `OFFICIAL_BLOCKED_REQUIRE_PINNACLE` is logged. Falsy recs fall to DISCOVERY (if status allows) or RESEARCH. YN recs are never gated (no Pinnacle counterpart for single-sided markets). Following gates renumbered (identity fields 10, YN reference odds 11).

**Tests (11 new, 5 updated):**
- `tests/test_pinnacle_value_model.py` — 5 required cases in `TestPinnacleRequiredForOfficial` (approved→official, missing→not official, threshold-fail→not official, fallback displayed but not official, different-line Pinnacle not used as reference); strict-mode test rewritten for display-not-official behavior.
- `tests/test_phase15_official_picks.py` — 6 gate tests in `TestPinnacleOfficialGate` (approved→official, missing→DISCOVERY, threshold-fail→DISCOVERY, RESEARCH reports pinnacle reason, legacy behavior when flag disabled, YN unaffected); `_make_rec` base now includes `pinnacle_approved=True` so all pre-existing official-tier tests still pass under the new default.
- `tests/test_phase16_comprehensive.py` — `_base_rec` gains `pinnacle_approved=True`/`is_official=True`.

### Live behavior note

Production feed still has no `pinnacle` book (`betmgm, bovada, caesars, draftkings, espnbet, fanduel, pointsbet, unibet, williamhill`), so every group runs the LOO fallback → all recs are `is_official=False` → no OFFICIAL_TRACKED picks until a `pinnacle` key appears. Fallback rows store as DISCOVERY_TRACKED or RESEARCH_ONLY. Worker logs will show `OFFICIAL_BLOCKED_REQUIRE_PINNACLE ... reason=missing_pinnacle` warnings and `[PINNACLE-REF] ... LOO median fallback used`.

### Next steps

1. Commit Phase 18B and push → Render auto-deploys. Watch logs for `OFFICIAL_BLOCKED_REQUIRE_PINNACLE` (expected per group) and confirm dashboard shows no OFFICIAL_TRACKED rows.
2. If a `pinnacle` book is ever added to the feed, watch for `[PINNACLE-REF] ... Pinnacle (<book>) ...` + `PINNACLE-APPROVED` lines and OFFICIAL picks appearing.
3. Pending from earlier: alt-line scanning, website, multi-league.

---

## Session: 2026-07-30/31 — Phase 18A: Pinnacle-first sharp value model

### What was done

Implemented the Pinnacle-first value model end-to-end (previously the Pinnacle branch was dormant). Pinnacle no-vig probabilities are now the fair reference whenever a `pinnacle` book has both sides; otherwise the model falls back to the market median (LOO). All 23 new tests pass; full suite 1316 passed / 5 pre-existing unrelated failures.

**`src/player_prop_analysis.py`:**
- New helpers: `is_pinnacle_book` (matches `pinnacle`, `pinnacle sports`, `pinny`, any name containing `pinnacle`), `american_to_implied_prob`, `american_to_decimal`, `calculate_no_vig_probs`, `calculate_ev`.
- `analyze_prop_group` reworked: when a `pinnacle` book has both Over and Under, `calculate_no_vig_probs` produces the fair reference; each other book gets `pinnacle_fair_prob`, `pinnacle_ev`, `pinnacle_prob_edge`, `pinnacle_approved` (approved when EV >= `MIN_PINNACLE_EV` AND prob edge >= `MIN_PINNACLE_PROB_EDGE`). Pinnacle rows are excluded from target books. Without a valid Pinnacle reference, per-book pinnacle fields are `None` and the LOO market-median path runs unchanged.
- Strict mode: with `REQUIRE_PINNACLE_FOR_OFFICIAL=True` and no Pinnacle reference, best_ev=None and recommendation=NO_BET (no LOO-based "official" picks).
- `[PINNACLE-REF]` debug prints: reference used, best recreational side, Pinnacle-approval lines, and fallback reasons.

**`src/prop_config.py`:** new flags — `USE_PINNACLE_VALUE_MODEL=True`, `REQUIRE_PINNACLE_FOR_OFFICIAL=False`, `PINNACLE_FALLBACK_TO_MARKET_MEDIAN=True`, `MIN_PINNACLE_EV=0.04`, `MIN_PINNACLE_PROB_EDGE=0.025`.

**`src/player_prop_scanner.py`:** `Pin` column (Y/N/`-`) in O/U results, verbose Pinnacle block (approved/ref prob/EV/prob edge), results header states the reference source. Also fixed a pre-existing `--help` crash: the `--min-ev` help string embedded `{:.0%}` output (e.g. `5%`) which argparse's `% params` expansion rejected — escaped with `.replace("%", "%%")`.

**`src/control_panel.py`:** Score Calibration metrics guard against `None` (mean/median/std dev show "N/A"), fixing a crash when no graded data exists.

**`tests/test_pinnacle_value_model.py` (NEW, 23 tests):** helper unit tests; Pinnacle reference used + books excluded; Pinnacle variant detection; no-vig correctness; approval thresholds (both/EV-only/edge-only/neither); per-book None fields on fallback; fallback EV equals LOO value; strict-mode suppression vs allowed fallback; single-side Pinnacle falls back; Score Calibration display guard regression.

### Live verification

- `python -m src.player_prop_scanner --market all --market-form ou` against cache: pipeline runs, `Pin` column renders `-`, and `[PINNACLE-REF] ... Pinnacle missing or one-sided — LOO median fallback used` confirms the fallback path. Pinnacle still absent from the feed (books: betmgm, bovada, caesars, draftkings, espnbet, fanduel).

### Test results

- **23/23 new Pinnacle tests pass. Full suite: 1316 passed, 5 failed.**
- The 5 failures are pre-existing and unrelated: `TestWorkerHeartbeat` ×3 (raw sqlite3 test conns lack `.dialect`, so `_write_heartbeat` swallows an AttributeError → heartbeat never written), `test_guard_bypass_for_load_recs_latest` (postgres env), `test_schedule_pregame_checks` (timing-sensitive, documented earlier as flaky). Not touched by this session.

### Next steps

1. Optional: fix the 3 `TestWorkerHeartbeat` failures by giving `_write_heartbeat`/`_read_heartbeat` a dialect guard (`getattr(conn, "dialect", "sqlite")`) or wrapping test conns in `DB`.
2. Optional: fix `test_guard_bypass_for_load_recs_latest` and re-verify `test_schedule_pregame_checks`.
3. Deploy to Render (commit + push); the Pinnacle path stays dormant there until a `pinnacle` key appears.
4. Pending from Phase 17C: alt-line scanning, website.

---

## Session: 2026-07-30/31 — O/U opportunities fix + Pinnacle investigation

### What was done

Fixed the O/U opportunities always being 0 and investigated using Pinnacle as a sharp reference. Concluded Pinnacle is not in the API feed; LOO consensus remains the reference strategy.

**O/U single-side fix (commit `058040c`):**
- `src/player_prop_analysis.py` `analyze_prop_group`: consensus computed per-side from ALL books (`set(over_prices) | set(under_prices)`); single-side books contribute to LOO consensus; removed early-return-when-one-side-empty; `_classify_market` counts total unique books, not paired books.
- `src/player_prop_scanner.py`: removed `if not gdata["over"] or not gdata["under"]: continue` guard.
- Live-verified on Render: 25 O/U + 8 YN opportunities, 0 errors, 1797 markets scanned.

**Pinnacle reference (commit `5452ded`):**
- When a `pinnacle` book has BOTH Over and Under, its no-vig probability is used as the fair reference for all other books; Pinnacle's own rows are skipped as targets. Otherwise falls back to same-side LOO median (paired LOO + vig removal when ≥3 paired books). Debug print `[PINNACLE-REF]` (commit `f3de8be`).

**Investigation result:**
- Searched worker logs for `[PINNACLE-REF]` → never fires. Queried live Postgres from Render shell: `odds` table DISTINCT sportsbooks = `betmgm, bovada, caesars, draftkings, espnbet, fanduel, pointsbet, unibet, williamhill`. No pinnacle key in `byBookmaker`.
- User decision: **Keep LOO consensus only** — Pinnacle branch stays dormant, no code removal, no alternate sharp-book config.
- Confirmed `player_prop_odds` being empty is expected: scanner fetches props from the live API and persists only recommendations, not raw props.

**Book listing (commit `be217b3`):**
- `run_scan` now prints `Books in approved O/U+YN rows (N): ...` before group analysis.

### Files changed

1. `src/player_prop_analysis.py` — per-side all-book consensus, Pinnacle no-vig reference branch, `_classify_market` total unique books
2. `src/player_prop_scanner.py` — removed single-side skip, added `seen_books` listing print
3. `PROJECT_STATUS.md`, `TODO.md`, `docs/SESSION_HANDOFF.md` — updated

### Test results

- **1134 passed** (full suite excludes known pre-existing flaky `test_schedule_pregame_checks`). Targeted scanner tests: 107 passed.

### Next steps

1. Optional: if the API ever adds a `pinnacle` byBookmaker key, the `[PINNACLE-REF]` branch activates automatically — no code change needed.
2. Optional: `git rm --cached mlb_dump.txt` + `.gitignore` entry (file committed accidentally in `058040c`).
3. Pending from Phase 17C: alt-line scanning, website.

---

## Session: 2026-07-28 — Phase 17C: Market rationalization, variable staking, pipeline indicator

### What was done

Completed Phase 17C — rationalized MARKET_REGISTRY from 21 to 8 high-signal markets, implemented variable Kelly staking, added pipeline completion indicator, fixed worker crash on Render, and cleaned up PostgreSQL.

**Market rationalization:**
- `src/prop_config.py`: MARKET_REGISTRY cut from 21 to 8 keepers. Dropped: pitching_outs, pitching_earnedRuns, pitching_pitchesThrown, batting_hits+runs+rbi, batting_RBI, batting_runs, batting_runs+rbi, batting_singles, batting_doubles, batting_triples, batting_walks, batting_strikeouts, batting_firstHomeRun (O/U and YN variants). `PITCHER_WALKS_ALLOWED` set to O/U-only (`market_type_yn=None, supports_yn=False`). `BATTER_TOTAL_BASES` set to O/U-only.
- All dropped-market assertions removed from 5 test files: `tests/test_pitcher_outs.py` (file deleted), `test_phase8_markets.py`, `test_player_prop_scanner.py`, `test_player_props.py`, `test_additional_props.py`, `test_daily_pipeline.py`.

**Variable Kelly staking:**
- `compute_variable_stake()` = 25% fractional Kelly × score multiplier [0.25, 2.0] units; 1 unit = 1% bankroll.

**Pipeline completion indicator:**
- `src/daily_pipeline.py`: `_write_completion_flag()` writes `database/.pipeline_completed` JSON after stage 9.
- `src/control_panel.py`: Reads `.pipeline_completed` and shows green `st.success()` banner above Pipeline section.

**Worker crash fix:**
- `src/worker.py:42`: Added `from database.db_manager import get_connection` (missing import caused Render crash loop). Pushed to GitHub as `b3e7bc2`.

**Render PostgreSQL cleanup:**
- Used `psql` in Render shell to delete 64 dropped-market rows from `historical_recommendations` and 2 linked rows from `official_picks`.
- `scripts/render_cleanup.py` pushed for future use (`c7fad73`).

### Files changed

1. `src/prop_config.py` — MARKET_REGISTRY: 8 keepers, YN disabled for walks_allowed/total_bases
2. `src/worker.py` — Added `get_connection` import (line 42)
3. `src/daily_pipeline.py` — Added `_write_completion_flag()`
4. `src/control_panel.py` — Reads `.pipeline_completed` flag for green banner
5. `scripts/render_cleanup.py` — NEW: clean up dropped-market picks from PostgreSQL
6. `scripts/cleanup_dropped_markets.py` — NEW: SQLite variant
7. `tests/test_pitcher_outs.py` — DELETED (dropped market)
8. `tests/test_phase8_markets.py` — Stripped dropped-market assertions
9. `tests/test_player_prop_scanner.py` — Stripped dropped-market assertions
10. `tests/test_player_props.py` — Stripped dropped-market assertions
11. `tests/test_additional_props.py` — Stripped dropped-market assertions
12. `tests/test_daily_pipeline.py` — Stripped dropped-market assertions

### Test results

- **1297 passed, 1 pre-existing flaky failure** (`test_schedule_pregame_checks`)
- All changes related to 8-market rationalization pass

### Known issues

- 75 tests fail on Render container due to environment differences (env vars like `MLB_LOG_LEVEL=INFO` override test configs, PostgreSQL `sqlite_master` query fails for health checks). These all pass locally with SQLite.
- `test_schedule_pregame_checks` is timing-sensitive flaky test, pre-existing.

### Next steps

1. Wait for rate-limit reset on SportsGameOdds API
2. Run morning pipeline on Render: `python -m src.daily_pipeline manual`
3. Verify dashboard shows picks from only the 8 keeper markets

---

## Session: 2026-07-27 — Phase 17B: PostgreSQL Migration for Production

### What was done

Completed Phase 17B — migrated the database layer from SQLite-only to dual-mode PostgreSQL/SQLite for production deployment.

**New source modules (1):**
1. `database/connection.py` — Dialect-aware `DB` wrapper class wrapping sqlite3 or psycopg2. Auto SQL conversion (`?`→`%s`, `datetime('now')`→`NOW()`, `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`, `AUTOINCREMENT`→`SERIAL`, `sqlite_master`→`information_schema`, `GROUP_CONCAT`→`STRING_AGG`, `BEGIN IMMEDIATE`→`BEGIN`, PRAGMA removal). `DBResult` uniform cursor. `get_connection()` factory auto-detects from `DATABASE_URL` env var.

**New scripts (1):**
1. `scripts/migrate_sqlite_to_postgres.py` — One-time SQLite→PostgreSQL data migration. Dynamic table discovery, batch inserts (500 rows), `--dry-run`, `--drop-existing`, error resilience.

**New tests (1):**
1. `tests/test_phase17b_postgres.py` — 22 tests across 4 test classes: SQL conversion (9 tests), DB wrapper (6 tests), dual-mode db_manager (5 tests), migration script (2 tests).

**Updated source files (16):**
1. `database/db_manager.py` — Complete dual-mode rewrite. `get_connection(db_path=None)` auto-detects PostgreSQL vs SQLite. All `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`. All `INSERT OR REPLACE`→`ON CONFLICT DO UPDATE`. `sqlite3.IntegrityError`→`Exception`. All type hints updated to `DB`. PRAGMA migration functions handle both dict-like and tuple-like rows.
2. `src/control_panel.py` — 23 `sqlite3.connect()` → `get_connection()`, 13 `row_factory` lines removed, 2 local `import sqlite3` removed
3. `src/worker.py` — 3 `sqlite3.connect()` → `get_connection()`, `sqlite3.IntegrityError` → `Exception`
4. `src/production_jobs.py` — 2 `sqlite3.connect()` → `get_connection()`, 2 local imports removed
5. `src/health_check.py` — 3 `sqlite3.connect()` → `get_connection()`, import added
6. `src/promotion.py` — 3 `sqlite3.connect()` → `get_connection()`, `import sqlite3` replaced
7. `src/delivery_gate.py` — 1 `sqlite3.connect()` → `get_connection()`, `import sqlite3` replaced
8. `src/discord_delivery.py` — 1 `sqlite3.connect()` → `get_connection()`, `row_factory` removed
9. `src/export_sheets.py` — 1 `sqlite3.connect()` → `get_connection()`
10. `src/live_readiness.py` — 2 `sqlite3.connect()` → `get_connection()`, `import sqlite3` replaced
11. `src/production_canary.py` — 1 `sqlite3.connect()` → `get_connection()`
12. `src/shadow_dashboard.py` — 1 `sqlite3.connect()` → `get_connection()`, import added
13. `tests/test_phase11_readiness.py` — Updated mock from `sqlite3` to `get_connection`
14. `render.yaml` — Added PostgreSQL database service (`mlb-postgres`, Starter $7/mo), `DATABASE_URL` wired via `fromDatabase`, retained disk for cache/output/backups
15. `requirements.txt` — Added `psycopg2-binary>=2.9.9`
16. `database/connection.py` — Fixed `_replace_datetime_offset` lambda syntax error (line 52)

**Key architectural decisions:**
- `DATABASE_URL` env var triggers PostgreSQL mode; unset = SQLite
- `DB` wrapper auto-converts SQLite SQL to PostgreSQL on execute
- For SQLite, `DBResult` preserves raw `sqlite3.Row` objects (supports both integer and string indexing)
- `backup_database.py` left SQLite-only (uses `.backup()` API; PostgreSQL uses `pg_dump`)
- Render cost: $21/mo (web $7 + worker $7 + PostgreSQL $7)

**Test results:** 1389/1389 passing (1367 original + 22 new)

---

## Session: 2026-07-27 — Phase 17: Cloud Deployment, Phone Access, and Production Automation

### What was done

Completed Phase 17 — deployed the MLB VIP Model for cloud access with persistent automation.

**New source modules (1):**
1. `src/worker.py` (~350 lines) — Background worker with persistent mode (signal handling, heartbeat, stale-job recovery, sub-daily scheduling), one-shot mode (for cron), and specific-job mode. Handles: morning scan, pregame checks, grading, backup, adaptive learning, health checks. Job locking with idempotency keys, timezone-aware scheduling.

**Updated source files (4):**
1. `database/db_manager.py` — Respects `MLB_DB_PATH` env var (was hardcoded). Loads `.env` via `python-dotenv`.
2. `src/production_config.py` — Added 3 new fields: `backup_dir`, `environment`, `scheduler_enabled`, `shadow_mode`. Added corresponding env vars: `MLB_BACKUP_DIR`, `MLB_ENVIRONMENT`, `MLB_SCHEDULER_ENABLED`, `MLB_SHADOW_MODE`.
3. `src/health_check.py` — Added 6 new health checks: `worker_heartbeat`, `persistent_storage`, `deployment_environment`, `timezone`, `scheduler`, `backup_directory`. Updated `run_health_checks()` signature with new parameters.
4. `src/control_panel.py` — Enhanced Automation tab (tab 7) with: deployment status (environment, scheduler, shadow mode, timezone), worker heartbeat display, job metrics, database/storage status, manual triggers with confirmation, production schedule display. Updated all health check calls to pass new parameters.

**New deployment files (4):**
1. `render.yaml` — Render Blueprint: web service (Streamlit) + cron worker + 1GB persistent disk
2. `Dockerfile` — Production container for self-hosted deployment
3. `Procfile` — Worker process definition
4. `streamlit_config/config.toml` — Production Streamlit settings (headless, no CORS, no XSRF, light theme)

**Updated files (3):**
1. `.env.example` — Added `MLB_BACKUP_DIR`, `MLB_ENVIRONMENT`, `MLB_SCHEDULER_ENABLED`, `MLB_SHADOW_MODE`
2. `.gitignore` — Added `backups/`, `data/_api_cache/`, `output/*.csv`, `output/*.json`, `output/*.txt`
3. `requirements.txt` — Reordered (pytest moved to dev-only section)

**Documentation (1):**
1. `docs/DEPLOYMENT.md` — Complete deployment guide: platform selection, account setup, env vars, persistent storage, services, first deployment, health verification, mobile access, rollback, database restore, cost estimate

**Tests (56 new, 1367 total):**
- `tests/test_phase17_cloud.py` — 56 tests across 11 test classes:
  - `TestEnvironmentLoading` (8) — env var loading, config fields, scheduler/shadow mode
  - `TestProductionDatabasePath` (2) — env var DB path, default path
  - `TestSchedulerEnableDisable` (3) — scheduler config, worker respects flag
  - `TestWorkerHeartbeat` (4) — write/read/overwrite heartbeat
  - `TestDuplicateJobPrevention` (4) — lock acquire/conflict/release, idempotency
  - `TestTimezoneAwareScheduling` (5) — timezone-aware now, backup time, schedule entries
  - `TestPersistentStorageHealth` (3) — storage check, missing, database dir
  - `TestSecretRedaction` (3) — API key redaction, empty key, secret fields
  - `TestBackupRestore` (4) — backup creation, compression, listing, restore with confirm
  - `TestWebWorkerSeparation` (7) — worker module, main, control panel, config usage
  - `TestHealthCheckNewChecks` (11) — worker heartbeat, deployment env, timezone, scheduler, backup dir
  - `TestStaleJobRecovery` (2) — stale job detection, no stale jobs

### Key design decisions

- **Platform**: Render (best fit for Streamlit + worker + persistent disk at ~$14/mo)
- **Worker modes**: Persistent (always-on with signal handling), one-shot (cron), specific-job
- **Job locking**: Check-then-insert pattern with `worker-lock` metadata to prevent duplicate concurrent execution
- **Database path**: `MLB_DB_PATH` env var overrides hardcoded default (was only configurable via `ProductionConfig`, not `db_manager.py`)
- **Health checks**: 11 checks total (5 original + 6 new for deployment infrastructure)
- **Manual triggers**: Confirmation required for full-slate run and grading (two-click pattern)

### Test status

1367 passed, 0 failed

### Current thresholds

| Parameter | Value | Notes |
|-----------|-------|-------|
| official_min_model_score | 7.0 | out of 10 |
| official_daily_max_picks | 3 | per day |
| official_max_per_game | 1 | per game |
| discovery_min_model_score | 6.0 | DISCOVERY tier |
| discovery_min_books | 3 | DISCOVERY tier |

### Deployment status

- **Platform**: Render (Blueprint ready via `render.yaml`)
- **Web service**: Streamlit dashboard on public URL
- **Worker**: Background process with heartbeat, sub-daily scheduling
- **Persistent storage**: 1GB disk at `/data`
- **Monthly cost**: ~$14/mo (2x Starter services)

### Remaining manual steps

1. Create Render account and connect GitHub
2. Set `SPORTSODDS_API_KEY` in Render environment
3. Deploy via Blueprint
4. Verify health checks pass
5. Test mobile access

### Next move

1. Confirm full suite passes (1367+)
2. Deploy to Render and verify
3. Or decide next feature stage

---

## Session: 2026-07-27 — Phase 16B: Adaptive Learning and Model Calibration

### What was done

Completed Phase 16B (Adaptive Learning and Model Calibration), including dashboard integration, enforcement verification, and all test fixes. Fixed final pre-existing test failure (`test_schedule_pregame_checks`).

**New source modules (1):**
1. `src/adaptive_learning.py` (~1400 lines) — `AdaptiveLearningEngine` class with grade analysis, score calibration (bucket calibration + distribution analysis), learning recommendations (6 statuses: INSUFFICIENT_DATA/OBSERVE/CANDIDATE/VALIDATED/REJECTED/APPROVED), champion/challenger holdout testing, config versioning, safety rules, chronological splits (60/20/20 train/val/holdout), high-variance market handling, per-sportsbook exclusion logic

**Updated source files (3):**
1. `database/db_manager.py` — 3 new tables (`adaptive_experiments`, `config_versions`, `learning_recommendations`) + 10 helper functions + 7 new columns on `historical_recommendations` (calibration_bucket, grade_timestamp, is_high_variance_market, grading_date, settlement_status, profit_units, risk_units)
2. `src/control_panel.py` — 9th tab "🧠 Adaptive Learning" (tabs[8]) with 6 sections: system status gate, data readiness tier counts, score calibration bucket analysis + distribution, performance by tier, learning recommendations, champion vs challenger holdout, experiments list
3. `tests/conftest.py` — 3 new tables + 7 diagnostic columns added to test DB schema

**Test updates (2):**
1. `tests/test_phase16b_adaptive_learning.py` — 79 tests across 9 test classes (all passing)
2. Root-cause fixes for 3 test failures:
   - `_seed_graded_db` in test file: Added `player_id` to INSERT columns/values (NOT NULL constraint fix)
   - `_seed_graded_db` in test file: Added sportsbook cycling to avoid single-book dominance
   - `approve_challenger()` in `src/adaptive_learning.py`: Changed to read `champion_roi`/`champion_drawdown` keys (matching `ChampionChallengerResult.to_dict()`)
   - `test_approval_requires_roi_improvement`: Added explicit `profit_units=-1.0, risk_units=1.0` for LOSS recs

### Key design decisions

- **Gate-only system**: All learning recommendations require manual approval; no automatic production config changes
- **Chronological split**: 60% train / 20% validation / 20% holdout — no future data leakage
- **High-variance markets**: batter_home_runs, batter_stolen_bases, pitcher_strikeouts get stricter sample-size rules
- **Score buckets**: 9 buckets from below_5.0 through 7.5+ for calibration
- **Safety minimums**: MIN_GRADED_OVERALL=100, MIN_GRADED_PER_MARKET=50, MIN_GRADED_PER_BUCKET=30, MIN_BETTING_DAYS=5, MIN_SPORTSBOOK_CONTRIBUTION=0.20

### Final cleanup: test_schedule_pregame_checks

**Root cause**: Nondeterministic time-of-day failure. The test inserted a game with `start_time = now + 3 hours`. When run after 21:00 UTC, `now + 3h` crosses midnight, making `date(start_time)` (tomorrow) != `date('now')` (today) in SQLite, causing the WHERE clause to return 0 rows.

**Fix**: Mock `src.automation.datetime` with a fixed noon UTC time, making the test deterministic regardless of execution time. No production code changed. No weakening of the pregame scheduling safeguard.

**Files changed**: `tests/test_phase16_comprehensive.py` — added `from unittest.mock import patch`, rewrote `test_schedule_pregame_checks` to use `patch("src.automation.datetime")`.

**Final test count**: 1311 passed, 0 failed.

### Test status

1311 passed, 0 failed

### Current thresholds (OFFICIAL pick criteria)

| Parameter | Value | Notes |
|-----------|-------|-------|
| official_min_model_score | 7.0 | out of 10 |
| official_daily_max_picks | 3 | per day |
| official_max_per_game | 1 | per game |
| official_allowed_statuses | ("QUALIFIED",) | bet_status must match |
| discovery_min_model_score | 6.0 | DISCOVERY tier |
| discovery_min_books | 3 | DISCOVERY tier |

### Active work

- Phase 16B Part 10 (enforcement verification) confirmed complete
- Project memory files updated

### Blocked

- (none)

### Next move

1. Confirm full suite passes (1310+)
2. Decide next feature stage — candidates:
   - Alt-line scanning
   - Cloud deployment (serverless daily run)
   - Website (market visualisation dashboard)
   - Or any other priority

---

## Session: 2026-07-26 — Phase 16A: Market Expansion, Score Diagnostics, 3-Tier System

### What was done

Completed Phase 16A (MLB Market Expansion and Score Diagnostics) and fixed all test failures.

**New source modules (1):**
1. `src/market_quality.py` — `MarketQualityResult` dataclass, `compute_market_quality_score()` with 6 weighted components (book_count, two_sided, freshness, mapping_confidence, price_consistency, sportsbook_diversity), 0-10 range

**Updated source files (5):**
1. `src/prop_config.py` — Added `BATTER_RUNS` market (21 total), `get_market_by_ou_type()`/`get_market_by_yn_type()` for lookups
2. `src/official_picks.py` — 3-tier system (OFFICIAL_TRACKED / DISCOVERY_TRACKED / RESEARCH_ONLY), `TIER_DISCOVERY` constant, `discovery_min_model_score=6.0`, `discovery_min_books=3`, `RULES_VERSION="official_pick_rules_v2"`
3. `src/model_scoring.py` — `ScoreResult` expanded with 6 diagnostic fields, `compute_model_score()` computes all
4. `src/control_panel.py` — Market Intelligence tab (index 5), "Why No Official Picks Today" section, `_load_recs` resilient fallback, Research tab shows discovery picks, System Health auto-refresh
5. `src/daily_pipeline.py` — `_stage_freeze()` computes MQS + score diagnostics per recommendation
6. `database/db_manager.py` — 6 new columns for diagnostics + MQS

**Updated test files (4):**
1. `tests/test_phase16_comprehensive.py` — 4 qualification tests changed TIER_RESEARCH→TIER_DISCOVERY
2. `tests/test_phase15_official_picks.py` — 7 tests updated (DISCOVERY tier, edge metric tracking, stricter config, rules version)
3. `tests/test_phase13_dashboard.py` — Schema updated with new columns
4. `tests/test_phase12_control_panel.py` — Schema updated with new columns
5. `tests/test_phase8_markets.py` — Market count 20→21

### Bug fixes in this session
1. **Duplicate function definition** in test_phase16_comprehensive.py — `test_qualifies_yn_rec` appeared twice
2. **`_load_recs` column resilience** — Production query listed explicit columns that don't exist in test DBs; fixed with try/except fallback to SELECT *

### What was NOT changed
- No pricing formulas, EV calculations, market logic, thresholds, shadow mode, or delivery safety changed
- No live delivery enabled by default

### Current state
- 1232/1232 tests passing (1162 prior + 70 Phase 16A additions)
- All Phase 15 and Phase 16A modules complete and tested
- PROJECT_STATUS.md, TODO.md, SESSION_HANDOFF.md updated

### Architecture decisions added
- 3-tier classification: OFFICIAL (strict gates) → DISCOVERY (relaxed gates, private research only) → RESEARCH (everything else)
- Market Quality Score uses 6 weighted components; score range 0-10
- `_load_recs` gracefully handles databases missing newer columns (SELECT * fallback)
- Discovery tier has its own allowed statuses: QUALIFIED, STRONG_EDGE, POSITIVE_EDGE

---

## Session: 2026-07-24 — Phase 12: One-Click Local Control Panel

### What was done

Completed Phase 12 (One-Click Local Control Panel) — all 13 parts (A-M).

**New source modules (1):**
1. `src/control_panel.py` — Streamlit-based local UI with RUN button, pipeline execution, recommendation table, status cards, safety controls, health check, dashboard, backup, advanced controls

**New launcher/setup scripts (3):**
1. `launch_mlb_model.bat` — Windows launcher (activates venv, checks Python/Streamlit, starts Streamlit, opens browser, logs errors)
2. `setup_local_app.bat` — First-time setup (checks Python 3.10+, creates venv, installs deps, verifies Streamlit, creates dirs, copies .env.example→.env, runs smoke test)
3. `create_desktop_shortcut.ps1` — Creates desktop shortcut to launch_mlb_model.bat

**New config (1):**
1. `.env.example` — Template for 17 environment variables (1 required, 16 optional)

**Updated files (1):**
1. `requirements.txt` — Added `streamlit>=1.35.0,<2.0.0` (currently 1.60.0)

**New test files (1):**
- `tests/test_phase12_control_panel.py` — 67 tests across 13 test classes (file existence, imports, config status, health status, recommendation table, O/U EV display, Y/N advantage display, empty recommendation state, pipeline states, CSV export, backup action, Streamlit config, advanced controls, launcher/setup)

**Bug fixes during implementation:**
- Test data used hardcoded dates that broke when UTC date differed from test date — fixed by using dynamic `datetime.now(timezone.utc).strftime()`
- "wager" false positive in tests — the word appears in a safety disclaimer ("No wagers are placed"), not in bet placement code — fixed assertion to check for actual bet placement functions
- Streamlit module-level import issues — fixed by using source-code checks instead of runtime imports for module-level tests
- Windows UTF-8 encoding — `Path.read_text()` defaults to cp1252 on Windows, breaking emoji characters — fixed by specifying `encoding="utf-8"`

### What was NOT changed
- No existing Phase 1-11 source files modified (except `requirements.txt`)
- No new betting markets added
- No model logic changed
- No model threshold auto-adjustment
- No live delivery enabled by default

### Current state
- 1068/1068 tests passing (1021 prior + 47 Phase 13)
- All Phase 13 modules complete and tested
- PROJECT_STATUS.md, TODO.md, AGENTS.md, SESSION_HANDOFF.md updated

### Architecture decisions added
- Control panel uses subprocess for pipeline execution (avoids blocking Streamlit event loop)
- Shadow mode is default ON in the control panel UI
- Delivery enable requires 6 independent checks + confirmation phrase
- Pipeline rerun guard enforces minimum 15-minute gap between runs
- Control panel uses lazy imports (after `st.set_page_config`) to avoid module-level Streamlit issues

### Next steps
- Alt-line scanning
- Cloud deployment (serverless daily run)
- Website (market visualisation dashboard)

---

## Session: 2026-07-24 — Phase 11: Shadow Production Validation

### What was done

Completed Phase 11 (Shadow Production Validation) — all 14 parts.

**New source modules (10):**
1. `src/shadow_mode.py` — `ShadowConfig` dataclass, delivery blocking, env overrides, file persistence
2. `src/api_usage.py` — `ApiUsageRecord`, `ApiUsageSummary`, table init, record/summary/quota functions
3. `src/data_quality.py` — 15 check functions, `DataQualityFinding`/`DataQualityReport`, persistence, critical detection
4. `src/audit_trail.py` — `TraceStep`, `RecommendationTrace`, 9 lifecycle recorders, secret redaction
5. `src/live_readiness.py` — 18 readiness checks, live-data acknowledgement, CLI with exit codes 0-5
6. `src/production_canary.py` — `CanaryResult`, minimal live test, schema validation, dry-run analysis
7. `src/delivery_gate.py` — 6-factor delivery safety, enable/disable with confirmation phrase
8. `src/shadow_dashboard.py` — Aggregated shadow-run summary across all systems
9. `src/promotion.py` — 7 promotion criteria, shadow start date tracking, YN review tracking
10. `src/manual_checklist.py` — 18 pre-live verification items, completion tracking

**New test files (2):**
- `tests/test_phase11_shadow.py` — 55 tests (shadow mode, API usage, data quality, audit trail)
- `tests/test_phase11_readiness.py` — 47 tests (live readiness, canary, delivery gate, dashboard, promotion, checklist)

**Bug fixes:**
- `src/api_usage.py`: Missing `field` import from `dataclasses`
- `src/promotion.py`: `BACKUP_DIR` imported from non-existent export in `backup_database.py` — replaced with `config.output_dir / "backups"`

**Documentation (4 new files):**
- `docs/SHADOW_MODE.md` — Shadow mode configuration and usage
- `docs/LIVE_READINESS.md` — Live-readiness checks and CLI
- `docs/FIRST_LIVE_DAY.md` — Transition guide from shadow to live
- `docs/PRODUCTION_CHECKLIST.md` — Pre-live verification checklist
- `docs/PHASE11_AUDIT.md` — Phase 11 audit report

### What was NOT changed
- No existing Phase 1-10 source files modified (except bug fix in api_usage.py import)
- No new betting markets added
- No model logic changed
- No model threshold auto-adjustment
- No public Discord delivery enabled by default

### Current state
- 954/954 tests passing (852 original + 102 Phase 11)
- All Phase 11 modules complete and tested
- PROJECT_STATUS.md, TODO.md, DECISIONS.md updated

### Architecture decisions added
- Shadow mode is the default for production delivery
- Promotion criteria never auto-disable shadow mode
- Delivery gate requires 6 independent checks to pass
- API usage is tracked per-request with quota monitoring
- Data-quality critical findings block recommendation delivery
- Recommendation trace links API request through settlement

### Next steps
- Alt-line scanning
- Cloud deployment (serverless daily run)
- Website (market visualisation dashboard)

---

## Session: 2026-07-24 — Phase 10: Production Automation and Delivery

### What was done

1. **Production configuration** (`src/production_config.py`):
   - `ProductionConfig` dataclass with 18 configurable fields
   - Env var support (18 vars: `SPORTSODDS_API_KEY`, `MLB_DB_PATH`, etc.)
   - Config file loading (JSON) with env var override priority
   - Secrets redaction in `redacted()` method
   - Validation with detailed error messages (timezone, log level, confidence range, etc.)
   - `.env.example` generation via `create_env_example()`
   - Fixed Python `bool` is subclass of `int` bug: check `bool` before `int` in type coercion

2. **Structured logging** (`src/structured_logging.py`):
   - `JSONFormatter` — one JSON line per log record with timestamp, level, logger, message, optional job_id
   - `HumanFormatter` — compact terminal-friendly format
   - `JobContextFilter` — injects job_id into all log records from active job
   - `setup_logging()` — configures root logger with level and format selection

3. **Job orchestration CLI** (`src/production_jobs.py`):
   - 8 job types: morning-run, pregame-run, export-sheets, deliver-discord, health-check, backup, calibrate, full-daily
   - `JobRun` dataclass tracks job_id, type, status, exit_code, duration, error_message
   - DB persistence of job runs in `job_runs` table
   - CLI with `--json`, `--dry-run`, `--config`, `--debug` flags
   - Morning-run chains: pipeline → Sheets export → Discord delivery → backup
   - Exit codes: 0=success, 1=no_recs, 2=config, 3=api, 4=db, 5=validation, 6=unexpected

4. **Production scheduler** (`src/scheduler.py`):
   - Platform-neutral schedule definitions (5 default entries)
   - `generate_cron()` — crontab format for Linux/macOS
   - `generate_windows_task_scheduler()` — PowerShell commands
   - `generate_github_actions()` — workflow YAML
   - `generate_cloud_config()` — generic JSON config
   - `install_cron()` — direct crontab installation
   - Default schedule: morning 9am, pregame 5pm, nightly 11pm, weekly backup Sunday 3am, health check 9:30am

5. **Health monitoring** (`src/health_check.py`):
   - `HealthCheck` dataclass with name, status (ok/warning/error), message, details
   - `HealthReport` with overall status (healthy/degraded/unhealthy), check counts
   - 5+ checks: database (schema), disk space, data freshness, API key, output dir
   - Optional checks: Google Sheets libraries, Discord availability
   - `run_health_checks()` — orchestrates all checks and returns report

6. **Message formatting** (`src/message_formatter.py`):
   - `format_recommendation()` — single rec block with player, market, book, odds, EV/PA, confidence, status
   - `format_daily_summary()` — grouped by status (BET/LEAN/MONITOR), with stats, truncation
   - `chunk_message()` — splits at newlines within 1900 char limit, continuation markers
   - `format_for_discord()` / `format_for_slack()` — channel-specific formatting
   - Confidence labels: Very High (80+), High (60+), Medium (40+), Low (20+), Very Low

7. **Google Sheets export** (`src/export_sheets.py`):
   - `export_recommendations()` — full export with batch updates
   - Fingerprint-based idempotent upsert (avoids duplicates)
   - `HEADERS` — 16 columns including fingerprint, confidence, EV
   - Auto-creates sheet with frozen header row
   - Summary sheet with counts by status
   - Early return when no recs (before credential checks)
   - Graceful degradation when libraries unavailable

8. **Discord delivery** (`src/discord_delivery.py`):
   - `deliver_recommendations()` — loads actionable recs, formats, sends to webhooks
   - `send_webhook_message()` — direct webhook POST with optional embed
   - Retry logic: 3 attempts with exponential backoff (2^n seconds)
   - Rate limiting: 1s minimum between requests
   - 429 handling: respects `retry_after` from Discord response
   - Confidence/EV filtering before delivery
   - Uses `message_formatter.chunk_message()` for long messages

9. **Database backup** (`src/backup_database.py`):
   - `backup_database()` — SQLite online backup API (safe for live DB)
   - Optional gzip compression
   - Retention-based pruning (oldest first)
   - `restore_database()` — explicit `confirm=True` safety gate
   - `list_backups()` — returns path, size, timestamp, compressed status
   - Microsecond-precision timestamps to avoid filename collisions

10. **129 new tests** across 8 test files:
    - `test_phase10_config.py` — 22 tests (config, env, validation, secrets)
    - `test_phase10_formatting.py` — 25 tests (logging, messages, chunking)
    - `test_phase10_health.py` — 20 tests (checks, reports, DB/disk)
    - `test_phase10_backup.py` — 12 tests (backup, restore, compression)
    - `test_phase10_discord.py` — 11 tests (webhooks, retry, filtering)
    - `test_phase10_scheduler.py` — 13 tests (cron, Windows, GH Actions)
    - `test_phase10_jobs.py` — 13 tests (orchestration, handlers, CLI)
    - `test_phase10_sheets.py` — 13 tests (export, fingerprints, early returns)
    - All tests use mocks/fixtures — no real API calls or external services
    - Full suite: 852/852 passing

### Bugs found and fixed during implementation

1. Python `bool` is subclass of `int` — `isinstance(False, int)` is `True`; fixed by checking `bool` before `int` in type coercion
2. SQLite backup filename collision when two backups created in same second — fixed with microsecond-precision timestamps
3. `analyze_calibration()` expects `sqlite3.Connection` not string path — fixed calibrate handler to open connection
4. Google Sheets export checked credentials before early return for empty DB — moved no-recs check before credential validation

### Key decisions

- Google Sheets and Discord are **optional integrations** — never hard dependencies; modules gracefully degrade when libraries/webhooks unavailable
- Scheduling is **platform-neutral** — generates config for cron, Windows Task Scheduler, GitHub Actions, and cloud; never runs as always-on process
- Backup uses **SQLite online backup API** for live-database safety; restore requires explicit `confirm=True`
- Job runs are **persisted to DB** in `job_runs` table for audit trail

---

## Session: 2026-07-23 — Phase 9: Intelligence Layer

### What was done

1. **Closing line capture** (`database/db_manager.py`):
   - Added `capture_closing_prices()` — looks up latest odds from `player_prop_odds` for each recommendation, stores closing price and CLV in `closing_prices` table
   - Added `get_all_recommendations_with_settlement()` — joins recommendations with settlements, units, and closing prices for analytics
   - Pipeline freeze stage updated to capture closing prices after saving recommendations

2. **Analytics engine** (`src/analytics.py`):
   - `roi_by_market()` — ROI breakdown by market_type
   - `roi_by_sportsbook()` — ROI breakdown by sportsbook
   - `roi_by_rec_status()` — ROI breakdown by recommendation status
   - `roi_by_ev_bucket()` — ROI by configurable EV buckets (converts decimal ev_pct to percentage points)
   - `roi_by_odds_bucket()` — ROI by American odds buckets
   - `roi_by_n_books()` — ROI by comparison-book count
   - `roi_by_day()` — ROI by scan date
   - `roi_by_hour_before_pitch()` — ROI by hours before first pitch
   - `clv_by_sportsbook()` — CLV metrics by sportsbook
   - `clv_by_market()` — CLV metrics by market type
   - `hit_rate_by_market()` — alias for roi_by_market
   - `overall_summary()` — aggregate performance metrics

3. **Calibration analyzer** (`src/calibration.py`):
   - `analyze_calibration()` — analyzes ROI by EV bucket, identifies profitable/unprofitable adjacent buckets, generates threshold-adjustment recommendations
   - Never auto-changes thresholds — only recommends

4. **Bookmaker quality scores** (`src/bookmaker_scores.py`):
   - `bookmaker_quality_scores()` — calculates quality_score per sportsbook from CLV and ROI (0-100 scale)
   - `bookmaker_disagreement()` — measures odds divergence from fair odds

5. **Confidence scoring** (`src/confidence.py`):
   - `compute_confidence()` — produces 0-100 confidence score from 5 measurable components
   - Components: n_books, market_quality, ev_magnitude, freshness, mapping_confidence
   - Each component normalized to 0-1, weighted by configurable `ConfidenceWeights`
   - Grades: A (80+), B (60+), C (40+), D (20+), F (<20)
   - Weights configurable via `CONFIDENCE_WEIGHTS` in `prop_config.py`

6. **Report generation** (`src/reports.py`):
   - `generate_performance_report()` — overall summary CSV
   - `generate_sportsbook_report()` — bookmaker quality rankings CSV
   - `generate_market_report()` — ROI and CLV by market CSV
   - `generate_recommendation_report()` — all recommendations with confidence scores CSV
   - `generate_confidence_report()` — confidence score distribution CSV
   - `generate_all_reports()` — batch generation of all 5 reports

7. **Configuration** (`src/prop_config.py`):
   - Added `CONFIDENCE_WEIGHTS` dict (n_books=2.0, market_quality=1.5, ev_magnitude=2.5, freshness=1.0, mapping_confidence=1.0)

8. **Tests**: 41 new tests in `tests/test_phase9_intelligence.py`:
   - TestCLVCapture: 6 tests (closing price stored, CLV favorable, line changed, no close, capture from odds, skip existing)
   - TestAnalytics: 9 tests (ROI by market/sportsbook/EV bucket/day/rec status, CLV by sportsbook/market, overall summary, hit rate)
   - TestConfidenceScoring: 6 tests (high/low quality, YN advantage, grade boundaries, components normalized, custom weights)
   - TestCalibration: 2 tests (returns buckets, empty data)
   - TestBookmakerScores: 2 tests (quality scores, empty)
   - TestReports: 7 tests (all 5 reports, batch generation, empty data)
   - TestBuckets: 3 tests (EV, odds, N_books bucket assignment)
   - TestDBHelpers: 1 test (get_all_recommendations_with_settlement)
   - TestComputeUnits: 5 tests (win positive/negative odds, loss, push, unresolved)

9. **Full suite: 723/723 passing** (682 original + 41 new)

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 25
- player_prop_scanner: 87
- phase5_integrity: 21
- phase6_grading: 77
- daily_pipeline: 74
- phase8_markets: 99
- phase9_intelligence: 41
- Total: 723

### Next action

- **Alt-line scanning** (currently preserved but not included in scanner output)
- **Historical snapshots**: automated data pulls on schedule
- **Google Sheets dashboard**: read-only display layer consuming from SQLite
- **Discord alerts**: positive-EV notifications
- **Cloud deployment**: serverless daily run

### Important

- Analytics: `from src.analytics import roi_by_market, overall_summary, ...`
- Confidence: `from src.confidence import compute_confidence, ConfidenceWeights`
- Reports: `from src.reports import generate_all_reports`
- Calibration: `from src.calibration import analyze_calibration`
- Bookmaker scores: `from src.bookmaker_scores import bookmaker_quality_scores`
- Closing prices captured automatically during pipeline freeze stage
- EV buckets use percentage points (2 = 2%), but ev_pct is stored as decimal (0.02 = 2%)
- Confidence weights configurable via `CONFIDENCE_WEIGHTS` in `prop_config.py`

### Key design decisions

- **Closing prices at freeze time**: Automated capture ensures every recommendation has a closing reference. Idempotent — skips already-captured records.
- **EV bucket conversion**: `roi_by_ev_bucket()` multiplies `ev_pct` by 100 before bucket comparison to match percentage-point bucket definitions.
- **Confidence is additive weighted**: No interaction terms, no ML. Pure weighted sum of normalized components. Transparent and explainable.
- **Calibration is advisory only**: Never auto-changes thresholds. Generates human-readable recommendations with evidence.

---

## Session: 2026-07-23 — Phase 8: Complete MLB Market Coverage

### What was done

1. **API discovery** — Full analysis of all 9,286 odd_ids across 10 live events:
   - 14 batter markets discovered (batting_hits, total_bases, hits+runs+rbi, home_runs, RBI, runs+rbi, singles, doubles, batter_walks, stolen_bases, triples, batting_strikeouts, batting_firstHomeRun, pitching_win reclassified)
   - `extra_base_hits` does NOT exist as a market
   - `pitching_pitchesThrown` confirmed O/U only, very low coverage (8 odd_ids, 0 books)
   - `pitching_win` confirmed YN only, low coverage (8 odd_ids, max 1 book)
   - `batting_firstHomeRun` confirmed YN only (173 odd_ids, max 2 books)

2. **14 new MarketConfig entries** in `src/prop_config.py`:
   - **Tier 1** (batter O/U + YN, highest coverage): `batter_hits`, `batting_totalBases`, `batting_hits+runs+rbi`, `batting_homeRuns`, `batting_RBI`, `batting_runs+rbi`
   - **Tier 2** (batter O/U + YN, moderate coverage): `batting_singles`, `batting_doubles`, `batting_basesOnBalls`, `batting_stolenBases`, `batting_triples`
   - **Tier 3** (composite/batter, lower coverage): `batting_strikeouts`, `batting_firstHomeRun`, `pitching_pitchesThrown`, `pitching_win`
   - Registry expanded from 5 to 20 entries total

3. **Parser name extraction** — `_extract_player_name_from_market()` updated with 40+ new suffix patterns covering all batter market types

4. **Pipeline CLI** — `daily_pipeline.py` market choices now derived from `MARKET_REGISTRY` dynamically (not hardcoded)

5. **Synthetic fixture** — `batter_event` in `tests/fixture_data.py` with Aaron Judge across 10+ market types (hits O/U+YN, home runs, total bases, H+R+RBI, RBI, singles, doubles, walks, first HR YN)

6. **Regression tests** — 99 new tests in `tests/test_phase8_markets.py`:
   - TestRegistryPhase8: 9 tests (all entries, stat prefixes, display names, O/U+YN support, group keys, CLI names)
   - TestOUNewMarkets: 15 tests (parser dispatch for all new O/U markets)
   - TestYNNewMarkets: 6 tests (parser dispatch for all new YN markets)
   - TestCLILookupPhase8: 15 tests (get_market_by_cli_name for all new markets)
   - TestTypeLookupPhase8: 6 tests (get_market_by_ou_type / get_market_by_yn_type)
   - TestParserPhase8: 11 tests (full parsing of all market types via batter_event fixture)
   - TestNameExtractionPhase8: 13 tests (player name extraction for all new suffix patterns)
   - TestCrossMarketIsolation: 3 tests (batter+pitcher markets independent, different market types)
   - TestSupportsFlags: 6 tests (supports_ou/supports_yn correct for all new markets)
   - TestGroupKeysPhase8: 3 tests (batter market group keys unique and correct)
   - TestValidationPhase8: 5 tests (status, price, decimal_odds, player_id, event_id fields)
   - TestPitcherRegression: 4 tests (existing pitcher markets unaffected)
   - TestEdgeCases: 2 tests (empty byBookmaker YN, unknown odd_id)

7. **Full suite: 682/682 passing** (583 original + 99 new)

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 25
- player_prop_scanner: 87
- phase5_integrity: 21
- phase6_grading: 77
- daily_pipeline: 74
- phase8_markets: 99
- Total: 682

### Next action

- **Alt-line scanning** (currently preserved but not included in scanner output)
- **Historical snapshots**: automated data pulls on schedule
- **Google Sheets dashboard**: read-only display layer consuming from SQLite
- **Discord alerts**: positive-EV notifications
- **Cloud deployment**: serverless daily run

### Important

- Generic scanner: `python -m src.player_prop_scanner --market <name> [--market-form ou|yn] [--require-fresh]`
- Old command: `python -m src.strikeout_scanner [--all] [--market ou|yn|all]`
- Pipeline: `python -m src.daily_pipeline [--live|--cache|--auto] [--dry-run] [--require-fresh]`
- Valid markets (20): `strikeouts`, `outs`, `hits_allowed`, `walks_allowed`, `earned_runs`, `pitches_thrown`, `pitching_win`, `batter_hits`, `total_bases`, `hits_runs_rbi`, `home_runs`, `rbi`, `runs_rbi`, `singles`, `doubles`, `batter_walks`, `stolen_bases`, `triples`, `batter_strikeouts`, `first_home_run`, `all`
- Valid forms: `ou`, `yn`, `all`
- `--min-ev` rejected for `--market-form yn` with nonzero exit code
- `--require-fresh` exits nonzero if data exceeds freshness threshold
- `--market all --market-form yn` silently filters to YN-supporting markets only
- Grading CLI: `python -m src.grade_recommendations [--grade-all] [--summary] [--json]`
- YN grading: UNRESOLVED in automated mode
- CLV positive = favorable
- Exit codes: 0=success, 1=no_recs, 2=config, 3=api, 4=db, 5=validation, 6=unexpected

### Key design decisions

- **14 new markets via zero production-code changes** — all work through the existing MarketConfig registry (Phase 1 architecture proven at scale)
- **`pitching_win` reclassified** as YN-only (from original pitcher classification); `batting_firstHomeRun` is also YN-only
- **`pitches_thrown` is O/U-only** — YN variant does not exist in the API
- **Low-coverage markets registered** — `pitching_win`, `pitches_thrown`, `first_home_run` are registered but may produce few/no recommendations due to low book coverage
- **Pipeline CLI derived from registry** — `--market` choices in `daily_pipeline.py` now read from `MARKET_REGISTRY` dynamically

---

## Session: 2026-07-23 — Phase 7: Daily Production Pipeline

### What was done

1. **Created `src/daily_pipeline.py`** — 9-stage production pipeline:
   - Stage 1: Validate configuration (API key, DB writability, registry integrity)
   - Stage 2: Create pipeline run (UUID run ID, persisted to scan_runs)
   - Stage 3: Fetch events (API or cache, counts events and sportsbooks)
   - Stage 4: Ingest odds (parse, save games/odds/audit, per-event logging)
   - Stage 5: Validate data (check approved rows, freshness enforcement)
   - Stage 6: Scan markets (generic scanner with mode/market/form filters)
   - Stage 7: Freeze recommendations (persist to historical_recommendations, dedup)
   - Stage 8: Produce reports (CSV, JSON, run_summary, text report)
   - Stage 9: Print terminal summary (status, metrics, timings)

2. **PipelineConfig** — dataclass with all configurable parameters (live, cache, auto, output_dir, market, market_form, actionable_only, positive_only, require_fresh, dry_run, as_json, as_csv, debug)

3. **PipelineState** — mutable state dataclass tracking run ID, timings, counters, errors, warnings, scan results

4. **Exit codes** — 6 standardized codes:
   - 0: success (recommendations saved)
   - 1: success_no_recs (pipeline ran but no opportunities)
   - 2: config_failure (invalid config, missing API key)
   - 3: api_failure (API fetch failed)
   - 4: db_failure (database write failed)
   - 5: validation_failure (stale data with --require-fresh)
   - 6: unexpected_failure (unhandled exception)

5. **CLI** with mutually exclusive flags:
   - Data source: `--live`, `--cache`, `--auto`
   - Mode: `--actionable-only`, `--positive-only`, `--all-markets` (default: actionable)
   - Market: `--market <name>`, `--market-form <form>`
   - Safety: `--require-fresh`, `--dry-run`, `--debug`
   - Output: `--output-dir`, `--json`, `--csv`

6. **Dry-run mode** — runs all stages except database writes and file output

7. **Report outputs**:
   - `recommendations.csv` — all opportunities as CSV
   - `recommendations.json` — all opportunities as JSON array
   - `run_summary.json` — structured run summary with metrics
   - `pipeline_report.txt` — human-readable text report

8. **Bug fixes**:
   - Changed imports from local to module-level for testability
   - Argparse `--actionable-only` defaults to `False` (mutual exclusion group), `main()` converts to `True` when no flag given
   - `_parse_status("")` returns `"scheduled"` (was undefined)
   - Added missing `DB_PATH` to module imports

9. **Tests**: 74 new tests in `tests/test_daily_pipeline.py`:
   - TestCLI: 18 tests (all flags, defaults, choices, mutual exclusion)
   - TestPipelineConfig: 2 tests (defaults, custom values)
   - TestPipelineState: 2 tests (defaults, accumulation)
   - TestExitCodes: 7 tests (all codes, uniqueness)
   - TestStageValidateConfig: 3 tests (valid, missing API key, dry-run skip)
   - TestStageCreateRun: 2 tests (dry run, live mode)
   - TestStageValidate: 4 tests (valid, no-rows warning, stale reject, stale without require-fresh)
   - TestReportBuilders: 5 tests (summary, report, warnings, errors, timings)
   - TestFileWriters: 8 tests (CSV, JSON, text, dry-run variants)
   - TestParseStatus: 4 tests (string, dict, empty, missing)
   - TestFullPipelineDryRun: 3 tests (no events, with events, no files created)
   - TestConfigFailure: 1 test (missing API key)
   - TestAPIFailure: 1 test (API exception)
   - TestEmptySlate: 1 test (no opportunities)
   - TestReportGeneration: 3 tests (CSV dry-run, live file creation)
   - TestPipelineSummary: 3 tests (prints, warnings, errors)
   - TestStageTimings: 4 tests (all stages record timing)
   - TestMainIntegration: 2 tests (returns int, passes config)
   - TestUnexpectedFailure: 1 test (unhandled exception)

10. **Full suite: 583/583 passing** (509 original + 74 new)

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 25
- player_prop_scanner: 87
- phase5_integrity: 21
- phase6_grading: 77
- daily_pipeline: 74
- Total: 583

### Next action

- **Remaining pitcher props**: pitches thrown, pitching_win (needs API discovery, low event count)
- **Hitter props**: batting hits, home runs, RBIs (needs API discovery)
- **Live verification**: Run `python -m src.daily_pipeline --dry-run` with live data
- **Post-Phase 7**: Google Sheets dashboard, Discord alerts, cloud deployment

### Important

- Pipeline command: `python -m src.daily_pipeline [--live|--cache|--auto] [--dry-run] [--require-fresh]`
- Pipeline defaults to actionable-only mode when no mode flag is given
- Dry-run mode skips all database writes and file output
- Exit codes: 0=success, 1=no_recs, 2=config, 3=api, 4=db, 5=validation, 6=unexpected
- Reports written to `output/` directory (configurable via `--output-dir`)
- Generic scanner command: `python -m src.player_prop_scanner --market <name> [--market-form ou|yn] [--require-fresh]`
- Old command still works: `python -m src.strikeout_scanner [--all] [--market ou|yn|all]`
- Valid markets: `strikeouts`, `outs`, `hits_allowed`, `walks_allowed`, `earned_runs`, `all`
- Valid forms: `ou`, `yn`, `all`
- `--min-ev` applies only to O/U markets (rejected for yn with nonzero exit)
- `--require-fresh` exits nonzero if data exceeds freshness threshold
- Grading CLI: `python -m src.grade_recommendations [--grade-all] [--summary] [--json]`
- YN grading is UNSUPPORTED in automated mode — always UNRESOLVED unless explicit external result supplied
- CLV positive = favorable (bet odds were better than closing odds)
- Recommendation fingerprint: SHA-256 of first 32 hex chars from key fields

### Key design decisions

- **Module-level imports** for testability: `SportsGameOddsClient`, `run_scan`, `parse_odds` imported at module level, not inside functions
- **Actionable-only default**: Production pipeline filters to actionable by default; users must explicitly opt into broader modes
- **Standardized exit codes**: 6 codes allow CI/CD and scheduling systems to distinguish failure modes
- **Dry-run without side effects**: All stages execute but no DB writes, no file output
- **PipelineConfig/PipelineState separation**: Config is immutable input; state is mutable accumulator

---

## Session: 2026-07-23 — Phase 6: Historical Recommendations, Grading, Settlement, CLV, Performance

### What was done

1. **Recommendation persistence** (`database/db_manager.py`):
   - Added `historical_recommendations` table with 33 columns + `created_at` DEFAULT
   - `save_recommendation()` — `INSERT OR IGNORE` with SHA-256 fingerprint deduplication, returns `None` for exact duplicates
   - `compute_fingerprint()` — SHA-256 of `event_id|player_id|market_type|market_form|period|line|side|sportsbook|offered_american_odds|rec_status|observation_timestamp` (first 32 hex chars)
   - `FINGERPRINT_FIELDS` constant for field order
   - `generate_recommendation_id()` — UUID-based

2. **O/U grading** (`src/grading.py`):
   - `grade_ou()` — deterministic settlement: OVER wins when final > line, UNDER wins when final < line, equality on whole-number lines = PUSH, half-lines cannot push

3. **YN grading** (`src/grading.py`):
   - `grade_yn()` — always returns UNRESOLVED (no automated settlement without explicit external result)

4. **Units tracking** (`database/db_manager.py`):
   - `compute_units()` — positive odds win profit = odds/100; negative odds win profit = 100/abs(odds); loss = -1; push/void/cancelled = 0; unresolved = excluded (risk=0)

5. **CLV calculation** (`src/grading.py`):
   - `calculate_clv()` — probability CLV = `bet_implied_prob - closing_implied_prob` (positive = favorable)
   - Line-change detection: same_line, line_changed, no_close
   - CLV unavailable when line changes (different lines not directly comparable)

6. **Performance summaries** (`src/grading.py`):
   - `performance_summary()` — overall ROI, win rate, units risked/won, average odds/EV/CLV
   - `breakdown_by_field()` — bucketed breakdowns by EV, odds, N_books, YN advantage
   - Bucket definitions: `EV_BUCKETS`, `ODDS_BUCKETS`, `N_BOOKS_BUCKETS`, `YN_ADV_BUCKETS`

7. **Manual overrides** (`database/db_manager.py`):
   - `apply_manual_override()` — updates settlement status with audit trail in `manual_override_audit` table
   - Rejects missing reason, preserves audit record

8. **Player stat results** (`database/db_manager.py`):
   - `save_player_stat_result()` — idempotent upsert for final stat ingestion
   - `get_player_stat_result()` — retrieve by event/player/market

9. **Event results** (`database/db_manager.py`):
   - `save_event_result()` — idempotent upsert for game outcomes

10. **Database schema** (`database/db_manager.py`):
    - 7 new tables: `historical_recommendations`, `event_results`, `player_stat_results`, `market_settlements`, `bet_units`, `closing_prices`, `manual_override_audit`
    - Indexes: `idx_hr_fingerprint` (UNIQUE), `idx_ms_rec` (UNIQUE), `idx_hr_event`, `idx_hr_player`
    - Migration-safe `init_db()` — `CREATE TABLE IF NOT EXISTS`

11. **CLI** (`src/grade_recommendations.py`):
    - `--grade-all`, `--grade-event`, `--grade-recommendation` for grading
    - `--show-unsettled`, `--show-settled`, `--summary` for display
    - `--ingest-result`, `--override` for manual input
    - `--dry-run`, `--json`, `--csv` for output control

12. **Bug fixes in this session**:
    - Fixed `save_recommendation()` INSERT statement: removed extra `?` placeholder (34 values → 33 to match columns)
    - Fixed `save_recommendation()` dedup: returns `None` when `INSERT OR IGNORE` skips duplicate (was returning existing ID)
    - Fixed CLV sign convention: `bet_prob - close_prob` (positive = favorable) instead of `close_prob - bet_prob`
    - Fixed index tests: `row["name"]` instead of `row[1]` for `sqlite3.Row` row_factory
    - Added missing indexes to test fixture schema (`idx_hr_event`, `idx_hr_player`)

13. **Tests**: 77 new tests in `tests/test_phase6_grading.py`:
    - TestRecommendationPersistence: 7 tests (snapshot, dedup, price/line/status changes, YN fields, old records unchanged)
    - TestFingerprint: 6 tests (deterministic, price/line/side/time changes, 32-char hex)
    - TestOUGrading: 11 tests (over/under win/loss, whole-line push, half-line no-push, void, unresolved, malformed, exact stat)
    - TestYNGrading: 2 tests (always unresolved, no automation)
    - TestUnits: 7 tests (positive/negative odds win, loss, push, void, cancelled, unresolved excluded)
    - TestCLV: 6 tests (favorable/unfavorable same-line, unchanged, changed line, missing close, YN labeled correctly)
    - TestBuckets: 4 tests (EV, odds, N_books, YN advantage boundaries)
    - TestSettlement: 4 tests (settle win, idempotent regrading, settle with stat, units saved)
    - TestManualOverrides: 4 tests (valid override, missing reason rejected, audit preserved, automated not overwritten)
    - TestPerformanceSummary: 9 tests (overall ROI, win rate denominator, pushes excluded, unresolved excluded, market/sportsbook/EV/odds/N_books bucket breakdowns)
    - TestDatabaseSchema: 9 tests (all 7 tables exist, fingerprint/settlement indexes)
    - TestPlayerStatResults: 3 tests (save/retrieve, idempotent upsert, multiple markets)
    - TestEventResults: 2 tests (save/update, upsert)
    - TestCLVStorage: 1 test (save closing price)
    - TestMigrationSafety: 2 tests (repeated init_db safe, indexes present)

14. **Full suite: 509/509 passing** (432 original + 77 new)

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 25
- player_prop_scanner: 87
- phase5_integrity: 21
- phase6_grading: 77
- Total: 509

### Next action

- **Phase 7**: Remaining pitcher props (pitches thrown, pitching_win) or hitter props
- **Live verification**: Run `python -m src.player_prop_scanner --market strikeouts --all` with live data
- **Post-Phase 7**: Historical snapshots, CLV tracking, pre-game scheduling, results grading CLI integration

### Important

- Generic scanner command: `python -m src.player_prop_scanner --market <name> [--market-form ou|yn] [--require-fresh]`
- Old command still works: `python -m src.strikeout_scanner [--all] [--market ou|yn|all]`
- Valid markets: `strikeouts`, `outs`, `hits_allowed`, `walks_allowed`, `earned_runs`, `all`
- Valid forms: `ou`, `yn`, `all`
- `--min-ev` applies only to O/U markets (rejected for yn with nonzero exit)
- `--require-fresh` exits nonzero if data exceeds freshness threshold
- Grading CLI: `python -m src.grade_recommendations [--grade-all] [--summary] [--json]`
- YN grading is UNSUPPORTED in automated mode — always UNRESOLVED unless explicit external result supplied
- CLV positive = favorable (bet odds were better than closing odds)
- Recommendation fingerprint: SHA-256 of first 32 hex chars from key fields

### Key design decisions

- **CLV sign convention**: `bet_implied_prob - closing_implied_prob` (positive = favorable). When closing odds are better (lower implied prob), the bettor got a better price, so CLV is positive.
- **YN grading**: Always UNRESOLVED because the settlement condition is implicit in the market definition. Without an explicit external settlement feed, we cannot determine YES/NO outcome.
- **Deduplication**: `INSERT OR IGNORE` with UNIQUE fingerprint index. Returns `None` for exact duplicates, existing ID for re-queries.
- **Units**: Risk always = 1.0 unit. Profit calculated from American odds: positive odds = odds/100, negative odds = 100/|odds|.
- **Performance summary**: Win rate denominator = settled (non-UNRESOLVED). Pushes/voids/cancelled excluded from units_risked and ROI.

### What was done

1. **Phase 4 audit fixes** (3 items):
   - **Fix 4a**: `--min-ev` rejected with nonzero exit code when `--market-form yn` is explicitly requested. Applied to both `player_prop_scanner.py` and `strikeout_scanner.py` CLIs.
   - **Fix 4b**: `--game` filtering improved — matches away/home team names first, then matchup string, then event-ID (only if >= 4 chars to avoid false positives on short substrings).
   - **Fix 4c**: `display_results()` prints contextual hints when no data is found: "No approved odds rows" vs "No market groups matched the filter" with market/form-specific guidance.

2. **Phase 5.1 — Run identity & auditability**:
   - Added `scan_runs` table (run_id UUID PK, started_at, finished_at, run_type, mode, market_filter, form_filter, n_events, n_markets, n_opportunities, n_yn_opps, data_source, research_only, error_message, metadata_json)
   - Added `ingestion_log` table (run_id FK, event_id, odds_rows, audit_rows, error_message)
   - Added `create_run()`, `finish_run()`, `log_ingestion()` helper functions in `db_manager.py`
   - Wired run tracking into `main.py` (ingestion) and `player_prop_scanner.py` (scan)
   - Scanner result dict includes `run_id` for traceability

3. **Phase 5.4 — API hardening**:
   - Added `_request_with_retry()` method: up to 3 retries, exponential backoff (1s/2s/4s), retries on ConnectionError, Timeout, HTTP 429/500/502/503/504
   - Updated `_get()` to use `_request_with_retry()` instead of direct `session.get()`

4. **Phase 5.5 — Rate limiting**:
   - Added `MIN_API_INTERVAL = 1.0` class variable and `_last_api_call` timestamp
   - `_get()` sleeps if < 1s since last live API call (cached responses bypass this)

5. **Phase 5.6 — Cache integrity**:
   - Added `max_cache_age` constructor parameter — cache files older than this are re-fetched
   - Added `clear_stale_cache(max_age_seconds)` — deletes old cache files, returns count
   - Added `get_cache_info()` — returns file count and total bytes

6. **Phase 5.7 — Freshness enforcement**:
   - Added `--require-fresh` flag to scanner CLI — exits nonzero if data is stale

7. **Phase 5.9 — Error persistence**:
   - Added `persist_scan_error()` function in `db_manager.py` — stores errors in `ingestion_log` with error type prefix

8. **Phase 5.10 — Config validation**:
   - Added `validate_config()` function in `prop_config.py` — checks threshold ordering, registry consistency, duplicate CLI names, empty names, freshness/comparison-book sanity
   - Called at CLI startup in `main()` — rejects invalid config with nonzero exit

9. **Phase 5.13 — Tests**: 21 new tests in `tests/test_phase5_integrity.py`:
   - Run tracking (4): create_run UUID, finish_run fields, metadata, ingestion_log
   - Config validation (5): valid config, threshold ordering (2), duplicate names, empty names
   - Error persistence (1): persist_scan_error
   - Database schema (2): scan_runs table, ingestion_log table
   - --min-ev YN rejection (2): rejected for yn, accepted for ou
   - --require-fresh (2): flag parsed, default false
   - Game filtering (4): away match, home match, short event_id ignored, long event_id matched
   - No-data hint (1): hint displayed when no approved rows

10. **Full suite: 432/432 passing** (411 original + 21 new)

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 25
- player_prop_scanner: 87
- phase5_integrity: 21
- Total: 432

### Next action

- **Phase 6**: Remaining pitcher props (pitches thrown, pitching win) or hitter props
- **Live verification**: Run `python -m src.player_prop_scanner --market strikeouts --all` with live data
- **Remaining items from original Phase 5 spec not yet implemented**:
  - 5.3: True idempotent upserts (currently INSERT-only, not INSERT OR REPLACE)
  - 5.8: JSON-formatted structured log output for production (current logging is human-readable)
  - 5.12: File-lock based concurrency for parallel CLI invocations (WAL mode handles DB concurrency)

### Important

- Generic scanner command: `python -m src.player_prop_scanner --market <name> [--market-form ou|yn] [--require-fresh]`
- Old command still works: `python -m src.strikeout_scanner [--all] [--market ou|yn|all]`
- Valid markets: `strikeouts`, `outs`, `hits_allowed`, `walks_allowed`, `earned_runs`, `all`
- Valid forms: `ou`, `yn`, `all`
- `--min-ev` applies only to O/U markets (rejected for yn with nonzero exit)
- `--require-fresh` exits nonzero if data exceeds freshness threshold
- Run IDs are UUIDs stored in `scan_runs` table — query with `SELECT * FROM scan_runs`
- Config validated at startup — invalid thresholds or registry cause immediate exit

### What was done

1. **Created `src/player_prop_scanner.py`** — generic scanner pipeline with:
   - `resolve_markets(market, form)` — validates market/form combinations against registry, rejects unsupported combos (e.g., `outs + yn`), silently filters `all + yn` to supported markets
   - `run_scan()` — full pipeline: fetch, parse, filter by market types, group, analyze, filter by sportsbook/player/game, sort, deduplicate, limit
   - `display_results()` / `display_verbose()` — registry-driven presentation, no hardcoded market-specific wording
   - `build_parser()` — generic CLI with `--market`, `--market-form`, `--sportsbook`, `--player`, `--game`, `--all`, `--positive-only`, `--actionable-only`, `--min-ev`, `--limit`, `--verbose`

2. **Refactored `src/strikeout_scanner.py`** — thin backward-compatible wrapper:
   - `run_scan()` delegates to `player_prop_scanner.run_scan(market="strikeouts")` with `--market ou|yn|all` mapped to `market_form`
   - `display_results()` / `display_verbose()` delegate to generic scanner
   - `parse_args()` / `main()` preserve identical CLI interface
   - No analysis logic remains in the wrapper (proven by test)

3. **Added `scanner_title` to `MarketConfig`** in `src/prop_config.py`:
   - `PITCHER_STRIKEOUTS.scanner_title = "MLB PITCHER STRIKEOUTS EDGE SCANNER"`
   - `PITCHER_OUTS.scanner_title = "MLB PITCHER OUTS RECORDED EDGE SCANNER"`
   - `PITCHER_HITS_ALLOWED.scanner_title = "MLB PITCHER HITS ALLOWED EDGE SCANNER"`
   - `PITCHER_WALKS_ALLOWED.scanner_title = "MLB PITCHER WALKS ALLOWED EDGE SCANNER"`
   - `PITCHER_EARNED_RUNS.scanner_title = "MLB PITCHER EARNED RUNS EDGE SCANNER"`

4. **Added 87 tests** in `tests/test_player_prop_scanner.py`:
   - TestMarketFormResolution: 14 tests (valid/invalid markets, forms, combinations, accepted types, scanner titles)
   - TestFiltering: 7 tests (sportsbook, player, case-insensitive, combined, no-match)
   - TestBackwardCompatibility: 12 tests (module entry, parse_args, delegation, display delegation)
   - TestGenericCLI: 13 tests (all flags, valid markets list)
   - TestCrossMarketScanner: 13 tests (titles for all 5 markets, O/YN support, no contamination)
   - TestYNOutput: 6 tests (no EV fields, price advantage fields, disclaimer, no EV in display)
   - TestFreshnessAndSource: 7 tests (stale/fresh, CACHE/LIVE/UNKNOWN, research-only, timestamps)
   - TestOutputStructure: 6 tests (O/U columns, YN columns, empty result, scanner title, verbose)
   - TestMinEvForYN: 2 tests (min-ev only applies to O/U)
   - TestStaleBlocking: 2 tests (stale cannot be actionable)
   - TestRegistryCompleteness: 4 tests (scanner_title, cli_name, valid_markets, lookups)
   - TestSingleImplementation: 2 tests (generic is canonical, wrapper has no pipeline)

5. **Full suite: 411/411 passing** (324 original + 87 new)

### Key findings

- **Zero analysis logic in the wrapper** — `strikeout_scanner.py` contains no `analyze_prop_group` or `analyze_yn_group` calls; all pipeline logic is in `player_prop_scanner.py`
- All 25 existing `test_strikeout_scanner.py` tests pass unchanged — backward compatibility confirmed
- Market/form resolution correctly rejects `outs + yn` and `hits_allowed + yn` with clear error messages
- `--market all --market-form yn` silently filters to only YN-supporting markets (strikeouts, walks_allowed, earned_runs)
- `--sportsbook`, `--player`, `--game` filters are case-insensitive substrings applied after analysis, before sorting/limiting
- Scanner titles are fully registry-driven — no hardcoded "PITCHER STRIKEOUT EDGE SCANNER" remains in display code

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 25
- player_prop_scanner: 87
- Total: 411

### Next action

- **Phase 5**: Remaining pitcher props (pitching_thrown, pitching_win) or hitter props
- **Live verification**: Run `python -m src.player_prop_scanner --market strikeouts --all` with live data

### Important

- Generic scanner command: `python -m src.player_prop_scanner --market <name> [--market-form ou|yn]`
- Old command still works: `python -m src.strikeout_scanner [--all] [--market ou|yn|all]`
- Valid markets: `strikeouts`, `outs`, `hits_allowed`, `walks_allowed`, `earned_runs`, `all`
- Valid forms: `ou`, `yn`, `all`
- Unsupported combos rejected: `outs + yn`, `hits_allowed + yn`
- `--min-ev` applies only to O/U markets
- YN output shows "SINGLE-SIDED MARKET COMPARISON / TRUE EV NOT AVAILABLE"

---

## Session: 2026-07-23 — Phase 3: Three additional pitcher prop markets via generic registry

### What was done

1. **API discovery** (completed in prior session):
   - `pitching_hits` — O/U only, 36 oddIDs, 10/10 events, both sides populated, alt lines present. Compatible.
   - `pitching_basesOnBalls` — O/U + YN, YN `byBookmaker` mostly sparse (same as strikeouts YN). Compatible.
   - `pitching_earnedRuns` — O/U + YN, same pattern as basesOnBalls. Compatible.
   - `pitching_homeRunsAllowed` — **NOT a market** (only live game stat). Cannot implement.

2. **Added 3 MarketConfig entries** in `src/prop_config.py`:
   - `PITCHER_HITS_ALLOWED`: `odd_id_stat_prefix="pitching_hits"`, `supports_ou=True`, `supports_yn=False`
   - `PITCHER_WALKS_ALLOWED`: `odd_id_stat_prefix="pitching_basesOnBalls"`, `supports_ou=True`, `supports_yn=True`
   - `PITCHER_EARNED_RUNS`: `odd_id_stat_prefix="pitching_earnedRuns"`, `supports_ou=True`, `supports_yn=True`

3. **Updated `_extract_player_name_from_market()`** in `src/player_prop_parser.py` — added suffixes "Hits Allowed Over/Under", "Walks Over/Under", "Earned Runs Over/Under" for player name extraction.

4. **Added 3 synthetic fixtures** in `tests/fixture_data.py`:
   - `hits_event`: 2 players (Cole 7.5, Verlander 6.5), 6 books on Cole main line, 5 on Verlander, alt lines (5.5, 8.5)
   - `walks_event`: O/U (Cole 2.5, Verlander 1.5) + YN (both players), 5-6 books
   - `earned_runs_event`: O/U (Cole 3.5, Verlander 2.5) + YN (both players), 5-6 books

5. **Added 69 tests** in `tests/test_additional_props.py`:
   - TestHitsAllowed: 19 tests (parsing, analysis, registry, isolation)
   - TestWalksAllowed: 20 tests (O/U + YN parsing, analysis, registry, isolation)
   - TestEarnedRuns: 19 tests (O/U + YN parsing, analysis, registry, isolation)
   - TestAllMarketsIsolation: 3 tests (6 markets independent, strikeout/YN regression)
   - TestStaleCache: 4 tests (observation time, unavailable/missing fields)
   - TestRegistryCompleteness: 4 tests (all markets in registry, all lookup functions)

6. **Full suite: 324/324 passing** (255 original + 69 new)

### Key findings

- **Zero market-specific production code** beyond the suffix fix in `_extract_player_name_from_market()`. All three new markets work entirely through the generic MarketConfig registry — same as Pitcher Outs in Phase 2.
- Parser dispatches via `cfg.match_ou_market()`/`cfg.match_yn_market()` automatically.
- Scanner groups via `cfg.get_market_by_ou_type()`/`cfg.get_market_by_yn_type()` automatically.
- Analysis uses existing `analyze_prop_group()` (O/U) and `analyze_yn_group()` (YN) — no new analysis functions needed.
- Hits Allowed has no YN variant; Walks and Earned Runs have both O/U and YN.
- `pitching_homeRunsAllowed` is NOT a market (only a live game stat).

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- additional_props: 69
- strikeout_scanner: 22
- Total: 324

### Next action

- **Phase 4**: Scanner display generalization — replace hardcoded "PITCHER STRIKEOUT EDGE SCANNER" with registry-based display names, avoid hardcoded strikeout wording
- **Live verification**: Run a fresh pregame scan with hits/walks/ER markets when available

### Important

- Hits/walks/ER markets appear in scanner alongside strikeouts and outs — no separate command needed
- `python -m src.strikeout_scanner --market ou --all` shows all O/U markets (strikeouts, outs, hits, walks, ER)
- `python -m src.strikeout_scanner --market yn --all` shows all YN markets (strikeouts, walks, ER)
- Walks and Earned Runs YN require at least 4 books (`YN_MIN_COMPARISON_BOOKS + 1`) for VALID market quality

---

## Session: 2026-07-23 — Phase 2: Pitcher Outs Recorded O/U integration

### What was done

1. **Fixed `_extract_player_name_from_market()`** in `src/player_prop_parser.py` — added `" Outs Recorded Over/Under"`, `" Outs Recorded O/U"`, `" Outs Recorded"`, `" Outs"` to the suffix list so player names are correctly extracted from "Gerrit Cole Outs Recorded Over/Under".

2. **Added synthetic outs fixture** in `tests/fixture_data.py` — `outs_event` with 2 players (Gerrit Cole 17.5, Justin Verlander 16.5), 6 books on Cole main line, 5 on Verlander, alt lines (15.5, 19.5) via `altLines` arrays.

3. **Added 49 comprehensive outs tests** in `tests/test_pitcher_outs.py` covering:
   - A: Valid normal market (7 tests) — correct count, market_type, player IDs/names, sides, lines, group keys, analysis (LOO, vig, fair_prob, EV)
   - B: Alt lines (4 tests) — separate groups, different lines, shared player, book count
   - C: Missing side (3 tests) — over-only excluded, under-only excluded, no YN variant
   - D: Insufficient books (3 tests) — 2 books INSUFFICIENT, 4 books INSUFFICIENT, 5 books VALID
   - E: Duplicates (1 test) — deterministic deduplication
   - F: Malformed line (2 tests) — missing line excluded, invalid line excluded
   - G: Invalid mapping (3 tests) — missing player ID, missing name, unavailable excluded
   - H: Positive EV (2 tests) — crafted odds with positive EV, extreme mispricing → STRONG_EDGE
   - I: Negative EV (2 tests) — all -110/-110 → all negative EV, all NO_EDGE still VALID
   - J: Freshness (1 test) — observation time preserved
   - K: Cross-market isolation (2 tests) — outs+strikeouts in same event → separate groups, different group keys
   - Regression (3 tests) — strikeout parsing, YN parsing, strikeout analysis unchanged
   - Registry (5 tests) — config values, match_ou, match_yn, type lookup, CLI name lookup
   - Field completeness (3 tests) — required fields, validation status, audit fields
   - Scanner grouping (2 tests) — scanner groups outs correctly, analysis produces opportunities
   - Stale blocking (1 test) — observation timestamps preserved

4. **Full suite: 255/255 passing** (206 original + 49 new outs tests)

### Key findings

- **Zero market-specific production code was required** beyond the suffix fix in `_extract_player_name_from_market()`. The entire outs integration works through the generic MarketConfig registry.
- Parser dispatches via `cfg.match_ou_market()` — automatically matches `pitching_outs-{PLAYER_ID}-game-ou-{side}`
- Scanner groups via `cfg.get_market_by_ou_type("pitching_outs_ou")` — automatically groups outs rows
- Analysis uses the same `analyze_prop_group()` function — LOO consensus, no-vig fair probability, EV, market quality
- No YN variant for outs (`PITCHER_OUTS.supports_yn = False`, `market_type_yn = None`)

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92
- pitcher_outs: 49
- strikeout_scanner: 22
- Total: 255

### Next action

- **Phase 3**: Scanner display generalization — replace hardcoded "PITCHER STRIKEOUT EDGE SCANNER" with registry-based display names, avoid hardcoded strikeout wording in outs output
- **Live verification**: Run a fresh pregame scan with outs markets when available

### Important

- Outs scanner command: `python -m src.strikeout_scanner --market ou --all` (outs appear alongside strikeouts in O/U section)
- Outs uses the same O/U analysis engine — no separate analysis needed
- Outs has no YN variant — `match_yn_market("pitching_outs-X-game-yn-yes")` returns None

---

## Session: 2026-07-23 — Phase 1: Market registry refactor

### What was done

1. **Created `MarketConfig` frozen dataclass** in `src/prop_config.py` — generic market type descriptor with fields: `cli_name`, `odd_id_stat_prefix`, `market_type_ou`, `market_type_yn`, `display_name`, `short_label`, `period`, `allowed_sides_ou`, `allowed_sides_yn`, `min_comparison_books_ou`, `min_comparison_books_yn`, `supports_ou`, `supports_yn`.

2. **Defined `PITCHER_STRIKEOUTS` and `PITCHER_OUTS`** as module-level `MarketConfig` instances. Added registry lookup functions: `match_ou_market(odd_id)`, `match_yn_market(odd_id)`, `get_market_by_cli_name()`, `get_market_by_ou_type()`, `get_market_by_yn_type()`.

3. **Refactored `player_prop_parser.py`**: `parse_player_props()` dispatches via `cfg.match_ou_market()`/`cfg.match_yn_market()` instead of hardcoded `_is_pitching_k_ou()`/`_is_pitching_k_yn()`. `_process_ou_market()`, `_process_entry()`, `_process_yn_market()`, `_process_yn_entry()` all accept `market_type` parameter. `_build_group_key()` and `_build_yn_group_key()` accept optional `market_type` param (defaults preserve old values).

4. **Refactored `strikeout_scanner.py`**: O/U vs YN grouping now uses `cfg.get_market_by_yn_type(market_type)`/`cfg.get_market_by_ou_type(market_type)` instead of hardcoded string comparison. Group data dicts now include `market_type` key. Opportunity dicts use `gdata["market_type"]` instead of hardcoded strings.

5. **Backward compatibility preserved**: All original constants (`STAT_ID`, `PERIOD`, `SIDE_*`, `_SIDE_MAP`, `_is_pitching_k_ou()`, `_is_pitching_k_yn()`, `_build_group_key()`, `_build_yn_group_key()`) still exist with default args matching old behavior.

6. **Added 12 regression tests**: Registry detection (6), group key format (1), backward-compat imports (1), analysis unaffected (1), Flaherty O/U/YN regression (2).

7. **Full suite: 206/206 passing**

### Key design decisions

- **Registry-based dispatch**: Parser and scanner discover markets via `MarketConfig` registry lookup, not hardcoded function calls. Adding a new market requires only a new `MarketConfig` entry.
- **Backward-compatible defaults**: `_build_group_key()` and `_build_yn_group_key()` default `market_type` to `"pitching_strikeouts_ou"` / `"pitching_strikeouts_yn"` respectively, preserving existing behavior for any code that calls them without the new parameter.
- **No changes to analysis module**: `player_prop_analysis.py` is already generic (accepts group data, not market-type-aware). No changes needed.

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 92 (55 original + 20 YN + 5 decimal_odds_advantage + 12 registry regression)
- strikeout_scanner: 22
- Total: 203 (actual: 206 — some overlap in count)

### Next action

- **Phase 2**: Outers recorded O/U integration (registry entry exists; parser/scanner dispatch via registry should pick it up automatically — verify)
- **Live verification**: Run a fresh pregame scan when live outs markets are available

## Session: 2026-07-21 — YN semantic audit

### What was done

1. **Renamed `price_difference_cents` → `decimal_odds_advantage`** across `player_prop_analysis.py`, `strikeout_scanner.py`, and `test_player_props.py`. The field computes `(offered_decimal - ref_decimal) × 100`, which is a decimal-scale metric — not American-odds cents.

2. **Added 5 targeted unit tests** for `_compute_decimal_odds_advantage`: negative-vs-negative, positive-vs-positive, negative-vs-positive crossing, positive-vs-negative crossing, and equal prices.

3. **Clarified threshold units** in `prop_config.py`: added comment that `0.08 = 8 percentage points`.

4. **Audit confirmed**: LOO median reference, minimum books rule (4 total, 3 comparison after LOO), and status thresholds all work correctly. All fixture recommendations were ineligible because no book had ≥ 4% price advantage.

5. **Full suite: 194/194 passing**

### Audit findings

- `price_difference_cents` was **incorrectly named** — it computed decimal-odds difference × 100, not American-odds cents. Renamed to `decimal_odds_advantage`.
- Recommendation eligibility: correct. All 5 fixture books are within ±1% of LOO median — tight market, no outliers.
- Median reference: correct. LOO exclusion verified for all 5 books. Even-sized medians averaged correctly.
- Threshold units: `0.08` = 8 percentage points (probability-point difference). Confirmed correct.

## Session: 2026-07-21 — Yes/No implementation complete

### What was done

1. **Implemented Pitcher Strikeout Yes/No as single-sided price-comparison market**

   - `src/prop_config.py`: Added YN comparison statuses (`STRONG_PRICE_OUTLIER`, `PRICE_OUTLIER`, `MARGINAL_PRICE_OUTLIER`, `IN_LINE_WITH_MARKET`, `WORSE_THAN_MARKET`), thresholds (8%/4%/2%), and `YN_MIN_COMPARISON_BOOKS = 3`.
   - `src/player_prop_parser.py`: Added `_is_pitching_k_yn()` filter, `_process_yn_market()`, `_process_yn_entry()`, `_build_yn_group_key()`. YN rows have `line=None`, `market_type="pitching_strikeouts_yn"`, no alt lines. No-side with empty `byBookmaker` produces audit-only row.
   - `src/player_prop_analysis.py`: Added `analyze_yn_group()` using LOO median implied probability as reference. Reports `price_advantage_pct`, `relative_payout_advantage_pct`, `decimal_odds_advantage`, `comparison_status`. No `ev_pct`, `fair_prob`, or `fair_odds` fields.
   - `src/strikeout_scanner.py`: Separated O/U and YN grouping. Added `yn_opportunities` to return dict. Added `--market ou|yn|all` CLI flag. Updated `display_results` and `display_verbose` with separate YN output sections labeled "SINGLE-SIDED MARKET COMPARISON / TRUE EV NOT AVAILABLE".

2. **Fixed 4 pre-existing tests** that didn't filter by `market_type` when iterating `odds_rows` (now that YN rows exist in the Flaherty fixture).

3. **Added 20 new YN-specific tests**: parser filter (3), parser extraction (7), analysis (7), group key (2), edge cases (1).

4. **Full suite: 194/194 passing, 0 skipped, 0 failed**

### Key design decisions

- **No true EV for YN**: Since only the Yes side has odds, two-sided vig removal is impossible. "Fair probability", "fair odds", and "expected value" are never computed or displayed for YN.
- **Reference method**: LOO median implied probability (median of all other books' implied probabilities).
- **Terminology**: `market_reference_probability`, `market_reference_odds`, `offered_implied_probability`, `price_advantage_pct`, `decimal_odds_advantage`, `comparison_status`, `recommendation_eligible`. Never reuse `fair_probability`, `fair_odds`, `no_vig_probability`, `expected_value`.
- **Scanner separation**: YN output is in a distinct section with its own column headers. The `--market` flag allows filtering.

### Test count breakdown

- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27
- player_props: 80 (55 original + 20 YN + 5 decimal_odds_advantage)
- strikeout_scanner: 22
- Total: 194

### Next action

- **Next market**: Remaining pitcher props (outs recorded, hits allowed) or hitter props.
- **Live verification**: Run a fresh pregame scan with `--market yn` when live YN markets are available.

### Important

- YN scanner command: `python -m src.strikeout_scanner --market yn --all`
- YN requires at least 4 books (`YN_MIN_COMPARISON_BOOKS + 1`) for VALID market quality
- Recommendation eligible: `STRONG_PRICE_OUTLIER` (>= 8% advantage) or `PRICE_OUTLIER` (>= 4% advantage)

---

## Previous Session: 2026-07-21 — Deterministic tests + Yes/No discovery

### What was done

1. **Replaced all cache-dependent fixtures with synthetic inline data**

   Created `tests/fixture_data.py` with 3 synthetic events:
   - `tb_tor_event`: 6 sportsbooks, betmgm priced opposite consensus (triggers POSSIBLE_MAPPING_ERROR)
   - `sf_kc_event`: 6 sportsbooks all matching consensus (betmgm not flagged)
   - `flaherty_event`: pitcher strikeout O/U for Flaherty (5 books, 5.5 line with alt lines) and Taillon (4.5/3.5 lines)

   Updated `tests/conftest.py`, `tests/test_participant_swap.py`, `tests/test_player_props.py` to use synthetic data instead of reading `data/_api_cache/_events_includeAltLines_true_leagueID_MLB_oddsAvailable_true.json`.

2. **Full suite: 169 passed, 0 skipped, 0 failed**

   Test count increased from 166 to 169 due to adding back the 5 `TestDatabaseRoundTrip` tests that previously skipped (they now use synthetic `_get_tb_tor()` instead of cache lookup).

3. **Updated project memory files** — PROJECT_STATUS.md, TODO.md, SESSION_HANDOFF.md updated.

### Current test count breakdown

7 + 15 + 40 + 27 + 55 + 22 = 166 original + 3 new tests... Actually 169 total means the database round-trip tests that were previously skipped are now counted. The earlier count of 166 may have excluded them. Let me verify: 166 was pre-synthetic. After adding synthetic data, all 5 `TestDatabaseRoundTrip` tests now execute instead of skip. So 166 + 3 = 169? Let me recount:

Test breakdown after this session:
- stage1: 7
- stage2: 15
- stage3: 40
- participant_swap: 27 (22 original + 5 round-trip that no longer skip)
- player_props: 55 (55 original, some no longer skipped)
- strikeout_scanner: 22

Total: 7 + 15 + 40 + 27 + 55 + 22 = 166... but 169 is the actual count. 

Actually, looking at the original 166-2026-07-21 state, the `TestDatabaseRoundTrip` tests had `pytest.skip()` paths so they were **collected** but **skipped** — they counted as 5 skipped tests. Now they run (5 passes). Plus some player_props tests may have been skipped too. So real count is 169 passing.

### Next action

**Pitcher Outs Recorded O/U — discovery complete, no implementation.**

Odd ID: `pitching_outs-{PLAYER_ID}-game-ou-{side}`. Structurally identical to strikeouts O/U — both sides populated, same `betTypeID: "ou"`, same `periodID: "game"`. No YN variant. 22 odd IDs across 11 pitchers in cache. Existing O/U analysis (`analyze_prop_group`) is fully reusable. `player_prop_odds` table needs no migration. Parser needs new `_is_pitching_outs_ou` filter and parameterized market_type. Scanner needs to handle outs as third market type or be generalized.

### Important

- Tests are now fully deterministic — no cache dependency, no skip()
- Fresh API responses still need live API calls (for discovery only, not tests)
- The stale-data freshness limitation still exists (captured_at uses parse time, not API timestamp)

---

## Discovery Report: Pitcher Strikeout Yes/No

### Exact Market Key(s)

| Side | oddID Pattern |
|------|--------------|
| Yes | `pitching_strikeouts-{PLAYER_ID}-game-yn-yes` |
| No | `pitching_strikeouts-{PLAYER_ID}-game-yn-no` |

betTypeID: `"yn"`, sideID: `"yes"` / `"no"`.

### Market Classification

- marketGroupName: `"Player Any Strikeouts Yes/No"`
- marketGroupNameAlias: `"Player Anytime Strikeouts"`
- True binary market (bet whether pitcher records >= 1 K). No line/number involved.

### Player Identifiers

- `playerID`: Same format (e.g. `"JACK_FLAHERTY_1_MLB"`)
- `statEntityID`: Same value as playerID
- `playerNames`: Not present in raw API response (same as O/U)
- Player name only in `marketName`: `"Jack Flaherty Any Strikeouts Yes/No"`

### Side / Outcome

- `sideID`: `"yes"` / `"no"`
- Both linked via `opposingOddID` — same pattern as O/U

### Sportsbook Coverage

From markets endpoint: 8 active events for Yes, 0 for No. Supported books include: draftkings, espnbet, bet365, bovada, caesars, fanatics, fliff, hardrockbet, pinnacle, prizepicks, underdog, novig, betrivers, betparx, betonline, mybookie, prophetexchange.

Fewer than O/U (27 books). In today's cache, best coverage was 2 books (draftkings, espnbet).

### Key Structural Difference from O/U

**Critical**: Only the Yes side has `byBookmaker` entries. The No side is always empty (`{}`). The implied probability for "No" is `1 - prob_yes`. This means:

- The existing `analyze_prop_group` (pairs Over/Under) cannot be reused
- Need a new single-side LOO analysis: `analyze_yn_group`
- Group key: `"{event_id}|{player_id}|pitching_strikeouts_yn|game"` (no line component)

### Alt Lines

**None.** Binary market, nothing to alternate.

### No Line

YN has no line field. `line` in the database should be `NULL`.

### Settlement

- Yes: pitcher records >= 1 strikeout
- No: pitcher records 0 strikeouts (extremely rare for starters)
- API confirms: `"score": 2` during in-play for a Yes-side odd

### Recommended Schema

Reuse `player_prop_odds` and `player_prop_mapping_audit` tables as-is. New values:
- `market_type`: `"pitching_strikeouts_yn"`
- `side`: `"YES"` / `"NO"`
- `line`: `NULL`
- `market_group_key`: `"{event_id}|{player_id}|pitching_strikeouts_yn|game"`

### Fixture

Added to `tests/fixture_data.py`: Flaherty event now includes YN odds (5 books on Yes, empty on No) alongside existing O/U odds. All existing tests still pass (169/169).

### Next Implementation Steps

1. Add `_is_pitching_k_yn(odd_id)` filter in `player_prop_parser.py`
2. In `parse_player_props`, detect YN odds and extract side, set market_type, line=None
3. Add `analyze_yn_group()` in `player_prop_analysis.py` — single-side LOO consensus
4. Update `strikeout_scanner.py` to include YN groups in output
5. Write tests: filter, parse, analyze, pipeline, scanner integration
6. Full suite pass

## Session: 2026-07-28 — Fix PostgreSQL file-guard bug in control_panel.py

### What was done

Fixed a critical bug where dashboard helpers (`_get_latest_run_id`, `_get_schedule_summary`, `_load_recs`, `_get_live_game_warnings`) checked `Path(db_path).exists()` before calling `get_connection()`. On Render, `DATABASE_URL` is set and `get_connection()` correctly connects to PostgreSQL, but the file-existence guard returned early with empty/default values before PostgreSQL was ever queried.

**Root cause:** `_get_latest_run_id()`, `_get_schedule_summary()`, `_load_recs()`, and `_get_live_game_warnings()` all had `if not Path(db_path).exists(): return ...` guards. These were written for SQLite where the file must exist. On Render PostgreSQL, the file doesn't exist (PostgreSQL is remote), so every guard returned empty data.

### Files changed

1. **`src/control_panel.py`**:
   - Added `from database.connection import get_database_url` import
   - Added `_is_postgres()` helper — returns True when `DATABASE_URL` is set
   - Added `_should_query(db_path)` helper — returns True if PostgreSQL or SQLite file exists
   - Replaced 4 `if not Path(db_path).exists()` guards with `if not _should_query(db_path)`:
     - `_load_recs()` (line 117)
     - `_get_latest_run_id()` (line 175)
     - `_get_schedule_summary()` (line 206)
     - `_get_live_game_warnings()` (line 277)
   - Updated DB labels to show "PostgreSQL" when configured:
     - Header label (line 419)
     - Database & Storage metric (line 1226)
     - DB Size metric (line 1228) — shows "Managed (PostgreSQL)" instead of file size
     - Footer label (line 1719)
   - Added `last_run_time` population from `scan_runs.finished_at` at session start

2. **`tests/test_phase17b_postgres.py`**:
   - Added `TestPostgresPathGuard` class with 11 tests covering:
     - `_is_postgres()` True/False
     - `_should_query()` for all 4 combinations (Postgres + no file, SQLite + no file, SQLite + file)
     - Guard bypass for all 4 dashboard functions with PostgreSQL + mock data
     - SQLite guard still returns empty when path missing (regression)
     - `get_database_url` is importable from control_panel

### Test results

- **1402 passed**, 1 pre-existing flaky failure (`test_schedule_pregame_checks`)
- All 11 new tests pass
- All 75 dashboard regression tests pass

### Commit message

```
Fix dashboard PostgreSQL file guards

- Add _is_postgres() / _should_query() helpers in control_panel.py
- Replace 4 Path(db_path).exists() guards with _should_query() so
  PostgreSQL queries proceed even when the SQLite file is missing
- Show "PostgreSQL" in DB labels when DATABASE_URL is configured
- Populate last_run_time from scan_runs at session start
- 11 new regression tests for guard bypass behavior
```
