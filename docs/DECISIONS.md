# docs/DECISIONS.md — Architecture decision log

Entries are dated. New entries are appended.

---

## SQLite is the source of truth

- **Decision**: Store all parsed odds, audit records, and raw API responses in SQLite.
- **Reason**: Zero-config, file-based, sufficient for single-user analysis. Entire DB ships with the repo.
- **Consequence**: Queries are always against local data. No network dependency after ingest. Google Sheets will be a read-only display layer later, never the primary store.

## Google Sheets is a display layer only

- **Decision**: Google Sheets will consume from SQLite (not the other way around). Never write back.
- **Reason**: SQLite is simpler, faster, and avoids API rate limits. Sheets are for human dashboards.
- **Consequence**: All data enrichment must go through the SQLite pipeline first.

## Validation statuses are stored per odds row

- **Decision**: Every row in the `odds` table carries `validation_status`, `mapping_confidence`, `mapping_method`, and `validation_reason`.
- **Reason**: Enables SQL-level filtering without joins. Each row is self-describing.
- **Consequence**: More columns but simpler queries and bulletproof exclusion.

## Audit data is stored in separate tables

- **Decision**: `odds_mapping_audit` and `player_prop_mapping_audit` are separate from the main odds tables.
- **Reason**: Audit rows include provenance data (entity IDs, mapping details) that are not needed for analysis queries. Separation keeps main tables lean.
- **Consequence**: Slightly more write complexity during ingest, but audit trail is preserved permanently.

## Consensus cannot auto-correct participant mappings

- **Decision**: Consensus-based sign analysis only assigns validation statuses. Prices are never swapped.
- **Reason**: Automatic correction risks masking real API errors or edge cases. Exclusion is safer than assumption.
- **Consequence**: Some sportsbooks may be excluded on a given day. Human review is required to understand why.

## Stable IDs are required

- **Decision**: Use `statEntityID` for teams (`"away"` / `"home"`) and `playerID` for player props. Never infer from array order or price sign.
- **Reason**: Array order and price sign are unreliable. Stable API fields provide deterministic mapping.
- **Consequence**: Player props required different parsing logic (no statEntityID), leading to a separate parser module.

## Leave-one-book-out consensus for player props

- **Decision**: EV for each sportsbook uses a fair probability computed from ALL OTHER books (LOO).
- **Reason**: A book's own price would bias the fair probability toward zero EV. LOO gives an independent benchmark.
- **Consequence**: Requires at least 2 other books (so 5 total paired books) for VALID market. More computation but more honest EV.

## Over/Under before Yes/No for pitcher strikeouts

- **Decision**: Pitcher strikeout Over/Under was implemented first. Yes/No is deferred.
- **Reason**: O/U is the more standard market format. Settling logic is simpler (just the line).
- **Consequence**: Yes/No will reuse the same parser infrastructure with a different side extraction and grouping strategy.

## Market quality and bet quality are separate

- **Decision**: `market_quality_status` (VALID / NEEDS_REVIEW / INSUFFICIENT / EXCLUDED) is distinct from `bet_status` (STRONG_EDGE / POSITIVE_EDGE / MARGINAL_EDGE / NO_EDGE / EXCLUDED).
- **Reason**: A market with 5+ paired books is valid even if every bet has negative EV. Mixing the two would conflate data sufficiency with betting opportunity.
- **Consequence**: Two classification steps in the analysis pipeline. Clearer output but more code.

## Thresholds centralised and dynamically referenced

- **Decision**: All edge thresholds live in `src/prop_config.py` as module-level constants.
- **Reason**: Single point of adjustment. Tests can override for scenario verification.
- **Consequence**: The analysis module (`player_prop_analysis.py`) initially imported names by name (e.g., `from .prop_config import STRONG_EDGE_THRESHOLD`), which meant runtime changes to the config module did not propagate. **Fixed on 2026-07-20** by refactoring to use module import (`from . import prop_config as cfg`) and accessing via `cfg.STRONG_EDGE_THRESHOLD` at runtime. Tests now mutate the public config module directly.

## Added actionable threshold for scanner

