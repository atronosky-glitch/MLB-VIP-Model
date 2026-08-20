# CHANGELOG

Dated, narrative record of notable engineering sessions. `PROJECT_STATUS.md`
is the authoritative current-state snapshot; this file is the story of how
it got there. Newest entries first.

## 2026-08-20 (latest) — WNBA player props + identity; shared game settlement; line-movement-aware CLV; confidence scoring wired in; multi-sport pick lifecycle website

### Why

Operator's 5-priority mandate: player props need a trustworthy identity
system before publishing them; settlement needs to happen automatically
and use the exact stored recommendation, never a reconstruction; the
website needs a complete Upcoming/Past/Performance pick lifecycle; CLV
needs to account for line movement, not just price; and the DB needs to
already be collecting everything a future model would need to reconstruct
what the system knew at recommendation time.

### What changed

- **Player identity** (`src/player_identity.py`, new): team-scoped 3-tier
  matching (HIGH/MEDIUM/LOW/UNRESOLVED) against ESPN's free roster API.
  LOW/UNRESOLVED never reach a recommendation. Enabled exactly the 8 WNBA
  player-prop markets proven live (points/rebounds/assists/threes/PRA/
  pts+reb/pts+ast/reb+ast) — not guessed.
- **Settlement** (`src/game_settlement.py`, new; `src/wnba_results.py`,
  new): sport-agnostic moneyline/spread/total grading shared by MLB/NFL/
  WNBA off each recommendation's own stored side/line/raw_line. New
  `NEEDS_REVIEW` status. Postponed/cancelled/suspended games now persist
  as VOID instead of staying stuck forever; player props for a voided
  game settle immediately via a void-shortcut instead of waiting on an
  impossible stat fact.