- **Decision**: An `ACTIONABLE_EDGE_THRESHOLD` (default 2%) and `FRESHNESS_THRESHOLD_SECONDS` (default 3600) were added to `prop_config.py`.
- **Reason**: The strikeout scanner needs a configurable threshold for actionable mode and a freshness cutoff for stale-data warnings.
- **Consequence**: Scanner mode defaults come from central config; overridable via `--min-ev` CLI flag.

## Ranked strikeout scanner created

- **Decision**: Built as a module at `src/strikeout_scanner.py` with CLI entry point `python -m src.strikeout_scanner`.
- **Reason**: Provide a single command to see all, positive, or actionable strikeout opportunities ranked by EV.
- **Consequence**: Reuses existing parser, analysis, and config infrastructure. No new data model or database changes.

## Deduplication keeps first observed occurrence

- **Decision**: When multiple rows represent the exact same (event, player, line, side, book), the first one in the sorted list (which is the highest EV) is kept.
- **Reason**: Since opportunities are sorted by EV before deduplication, keeping the first means the best EV wins.
- **Consequence**: If consecutive pulls show different odds for the same observation, the best (highest EV) entry remains.

## Scanner modes are exclusive

- **Decision**: `--all`, `--positive-only`, and `--actionable-only` are mutually exclusive flags.
- **Reason**: Prevents contradictory mode combinations. Default is actionable if no flag given.
- **Consequence**: Simplifies CLI and ensures unambiguous user intent.

## Deterministic fixtures replace cache-dependent tests

- **Decision**: All tests use synthetic inline API event dicts from `tests/fixture_data.py` instead of reading the mutable API cache.
- **Reason**: Cache is overwritten by live API calls, causing 40+ tests to skip when old event IDs disappear. Synthetic data is deterministic and permanent.
- **Consequence**: Tests never skip, never depend on live API, and always reproduce identically. The cache file still exists for the real pipeline but tests no longer reference it.

## Yes/No strikeout markets use single-side analysis

- **Decision**: Unlike O/U (both sides have prices for every book), YN only has odds on the Yes side. Analysis must use a single-side LOO consensus model.
- **Reason**: The API returns `byBookmaker` only for the Yes oddID. The No side is always empty in the events response.
- **Consequence**: `analyze_prop_group` (which pairs Over/Under) cannot be reused. A new `analyze_yn_group` function is needed for single-side LOO consensus, with `prob_no = 1 - prob_yes`.

## YN uses price advantage metrics, not EV

- **Decision**: Yes/No markets report price advantage metrics (implied probability difference, relative payout advantage, decimal odds advantage) instead of EV, fair probability, or fair odds.
- **Reason**: Without a complementary No-side price, two-sided vig removal is impossible. "Fair probability" and "expected value" would be misleading. The reference method is LOO median implied probability.
- **Consequence**: Scanner output for YN uses a separate section labeled "SINGLE-SIDED MARKET COMPARISON / TRUE EV NOT AVAILABLE". Recommendation eligibility is based on `STRONG_PRICE_OUTLIER` (>= 8%) and `PRICE_OUTLIER` (>= 4%) thresholds, not EV.

## YN scanner uses --market flag

- **Decision**: The strikeout scanner accepts `--market ou|yn|all` to filter by market type.
- **Reason**: YN and O/U have fundamentally different output formats (EV vs price advantage). Mixing them in a single table would be confusing.
- **Consequence**: Default is `all` (shows both sections). `--market yn` shows only YN results. `--market ou` shows only O/U results.

## YN field named `decimal_odds_advantage`, not `cents`

- **Decision**: The field is named `decimal_odds_advantage` (formula: `round((offered_decimal - ref_decimal) * 100)`), not `price_difference_cents`.
- **Reason**: The formula computes a decimal-odds-scale difference, not American-odds cents. In sportsbook convention, "cents" means American-odds points (e.g., -110 vs -105 is a 5-cent gap). The computed value is meaningful but not conventional cents.
- **Consequence**: Column header shows `DecAdv` in scanner output. Field value is positive when offered odds are better (higher payout per dollar).

## Tests use in-memory databases

- **Decision**: Every test gets a fresh in-memory SQLite database via the `db_conn` fixture in `conftest.py`.
- **Reason**: Isolated, fast, no cleanup needed. Never touches `mlb_model.db`.
- **Consequence**: Tests are safe to run at any time without affecting real data. Schema must be recreated per fixture.

## Market registry replaces hardcoded dispatch

- **Decision**: `MarketConfig` frozen dataclass in `src/prop_config.py` replaces hardcoded parser/scanner dispatch. Parser calls `cfg.match_ou_market()`/`cfg.match_yn_market()` instead of `_is_pitching_k_ou()`/`_is_pitching_k_yn()`. Scanner uses `cfg.get_market_by_ou_type()`/`cfg.get_market_by_yn_type()` instead of string comparison.
- **Reason**: Hardcoded dispatch means every new market requires editing parser, scanner, and tests in multiple places. A registry lets you add a market by defining one `MarketConfig` entry.
- **Consequence**: `PITCHER_STRIKEOUTS` and `PITCHER_OUTS` are defined as registry entries. `_is_pitching_k_ou()`, `_is_pitching_k_yn()`, `_build_group_key()`, `_build_yn_group_key()` are preserved as backward-compatible wrappers with default args matching old behavior. 12 regression tests verify identical results.

## Pitcher Outs uses the same generic O/U engine as strikeouts

- **Decision**: Pitcher Outs Recorded O/U is implemented entirely through the generic MarketConfig registry — no separate outs parser, outs analysis module, or outs-specific scanner logic.
- **Reason**: The O/U analysis pipeline (exact-line grouping, paired Over/Under, LOO consensus, no-vig fair probability, EV calculation, market quality classification) is structurally identical for all two-sided O/U markets. Outs differ only in `market_type` and `odd_id_stat_prefix`.
- **Consequence**: Outs markets are automatically recognized by `cfg.match_ou_market()`, parsed into the same canonical record structure, grouped by `market_group_key`, and analyzed by `analyze_prop_group()`. The scanner groups and ranks them alongside strikeouts. Only `_extract_player_name_from_market()` needed a new suffix pattern for "Outs Recorded Over/Under".

## Three new pitcher prop markets added via generic registry (Phase 3)

- **Decision**: Added `PITCHER_HITS_ALLOWED`, `PITCHER_WALKS_ALLOWED`, `PITCHER_EARNED_RUNS` to `MarketConfig` registry. No changes to parser dispatch, scanner grouping, or analysis functions.
- **Reason**: The registry-based architecture (Phase 1) was designed so adding a new market requires only a `MarketConfig` entry and suffixes in `_extract_player_name_from_market()`. All three new markets were confirmed structurally identical to existing O/U markets via API discovery.
- **Consequence**: Parser dispatches via `cfg.match_ou_market()`/`cfg.match_yn_market()` automatically. Scanner groups via `cfg.get_market_by_ou_type()`/`cfg.get_market_by_yn_type()` automatically. Analysis uses existing `analyze_prop_group()` and `analyze_yn_group()` functions. Only `_extract_player_name_from_market()` needed new suffix patterns for "Hits Allowed Over/Under", "Walks Over/Under", "Earned Runs Over/Under".

## `pitching_homeRunsAllowed` is not a market

- **Decision**: `pitching_homeRunsAllowed` appears only as a live game stat (`homeRunsAllowed`), not as a betting market with oddIDs. It cannot be implemented.
- **Reason**: API discovery confirmed the field is a live statistic, not a sportsbook market. No oddID pattern exists.
- **Consequence**: Removed from implementation scope. Remaining unimplemented pitcher props: `pitching_pitchesThrown` (O/U, low event count), `pitching_win` (YN only, low event count).

## Generic scanner replaces strikeout-specific scanner (Phase 4)