- **CLV / line movement** (`src/grading.py`): `classify_line_movement()`
  reports favorable/unfavorable direction from each market's real win
  condition when the line moved and price CLV can't be computed at the
  same line; `calculate_clv()` tries an exact-line closing-price lookup
  first (restricted to the same capture batch, so it can't match the
  bet's own stale original quote). Added `pct_beating_close` to
  `performance_summary()`.
- **Confidence scoring**: `compute_confidence()` existed but was dead
  code — now actually wired into `daily_pipeline._stage_freeze`, with
  `mapping_confidence` flowing from the identity resolver into the score.
- **Website** (`src/customer_view.py`): multi-sport (MLB/NFL/WNBA),
  filter bars on Upcoming/Past Picks (default no-op — never hides losses
  by default), fair odds/confidence/market quality/closing-line/CLV
  fields, and a real Performance Dashboard with breakdowns by sport,
  market, sportsbook, confidence grade, and EV bucket.

Full suite: **1643 passed, 0 failed** (was 1568). Verified: two real bugs
self-caught before any test ran against them (per-side raw_line sign,
stale exact-line-match false positive) — see `docs/SESSION_HANDOFF.md` for
the full account.

## 2026-08-19 — WNBA game-market odds: real, live, working

### Why

Following the cost-research entry below, the operator provided a free
The Odds API key and asked for it to be added to `.env` and used for live
WNBA testing — the natural next step once the free-tier math looked
promising. Added to `.env` as `THE_ODDS_API_KEY` (gitignored; never
logged, printed, or committed — verified via masked-only diagnostics
throughout, same discipline as `SPORTSODDS_API_KEY`).

### What was found, live

- `GET /v4/sports` confirms `basketball_wnba` is active on the free key.
- `GET /v4/sports/basketball_wnba/odds` (regions=us, markets=h2h,spreads,totals):
  5 real games, 9 bookmakers on the sample game (fanduel, draftkings,
  betmgm, bovada, betrivers, betus, betonlineag, mybookieag, lowvig) —
  comparable book depth to MLB's SportsGameOdds feed. Cost: exactly 3
  credits (confirmed the `markets × regions` formula from the earlier
  research entry). 500 free credits/month covers a daily game-markets
  scan comfortably (~90/month).
- `GET /v4/sports/basketball_wnba/events` — confirmed free (0 credits).
- `GET /v4/sports/basketball_wnba/events/{id}/odds` with
  `markets=player_points,player_rebounds,player_assists,player_threes`:
  real player props, 3-4 books, real players (Shakira Austin, Georgia
  Amoore) and lines. **This resolves the open question from the prior
  research entry — player props ARE available on the free tier.**
  Cost: 4 credits per event; scanning props for every game on a 5-game
  day would be ~20 credits/day (~690/month) — enough to blow through the
  free 500/month budget for a *sustained daily* game+props cadence, even
  though a single verification call was cheap. Game markets alone stay
  well within budget.
- Bookmaker/market objects carry `last_update` (ISO timestamp) at both
  levels — usable the same way `lastUpdatedAt` is used for freshness on
  the SportsGameOdds side.

### What was built

The Odds API's wire format has no oddID grammar at all — nested
`bookmakers[].markets[].outcomes[]` objects, team names as plain strings,
no stable player ID for props (only free-text names in
`outcome.description`). This ruled out extending `src/sports/base.py`'s
`MarketConfig`/`match_ou_market` matching (built entirely around
SportsGameOdds's composed oddID strings) — WNBA needed its own ingestion
path that produces the *same generic odds-row schema* everything
downstream already consumes, not a reuse of the oddID matcher.

- **`src/odds_api_client.py`** — a second, independent HTTP client
  (caching, retry, same shape as `SportsGameOddsClient`). Unlike
  `SPORTSODDS_API_KEY`, `THE_ODDS_API_KEY` is optional at import time —
  MLB/NFL must keep working with no WNBA key configured at all; the key
  is only required the moment a WNBA fetch is actually attempted
  (`OddsAPIKeyError`, mirroring `APIKeyError`'s fail-fast clarity).
- **`src/wnba_odds_parser.py`** — parses `h2h`/`spreads`/`totals` into
  rows with the exact same fields `player_prop_parser.py` produces
  (`event_id`/`sportsbook`/`player_id`/`player_name`/`team_id`/
  `team_name`/`market_type`/`market_group_key`/`side`/`line`/`price`/
  `decimal_odds`/`validation_status`/...), reusing
  `player_prop_parser._build_game_group_key`/`_SIDE_MAP` directly for
  identical grouping semantics. Team-name-to-side resolution is exact-match
  only (never fuzzy) — an unrecognized name is excluded, not guessed.
  Player props intentionally not parsed yet (see Deferred below).
- **`src/sports/wnba.py`** — `AVAILABLE = True`, a real 3-market registry
  (moneyline/spread/total; the `odd_id_stat_prefix`/`bet_type` fields on
  these `MarketConfig` entries are vestigial for this provider, documented
  as such — matching only happens via `market_type` string equality here,
  never `match_ou_market`), `ODDS_PROVIDER = "the_odds_api"`, and a new
  `fetch_and_parse(event_id=None)` entry point returning
  `(odds_rows, audit_rows, normalized_events, from_cache)` — the
  normalized events are shaped so `player_prop_scanner._build_event_map`
  (which already has SportsGameOdds/fallback field lookups) accepts them
  with zero changes needed there.
- **Pluggable-provider dispatch**: `player_prop_scanner.run_scan()` now
  checks each league's `ODDS_PROVIDER` (default `"sportsgameodds"`) and
  branches to `league_mod.fetch_and_parse()` for a league that declares a
  different one, skipping the SportsGameOdds-specific fetch/parse path
  entirely. `daily_pipeline._stage_fetch_events` does the same check and
  cleanly no-ops (not fails) for a non-SportsGameOdds league — its
  raw-ingest stage doesn't apply there; the real fetch happens inside the
  scan stage. `_stage_ingest` already no-op'd on an empty event list, so
  no change was needed there.

### Verification

Ran the full pipeline against real live data (not just synthetic
fixtures) before writing a single test: `run_scan(league="WNBA", ...)`
against the real cached response produced **5 events → 210 approved odds
rows → 31 O/U groups → 25 ranked opportunities with real EV values**,
real team names (Connecticut Sun @ Las Vegas Aces, Toronto Tempo @
Washington Mystics, etc.) flowing correctly through the *completely
unmodified* LOO consensus / EV / market-quality / Pinnacle-fallback /
qualification pipeline.

16 new deterministic tests in `tests/test_wnba_odds.py` (parser
correctness including exact-match team resolution and abs-valued spread
pairing, missing-key handling, the exact credit-formula params sent to
the API, `fetch_and_parse`'s event normalization, and a full
`run_scan(league="WNBA")` integration test) — all synthetic fixtures
shaped from the real verified schema above, zero live network calls in
the suite (same discipline as every other test here). 5 pre-existing
tests from the prior session that asserted WNBA was *unavailable*
(correct at the time) were updated to assert it's available now, or
rewritten to simulate an unavailable league via monkeypatch so the
rejection code path itself still has coverage even though no real league
currently exercises it. One test bug found along the way: `load_dotenv()`
re-populating a `monkeypatch.delenv`'d var on first import within a test
— fixed by importing the module at collection time instead.

Full suite: **1568 passed, 0 failed** (was 1552).

### Deliberately deferred

- **WNBA player props**: real, live, confirmed working — not registered
  because The Odds API gives no stable player ID, only a free-text name.
  Needs its own identity-resolution design (name normalization against a
  roster, with the same "ambiguous means excluded, never inferred"
  discipline this project uses everywhere else) before it can be trusted.
- **`src/wnba_results.py`** (settlement): ESPN's free public WNBA API
  verified live and working for scores/boxscores, same pattern as MLB and
  NFL — but not built. Game-level auto-settlement (moneyline/spread/total
  via score comparison) isn't wired for *any* league yet
  (`automatic_grading.py` only grades `player_stat_results`), so this is
  parity with MLB/NFL's existing gap, not a WNBA-specific shortfall.
- **The $30/month Odds API tier**: not needed yet. Game markets alone fit
  the free tier for a daily cadence; only needed once player props are
  actually wired in at production scanning frequency.
- **WNBA production scheduling**: no cron/schedule wired into
  `render.yaml`/`src/scheduler.py`, matching NFL's same deferred state.

## 2026-08-19 (later) — Data provider cost research; production_canary.py multi-league

### Data provider / cost policy research (WNBA data access)

Operator set a standing cost policy: prefer free/low-cost, but data
quality matters more than avoiding every expense; never auto-subscribe to
a paid provider — present options with full details and let the operator
decide. Applied it to the open WNBA data-access question:

- Re-confirmed via SportsGameOdds's own pricing page (not just our
  account): **no tier — free, $99/mo Rookie, $299/mo Pro, or custom
  All-Star — includes WNBA.** Upgrading our existing provider does not
  solve this.
- Confirmed ESPN's free public API covers WNBA scoreboard/summary/boxscore
  (`site.api.espn.com/apis/site/v2/sports/basketball/wnba/...`), live
  request succeeded. Settlement/results for WNBA is a solved, zero-cost
  problem, same as MLB (StatsAPI) and NFL (ESPN NFL).
- Researched The Odds API (the-odds-api.com) as a real option for WNBA
  **odds**. Their credit formula (`markets × regions` per live request)
  means our scan cadence would likely cost roughly 90-180 credits/month
  for game-level markets (moneyline/spread/total) — well inside their free
  500-credits/month tier. Player-props tier requirements and exact update
  frequency were not resolved from public docs alone (conflicting
  marketing copy) — flagged as needing hands-on verification with a free
  API key, not asserted.
- Presented the paid-tier comparison (provider/data/leagues/books/markets/
  props/frequency/history/limits/cost/trial/integration-difficulty/why-free-
  is-insufficient, per the operator's requested format) for the $30/mo tier
  in case props need it. **Nothing was subscribed to.**
- Historical odds is paid-only on every external provider checked, but
  this project doesn't need to buy it: the pipeline already builds its own
  historical archive going forward the moment a league starts being
  scanned (`closing_prices`, `recommendation_lifecycle_events`, etc.). A
  paid historical archive would only matter for backtesting against a
  season we didn't capture ourselves — not a launch blocker.

### `production_canary.py` made multi-league

Follow-up to the multi-league architecture work earlier the same day —
`production_canary.py` still had `league="MLB"` hardcoded in
`_fetch_canary_sample` and `_validate_market_mappings` (both flagged as
"deliberately deferred" in the earlier entry below). Now:

- `run_canary(..., league="MLB")` + `--league` CLI flag, with the same
  unknown-league / unavailable-league rejection pattern used in
  `daily_pipeline.py` and `player_prop_scanner.py`.
- `_validate_market_mappings` resolves the correct registry via
  `src.sports.get_league(league).get_market_registry()` instead of
  hardcoding MLB's.
- `live_readiness.py` was reviewed but left as-is — its checks (API key
  format, DB, disk, timezone) are infrastructure-level, not league-specific
  market data, so there was nothing to thread a league through.
- 3 new tests in `tests/test_phase11_readiness.py`, including one proving
  a real NFL oddID matches NFL's registry but not MLB's (catches the
  hardcoding bug pattern by construction, not just by inspection).

Full suite: **1552 passed, 0 failed** (was 1549).

## 2026-08-19 — Multi-league architecture; NFL added; WNBA blocked (data access)

### Why

The project began as an MLB-only sportsbook market-analysis platform.
Operator directive: evolve it into a reusable multi-sport platform (MLB,
NFL, WNBA now; NBA/NHL/NCAAF/NCAAB/soccer addable later) while preserving
all existing MLB behavior and tests, without faking data-provider coverage
for markets or leagues that aren't actually verified live.

### Audit findings (before any code changed)

Read through the full pipeline (`odds_parser.py`, `player_prop_parser.py`,
`player_prop_analysis.py`, `market_analysis.py`, `official_picks.py`,
`grading.py`) and found the core engine was **already sport-agnostic** —
no MLB-specific logic in odds parsing, EV math, market-quality scoring, or
pick qualification. The actual MLB-only surface area was narrow:

- `prop_config.MARKET_REGISTRY` — MLB's list of player-prop market patterns.
- `mlb_results.py` — MLB StatsAPI settlement, MLB-specific end to end.
- Hardcoded `league="MLB"` in `daily_pipeline.py`, `player_prop_scanner.py`.
- No `league`/`sport` column anywhere except `games.league` (unused
  elsewhere — every other table implicitly assumed MLB).
- `production_canary.py`/`live_readiness.py`/dashboard/customer_view still
  MLB-labeled (not touched this session — see Deferred below).

This materially changed the plan: rather than a ground-up rewrite, the
work was "add a thin per-league adapter layer, thread a `league` parameter
through the handful of genuinely MLB-coupled call sites, add schema
columns" — not a redesign of the analysis engine.

### Live verification before writing any NFL code

Per the project's own non-negotiable rule ("never guess API fields"),
fetched real data before designing anything:

- `GET /events?leagueID=NFL` — confirmed identical event/odds schema to
  MLB (`eventID`/`teams`/`odds`/`byBookmaker`, same oddID grammar
  `{statID}-{entityID}-{periodID}-{betTypeID}-{sideID}`).
- `GET /markets?leagueID=NFL` — full market catalog (339 rows / 171 market
  groups) with real `activeEvents` and per-bookmaker support, used to pick
  markets with genuine live liquidity rather than the theoretical maximum
  (mirroring MLB's own Phase 17C rationalization).
- `GET /leagues` and `GET /events?leagueID=WNBA` — **WNBA is not available
  on the current SportsGameOdds account** (absent from `/leagues`; direct
  event fetch returns HTTP 400). Confirmed twice. This is a real
  data-provider/plan gap, not a bug — flagged to the operator and left
  genuinely unimplemented (empty registry, `AVAILABLE = False`, explicit
  reason) rather than guessed.
- ESPN's public NFL scoreboard/summary API (`site.api.espn.com`, free,
  keyless) verified live against a real completed game (event 401873272,
  CIN 16–DET 14) for settlement — same free/public-API pattern already
  used for MLB via MLB StatsAPI.

### What was built

**`src/sports/` — the league adapter package**
- `base.py`: `MarketConfig` (moved here from `prop_config.py`, re-exported
  for backward compatibility) and registry-parameterized `match_ou_market`/
  `match_yn_market`/`build_lookup_maps` — fully sport-agnostic.
- `__init__.py`: `get_league(league)`, `supported_leagues()`,
  `available_leagues()`, `market_capability_report()`. Lazy submodule
  imports (avoids a circular import: `prop_config.py` imports
  `sports.base`, which would otherwise trigger `sports/__init__.py`
  eagerly importing `sports.mlb`, which imports `prop_config.py`).
- `mlb.py`: thin wrapper around the existing `prop_config.MARKET_REGISTRY`
  and `mlb_results` — zero behavior change.
- `nfl.py`: 11-market registry (3 game markets + 8 player props) built
  from the verified live catalog above, with a documented
  `PARTIALLY_SUPPORTED_MARKETS` list for catalog entries that exist but
  don't yet have enough live liquidity to trust.
- `wnba.py`: `AVAILABLE = False`, empty registry, dated reason string —
  intentionally inert rather than faked.

**`src/nfl_results.py`** — ESPN-based NFL settlement adapter, mirroring
`mlb_results.py`'s interface (`ingest_results_for_recommendations(conn,
recommendations, client=None)`). Settles passing/rushing/receiving
yards+touchdowns+interceptions, receptions, field goals made, and anytime
touchdown (summed across rushing/receiving/kick-return/punt-return TD
columns). Same conservative identity-matching discipline as MLB: exact
team-pair match, exactly one exact player-name match within the game's
boxscore, or unresolved (never inferred).

**Threaded `league`/`registry` through the pipeline** (every new parameter
defaults to MLB, so no existing call site's behavior changed):
- `player_prop_parser.parse_player_props(event, registry=None)`.
- `player_prop_scanner.resolve_markets/_group_side/run_scan(..., league="MLB")`
  — also fixed a real bug found in the process: the O/U-vs-YN grouping
  check (`cfg.get_market_by_yn_type`/`get_market_by_ou_type`) was hardcoded
  to MLB's registry, so a non-MLB market_type would silently match neither
  branch and never become an opportunity, even after parsing correctly.
  Caught by writing an NFL end-to-end scan test (see Testing below) before
  trusting the feature.
- `daily_pipeline.PipelineConfig.league` (+ `--league` CLI flag),
  `PipelineState.sport` (derived via the league adapter).
- `src/worker.py::_run_catchup_grading` now groups unresolved
  recommendations by their `league` column and dispatches each group to
  its own settlement module via `get_league(league).get_settlement_module()`
  — a league with none (WNBA) is skipped cleanly, not guessed.
  `_run_morning_scan`/`_run_pregame_scan` accept a `league` parameter
  (default MLB, matching current one-service-per-league production
  deployment; a future multi-league scheduler is a cadence-policy decision
  left for when a second league actually goes to production — NFL runs
  weekly, not daily).

**Player-name resolution improvement (found while wiring NFL, benefits
every league including MLB):** `player_prop_parser.py` previously
resolved player names from `odd_data.playerNames.full/short`, falling back
to a hand-maintained, MLB-specific suffix-stripping heuristic on
`marketName` text. Verified NFL odd rows have `playerNames = None`, so
that fallback would have been load-bearing for NFL. Instead, added
`_resolve_player_name()`, which tries the event-level `event.players
[playerID].name` field first — a structured, sport-agnostic identity field
verified present on both MLB and NFL live events, and more reliable than
either previous path. The MLB-specific suffix list remains as the final
fallback, documented as intentionally not extended per league.

**Database migration** (`database/db_manager.py::init_db`, additive,
idempotent, `_add_columns_if_missing` pattern already used throughout this
project): `league TEXT DEFAULT 'MLB'` added to `odds`, `player_prop_odds`,
`official_picks`, `event_results`, `player_stat_results`,
`market_settlements`, `closing_prices`, `recommendation_lifecycle_events`;
`league`/`sport` (default `'MLB'`/`'baseball'`) added to
`historical_recommendations` and `official_picks`. `save_recommendation()`
and `freeze_official_pick()` updated to actually persist an explicit
league/sport (the latter via a `COALESCE` subquery against the source
recommendation, falling back to MLB/baseball if the source row isn't
visible yet — preserves the pre-existing "insert doesn't require the
recommendation row to exist first" behavior some callers rely on).

**Dashboard (`src/control_panel.py`)**: the main picks query now selects
`league`/`sport`; the "RUN" control gained a league selector (only shown
when more than one league is available) and threads the choice through to
`python -m src.daily_pipeline --league <LEAGUE>`. This is the operator
control surface, not a customer-facing redesign — see Deferred below.

### Testing

- Full suite still green throughout, checked after every meaningful edit:
  **1549 passed, 0 failed** (was 1503 before this session's earlier
  debt-fixing work; net +46 tests today across the additions below, zero
  regressions).
- `tests/test_sports_adapters.py` (26 tests): the `sports/` package
  contract, generic `base.py` matching against an arbitrary synthetic
  registry, the NFL registry against real verified oddIDs, player-name
  resolution via `event.players`, and a full `run_scan(league="NFL", ...)`
  end-to-end test with a mocked API client — this is the test that caught
  the O/U-vs-YN grouping bug above.
- `tests/test_nfl_results.py` (13 tests): settlement extraction against
  synthetic-but-schema-verified ESPN fixtures, including the multi-category
  anytime-touchdown sum, ambiguous-name and not-final unresolved paths, and
  full `ingest_results_for_recommendations` persistence.
- `tests/test_multi_league_migration.py` (6 tests): migration is
  idempotent and preserves existing rows on a database that predates the
  league/sport columns (via `ALTER TABLE ... DROP COLUMN` to build a
  realistic pre-migration snapshot rather than hand-duplicating the whole
  schema); `save_recommendation`/`freeze_official_pick` actually persist an
  explicit league instead of silently defaulting.
- `tests/test_automation_fixes.py`: new
  `TestCatchupGradingLeagueDispatch` proving unresolved MLB/NFL
  recommendations route to their own settlement module and WNBA is skipped
  cleanly.
- Two pre-existing hand-rolled schema fixtures (`tests/conftest.py::db_conn`,
  `tests/test_phase6_grading.py::db`) duplicate the real schema rather than
  calling `init_db()`, and needed the same `league`/`sport` columns added
  by hand to stay in sync — a known fragility of that pattern, not
  introduced this session.

### Deliberately deferred (scoped out, not forgotten)

- **WNBA markets/settlement**: genuinely blocked on data access (see
  above). `src/sports/wnba.py` is ready to receive a real registry the
  moment access exists — operator decision needed (SportsGameOdds plan
  upgrade, or a second provider).
- **Customer-facing multi-league UI** (`src/customer_view.py`): not
  touched. The data layer underneath it is fully multi-league now; the
  customer-facing display logic itself is still MLB-labeled. Left for a
  follow-up focused specifically on customer-facing UI, since it's
  higher-risk to change without the ability to visually test a Streamlit
  app in this environment.
- **Full admin dashboard redesign** (`src/control_panel.py`): only the
  minimum needed for operator control was added (league column in the
  picks query, a league selector on the run button). Tabs like Market
  Intelligence, Performance, and Line Movement still implicitly assume
  MLB in places and would benefit from a dedicated pass.
- **`production_canary.py`/`live_readiness.py`**: still MLB-only
  (`get_events(league="MLB", ...)`), from the prior debt-fixing session.
  Not extended to multi-league this session.
- **Pinnacle sharp-reference feed for NFL**: `pinnacle_feed.py` is
  hardcoded to baseball's Pinnapi sport ID. NFL currently runs on LOO
  market-median consensus only (the same fallback path MLB itself uses
  whenever Pinnacle is absent) — a legitimate, already-proven-safe mode,
  not a broken state, but Pinnacle coverage for NFL is unverified.
- **Worker scheduling cadence for NFL**: the worker's job functions accept
  a `league` parameter now, but there is no NFL-specific cron/schedule
  wired into `render.yaml` or `src/scheduler.py` yet — NFL's weekly cadence
  is a different scheduling shape than MLB's daily one and deserves its own
  design pass when NFL actually goes to production.