- **Decision**: Created `src/player_prop_scanner.py` as the single source of truth for all scanner logic. `src/strikeout_scanner.py` becomes a thin backward-compatible wrapper that delegates to the generic scanner with `market="strikeouts"`.
- **Reason**: The strikeout scanner had hardcoded presentation (title, column labels, empty-state messages) and a `--market ou|yn|all` flag that was ambiguous with market selection. A generic scanner with separate `--market` (market name) and `--market-form` (ou/yn/all) flags is cleaner and scales to all markets.
- **Consequence**: All scanner logic lives in one module. `--market` accepts registry CLI names (`strikeouts`, `outs`, `hits_allowed`, `walks_allowed`, `earned_runs`, `all`). `--market-form` accepts `ou`, `yn`, or `all`. Old command `python -m src.strikeout_scanner` still works identically. Unsupported market/form combinations (e.g., `outs + yn`) are rejected with a nonzero exit code.

## MarketConfig gains scanner_title field

- **Decision**: Added `scanner_title: str` field to `MarketConfig` dataclass. Each market defines its own scanner header (e.g., `"MLB PITCHER STRIKEOUTS EDGE SCANNER"`).
- **Reason**: Scanner titles were hardcoded in `display_results()`. Registry-driven titles eliminate hardcoded market-specific wording and scale automatically to new markets.
- **Consequence**: Each `MarketConfig` instance now has a `scanner_title`. The generic scanner reads `scanner_title` from the resolved market config. "ALL MARKETS" mode uses a generic title.

## Filtering is case-insensitive substring matching

- **Decision**: `--sportsbook`, `--player`, and `--game` filters use case-insensitive substring matching (`.lower()` + `in`).
- **Reason**: Users shouldn't need exact case-sensitive input. Substring matching is more forgiving (e.g., "flaherty" matches "Jack Flaherty").
- **Consequence**: Filters are applied after analysis but before sorting and limiting. Combined filters narrow results progressively.

## `--market all --market-form yn` silently filters

- **Decision**: When `--market all` is combined with `--market-form yn`, markets that don't support YN are silently filtered out rather than rejected.
- **Reason**: `--market all` implies "all supported markets for the requested form." Rejecting because some markets lack YN would be unhelpful.
- **Consequence**: The resolver returns only markets where `supports_yn=True` when form is `yn` and market is `all`. Specific market names (e.g., `outs + yn`) still reject with a clear error.

## `--min-ev` rejected for YN markets

- **Decision**: `--min-ev` is rejected with a nonzero exit code when `--market-form yn` is explicitly requested.
- **Reason**: EV is not computed for YN markets (no complementary price for vig removal). Silently ignoring `--min-ev` is confusing — users think their threshold is being applied when it is not.
- **Consequence**: `--min-ev` can only be used with O/U markets or `--market-form all` (which includes O/U). The scanner's `run_scan()` still accepts `min_ev` for programmatic use.

## `--require-fresh` exits nonzero on stale data

- **Decision**: Added `--require-fresh` flag that exits with code 1 if data exceeds the freshness threshold.
- **Reason**: Automated pipelines need a way to fail loudly when fresh data is unavailable, rather than silently returning stale results.
- **Consequence**: The flag is checked after `run_scan()` returns. If `stale_warning` is True, the scanner prints an error to stderr and exits 1.

## Config validated at startup

- **Decision**: `validate_config()` is called at CLI startup and rejects invalid configurations with a nonzero exit code.
- **Reason**: Misconfigured thresholds (e.g., STRONG <= POSITIVE) would silently produce incorrect analysis results. Failing fast prevents corrupt output.
- **Consequence**: `validate_config()` checks threshold ordering, registry consistency (no duplicate CLI names, no empty names), and freshness/comparison-book sanity.

## Run tracking for auditability

- **Decision**: Every scan and ingestion run gets a UUID-based run_id stored in a `scan_runs` table. Ingestion events are logged to `ingestion_log` with the run_id.
- **Reason**: Reproducibility and debugging require knowing which data was fetched when, and which run produced which results.
- **Consequence**: `create_run()` and `finish_run()` bracket every scan. `log_ingestion()` records per-event ingestion. The scanner's result dict includes `run_id` for traceability. All DB operations are wrapped in try/except to avoid breaking scans if the DB is unavailable.

## API retry with exponential backoff

- **Decision**: API requests retry up to 3 times with exponential backoff (1s, 2s, 4s) on connection errors, timeouts, and HTTP 429/5xx.
- **Reason**: The free-tier API is rate-limited and occasionally returns transient errors. Retry logic avoids losing entire scan runs to single-request failures.
- **Consequence**: `_request_with_retry()` wraps `session.get()`. Rate-limit responses (429) and server errors (500/502/503/504) trigger retries. After max retries, the last exception is raised.

## Rate limiting between API calls

- **Decision**: A minimum 1-second interval is enforced between live API calls via `time.monotonic()` tracking.
- **Reason**: The free plan has strict rate limits. Violating them triggers 429 responses and potential account restrictions.
- **Consequence**: Cached responses bypass the rate limiter. Only actual API calls trigger the sleep. The interval is configurable via `MIN_API_INTERVAL` class variable.

## Cache staleness detection

- **Decision**: `SportsGameOddsClient` accepts an optional `max_cache_age` parameter. When set, cache files older than the threshold are re-fetched instead of served.
- **Reason**: The default behavior (serve all cache regardless of age) means users may unknowingly work with day-old data.
- **Consequence**: `max_cache_age` is in seconds. `clear_stale_cache()` deletes old files. `get_cache_info()` provides observability. Default behavior (None) preserves backward compatibility.

## Daily production pipeline (Phase 7)

- **Decision**: Created `src/daily_pipeline.py` as a single-command 9-stage pipeline from config validation through report generation. Uses `run_scan()` from the generic scanner, `save_recommendation()` from Phase 6, and produces CSV/JSON/text reports.
- **Reason**: Manual multi-step workflow (ingest → scan → grade → report) is error-prone and not automatable. A pipeline enables future scheduling (cron, serverless) and ensures consistent execution.
- **Consequence**: Pipeline is a standalone CLI (`python -m src.daily_pipeline`). It reuses all existing modules but adds its own state management (`PipelineConfig`, `PipelineState`) and structured exit codes.

## Pipeline uses module-level imports (not local)

- **Decision**: `daily_pipeline.py` imports `SportsGameOddsClient`, `run_scan`, `parse_odds`, etc. at module level rather than inside functions.
- **Reason**: Local imports were originally used to avoid circular imports, but they broke test patching (`patch("src.daily_pipeline.SportsGameOddsClient")` cannot target local imports). Module-level imports are more testable and the standard Python pattern.
- **Consequence**: No circular import issues exist in the current codebase. Test patching works correctly with `patch()` targeting the module namespace.

## Pipeline defaults to actionable-only mode

- **Decision**: When no mode flag (`--actionable-only`, `--positive-only`, `--all-markets`) is given, the pipeline defaults to actionable-only. The argparse default is `False` for `--actionable-only`, and `main()` converts "no mode flag" to `actionable_only=True`.
- **Reason**: Production pipelines should filter to actionable opportunities by default to avoid noise. Users must explicitly opt into broader modes.
- **Consequence**: `PipelineConfig.actionable_only` defaults to `True`. Argparse `--actionable-only` defaults to `False` (mutual exclusion group), and `main()` sets `actionable_only=True` when no flag is given.

## Pipeline exit codes are standardized

- **Decision**: Six distinct exit codes: 0 (success with recs), 1 (success no recs), 2 (config failure), 3 (API failure), 4 (DB failure), 5 (validation failure), 6 (unexpected failure).
- **Reason**: Automated scheduling (cron, CI/CD) needs machine-readable exit codes to determine retry logic, alerting, and logging. Different failure modes require different responses.
- **Consequence**: Exit codes are module-level constants. `run_pipeline()` returns the appropriate code. CI/CD can distinguish "no data" (exit 1) from "broken config" (exit 2) from "API down" (exit 3).

## Pipeline dry-run mode skips all writes

- **Decision**: `--dry-run` runs all 9 stages but skips database writes, file output, and persists no run records.
- **Reason**: Users need to verify pipeline behavior without side effects. Dry-run validates the full execution path including config validation, API calls, and report building.
- **Consequence**: Dry-run prints what *would* happen at each stage. No files are created. No DB records are written. Exit codes still reflect pipeline logic (e.g., dry-run with no events returns 1).

## 14 new markets added via registry-only approach (Phase 8)

- **Decision**: Added 14 new `MarketConfig` entries (10 batter, 4 pitcher/composite) to `MARKET_REGISTRY`. Zero changes to parser dispatch, scanner grouping, analysis functions, or pipeline logic.
- **Reason**: Phase 1's registry-based architecture was designed to scale — adding a market should require only a `MarketConfig` entry and name-extraction suffixes. API discovery confirmed all 14 markets follow the identical structural pattern (odd_id prefix + playerID + game + betType + side).
- **Consequence**: Registry expanded from 5 to 20 entries. Parser dispatches automatically via `cfg.match_ou_market()`/`cfg.match_yn_market()`. Scanner groups automatically. Pipeline CLI market choices derived from registry dynamically.

## `pitching_win` reclassified as YN-only

- **Decision**: `pitching_win` is registered as YN-only (`supports_ou=False`, `supports_yn=True`), not O/U+YN.
- **Reason**: API discovery confirmed the market only has Yes-side odds. No Over/Under variant exists.
- **Consequence**: `pitching_win` is filtered out when `--market-form ou` is requested. Available only via `--market-form yn` or `--market-form all`.

## `pitching_pitchesThrown` is O/U-only

- **Decision**: `pitching_pitchesThrown` is registered as O/U-only (`supports_ou=True`, `supports_yn=False`).
- **Reason**: API discovery confirmed only O/U structure exists. YN variant does not exist.
- **Consequence**: Filtered out when `--market-form yn` is requested. Very low book coverage (0 books with data in discovery cache).

## Low-coverage markets registered anyway

- **Decision**: `pitching_win` (max 1 book), `pitches_thrown` (0 books), and `first_home_run` (max 2 books) are registered in the market registry despite insufficient book coverage for actionable recommendations.
- **Reason**: Registering them now means they will automatically produce recommendations when book coverage improves (e.g., during playoffs or when new sportsbooks add these markets). Removing them later and re-adding would require more work than leaving them registered.
- **Consequence**: These markets will appear in `--market all` scans but will likely produce no recommendations (INSUFFICIENT book quality). Users see "no data" rather than a crash.

## Pipeline CLI market choices derived from registry

- **Decision**: `daily_pipeline.py` builds its `--market` argparse choices from `MARKET_REGISTRY` dynamically rather than using a hardcoded list.
- **Reason**: Hardcoded lists go stale when markets are added. Registry-derived choices are always current.
- **Consequence**: New markets are automatically available in the pipeline CLI without touching `daily_pipeline.py`. Choices include all 20 CLI names plus `all`.

## Closing prices captured at freeze time (Phase 9)

- **Decision**: Closing prices are captured automatically when recommendations are frozen in the pipeline. The system looks up the latest odds for each recommendation's market from `player_prop_odds` and stores them in `closing_prices`.
- **Reason**: Manual closing-price capture is error-prone and easily forgotten. Automating it at freeze time ensures every recommendation has a closing reference point. CLV can then be computed as `bet_implied_prob - closing_implied_prob`.
- **Consequence**: `capture_closing_prices()` runs after `save_recommendation()` in the freeze stage. Records that already have closing prices are skipped (idempotent). CLV is only available when odds exist at freeze time and the line hasn't changed.

## EV buckets use percentage points, ev_pct stored as decimal

- **Decision**: `EV_BUCKETS` thresholds are in percentage points (2 = 2%, 5 = 5%). `ev_pct` in the database is stored as a decimal (0.02 = 2%). The analytics engine converts `ev_pct` to percentage points (`ev * 100`) before bucket comparison.
- **Reason**: The existing bucket definitions in `grading.py` predate Phase 9 and use percentage-point integers. Changing them would break existing tests and downstream code.
- **Consequence**: `roi_by_ev_bucket()` multiplies `ev_pct` by 100 before calling `test_fn`. Bucket labels remain human-readable ("2_to_5" = 2-5%).

## Confidence weights are configurable, not hardcoded

- **Decision**: Confidence score weights live in `CONFIDENCE_WEIGHTS` dict in `prop_config.py`. The `ConfidenceWeights` dataclass in `confidence.py` loads from this config.
- **Reason**: Different use cases may value different components (e.g., a conservative system might weight market_quality higher; an aggressive system might weight ev_magnitude higher). Making weights configurable allows tuning without code changes.
- **Consequence**: Default weights: n_books=2.0, market_quality=1.5, ev_magnitude=2.5, freshness=1.0, mapping_confidence=1.0. Total weight = 7.0. Users adjust via `CONFIDENCE_WEIGHTS` in `prop_config.py`.

## Calibration recommends but never auto-changes thresholds

- **Decision**: The calibration analyzer identifies profitable/unprofitable EV buckets and suggests threshold adjustments, but never modifies `STRONG_EDGE_THRESHOLD` or `POSITIVE_EDGE_THRESHOLD` automatically.
- **Reason**: Automatic threshold changes based on historical data could overfit to past performance. Human judgment is required to evaluate whether a threshold change is warranted.
- **Consequence**: `analyze_calibration()` returns `recommendations` list with `reason` strings. A future CLI or dashboard can display these for human review.

---

## Google Sheets and Discord are optional integrations

- **Decision**: Google Sheets export and Discord delivery are optional features that must not be hard dependencies. If libraries are unavailable or webhooks are not configured, the system continues functioning normally.
- **Reason**: Core pipeline value is market analysis and recommendation generation. Delivery channels are presentation layers that should never block the core workflow.
- **Consequence**: `export_sheets.py` checks `_HAS_GOOGLE` at import time; `discord_delivery.py` uses stdlib `urllib` only. Both modules return success (with 0 sent) when not configured. Health checks report integration readiness separately.

## Scheduling is platform-neutral

- **Decision**: Production scheduling generates configuration files for multiple platforms (cron, Windows Task Scheduler, GitHub Actions, cloud runners) rather than implementing a scheduler within the application.
- **Reason**: An always-running Python scheduler process adds operational complexity, resource consumption, and failure modes. Platform-native schedulers (systemd timers, crontab, cloud cron) are more reliable and familiar to operators.
- **Consequence**: `src/scheduler.py` is a code generator, not a runtime scheduler. It produces platform-specific configs that operators install independently. No daemon process is required.

## Database backup uses SQLite online backup API

- **Decision**: Backups use `sqlite3.Connection.backup()` (SQLite online backup API) rather than file copy or SQL dump.
- **Reason**: The online backup API is safe for live databases — it acquires a shared lock and copies pages atomically without blocking reads or writes. File copy risks corruption if writes occur mid-copy. SQL dump requires parsing and is slower.
- **Consequence**: Backup works while the pipeline is running. Restore requires explicit `confirm=True` parameter to prevent accidental overwrites. Retention-based pruning removes oldest backups automatically.

## Job orchestration runs via one-shot CLI invocations

- **Decision**: Each job (morning-run, pregame-run, backup, etc.) runs as a separate CLI invocation via `python -m src.production_jobs <job-type>`, not as a long-running process.
- **Reason**: One-shot invocations work with any scheduler (cron, Task Scheduler, cloud), are easier to debug, and naturally isolate failures. A crashed morning-run doesn't prevent the pregame-run from starting.
- **Consequence**: Each invocation loads config fresh, creates its own DB connection, and logs independently. Job runs are persisted to `job_runs` table for audit trail.

## Shadow mode is the default for production delivery

- **Decision**: Shadow mode (`SHADOW_MODE=true`) is the default state. Public/VIP Discord delivery requires explicit operator action to enable.
- **Reason**: Accidental delivery of未经验证的 recommendations to users is the highest-risk failure mode. Defaulting to blocked delivery ensures operators must consciously opt in.
- **Consequence**: First-time setup requires: readiness check → manual checklist → promotion criteria → explicit enable command with confirmation phrase.

## Promotion criteria never auto-disable shadow mode

- **Decision**: The promotion criteria evaluator only reports pass/fail status. It never automatically disables shadow mode.
- **Reason**: Automatic promotion removes the human judgment step. An operator must review all criteria and explicitly decide to go live.
- **Consequence**: Even when all 7 criteria are met, an operator must run `delivery_gate enable --confirm "ENABLE LIVE DELIVERY"` to activate live delivery.

## Delivery gate requires 6 independent checks

- **Decision**: Live delivery requires ALL of: shadow mode off, acknowledgement given, recent passing readiness, no critical health, no critical data-quality, valid config.
- **Reason**: No single check is sufficient. A system could pass readiness but have a critical data-quality finding. Multiple independent gates provide defense in depth.
- **Consequence**: Each gate is checked independently and reported separately. One failure blocks all delivery.

## API usage is tracked per-request with quota monitoring

- **Decision**: Every API interaction is persisted with endpoint, job type, cache hit, HTTP status, response time, retry count, and estimated quota usage.
- **Reason**: API quotas are finite and costly. Without per-request tracking, operators cannot diagnose quota exhaustion or optimize cache hit rates.
- **Consequence**: `api_usage` table grows over time. Quota warnings trigger at configurable thresholds (default 80%).

## Data-quality critical findings block recommendation delivery

- **Decision**: Any CRITICAL data-quality finding in the last 24 hours blocks all public/VIP delivery.
- **Reason**: Critical findings indicate systematic data corruption (impossible prices, future timestamps, >50% sportsbook loss). Delivering recommendations based on corrupted data would be worse than delivering nothing.
- **Consequence**: Operators must investigate and resolve critical findings before delivery resumes. Warning-level findings do not block delivery.

## Recommendation trace links API request through settlement

- **Decision**: Every live recommendation gets a trace record linking: API request → ingestion → scan → recommendation → confidence → delivery decision → delivery attempt → closing price → settlement.
- **Reason**: When a recommendation is questioned, operators need to trace the full lifecycle to identify where an error occurred. Without traceability, debugging production issues is guesswork.
- **Consequence**: `recommendation_traces` table grows with each recommendation. Trace data is queryable by recommendation ID.

## Control panel uses subprocess for pipeline execution

- **Decision**: The Streamlit control panel runs pipeline commands via `subprocess.Popen` in a background thread, not by importing and calling Python functions directly.
- **Reason**: Streamlit runs in its own event loop. Calling pipeline functions directly would block the UI thread and prevent progress updates. Subprocess execution provides natural process isolation and allows the UI to remain responsive while the pipeline runs.
- **Consequence**: Pipeline logs are captured from subprocess stdout/stderr and displayed in the UI. Exit codes are available for status determination. The subprocess inherits the parent's environment (including .env variables).

## 3-tier recommendation classification

- **Decision**: Recommendations are classified into OFFICIAL_TRACKED (strict gates: score>=7.0, 4+ books, EV>=3%, QUALIFIED status), DISCOVERY_TRACKED (relaxed gates: score>=6.0, 3+ books, private research only), or RESEARCH_ONLY (everything else).
- **Reason**: A binary official/research split was too coarse. Many high-quality recs fall just below official thresholds but have research value. A middle tier captures these without polluting the official record.
- **Consequence**: Official picks are graded and tracked. Discovery picks are stored separately for private research. Research picks are for threshold calibration only.

## Market Quality Score is a composite of 6 weighted components

- **Decision**: MQS is computed from book_count (0.30), two_sided (0.20), freshness (0.15), mapping_confidence (0.10), price_consistency (0.15), sportsbook_diversity (0.10), yielding a 0-10 score.
- **Reason**: No single metric captures market quality. Book count alone ignores freshness or diversity. A composite score provides a single rankable metric while preserving component-level diagnostics.
- **Consequence**: MQS is computed during pipeline freeze and stored per recommendation. Dashboard shows MQS in Market Intelligence tab with per-market averages.

## _load_recs uses try/except fallback for column resilience

- **Decision**: The dashboard's `_load_recs()` function tries an explicit column list first, then falls back to `SELECT *` on OperationalError.
- **Reason**: Test databases and older databases may lack newer columns added by recent migrations. The explicit column list is preferred for production stability, but the fallback ensures the dashboard works against any schema version.
- **Consequence**: The dashboard gracefully handles databases with missing columns. New columns appear as soon as the schema migration runs.
