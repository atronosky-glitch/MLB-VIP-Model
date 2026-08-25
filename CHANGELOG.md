# CHANGELOG

Dated, narrative record of notable engineering sessions. `PROJECT_STATUS.md`
is the authoritative current-state snapshot; this file is the story of how
it got there. Newest entries first.

## 2026-08-25 — 3-day production save-failure outage fixed; NFL schedule-discovery fallback added

### Why

Operator asked for a full production audit first ("did today's run find few edges, or did it find good bets our gates rejected?"), which surfaced that zero recommendations had saved anywhere since 2026-08-22. Operator then directed a prioritized fix: never let a save failure be silently indistinguishable from an intentional skip, find the real root cause (not just add logging), fix it, prove the fix with real production data, then move to NFL's scheduling failure and Pinnacle production auth — explicitly without touching any EV/model-score threshold or qualification rule.

### What was found and fixed

1. **The save-failure root cause**: `database/db_manager.py::save_recommendation()`'s bare `except Exception: conn.rollback(); return None` was masking a 100%-reproducible `psycopg2.errors.DatatypeMismatch`. `_persist_recommendation_evidence()` bound raw Python `bool`s for `pinnacle_found`/`pinnacle_reference_used`/`pinnacle_approved` against `integer`-typed PostgreSQL columns — SQLite accepts this silently; PostgreSQL does not. `tests/test_phase17b_postgres.py`'s own docstring notes CI only runs against SQLite, so this was structurally invisible to the test suite. Reproduced live against the real production schema (via explicitly-rolled-back transactions with `conn.commit` monkey-patched to a no-op) before touching any fix code, per the operator's explicit "find the actual root cause" instruction. Fixed with a new `_bool_to_int_or_none()` helper.
2. **Duplicate-vs-error ambiguity**: split the old single-return `save_recommendation()` into `save_recommendation_result()`, returning a `SaveRecommendationResult` with `.status` ∈ `saved`/`duplicate`/`error` (plus `.error_type`/`.error_message`), and kept `save_recommendation()` as a thin backward-compatible wrapper. `daily_pipeline.py::_stage_freeze` now tracks `saved`/`duplicates`/`save_errors` as three separate real counters (previously `duplicates` was inferred by subtraction) and surfaces save errors into `state.errors` with the failing recommendation's identifiers, never secrets.
3. **NFL scheduling silence**: `_discover_nfl_game_times()` caught every exception — including a real, currently-ongoing SportsGameOdds 429 — and returned `[]`, and `_check_and_schedule_nfl()` returns immediately on an empty list, so no NFL job had been queued since 2026-08-22. Added a fallback specifically on a real 429 (mirroring `daily_pipeline.py`'s existing `except requests.exceptions.HTTPError` + `status == 429` pattern) to The Odds API's free `/events` endpoint, reusing `src/sports/nfl.py`'s existing `ODDS_API_SPORT_KEY` — the same already-integrated provider the game-odds 429 fallback already uses, not a new architecture. Any other failure still degrades to `[]` exactly as before.

### Verification

Reproduced the exact `DatatypeMismatch` live against production before fixing it (`git stash` on the fix, confirmed the new regression test fails with the raw psycopg2 error; `git stash pop`, confirmed it passes). Live-verified the NFL fallback the same way: real SGO 429 in production right now, fallback correctly recovers 272 real NFL game times via The Odds API. After deploying, waited for and inspected a real post-deploy scheduled scan (not a manual trigger): 28 events, 1152 markets, 25 opportunities → 15 newly-saved recommendations (10 `OFFICIAL_TRACKED`, 5 `DISCOVERY_TRACKED`) → 3 brand-new `official_picks` rows, the first since 2026-08-21 — confirming the full save → qualify → official-pick chain end-to-end. Confirmed zero duplicate fingerprints anywhere in `historical_recommendations`. Pinnacle auth in production is still failing (`pinnacle_found=0` across the board) — separate from this fix, root cause blocked on the operator checking Render's actual configured key value.

### A mistake made and disclosed in the same session

An earlier reproduction script (`repro_save_bug2.py`) called the real `save_recommendation()` against production wrapped in `raw_conn.rollback()` for safety, but `save_recommendation()` commits partway through its own body — the rollback ran after the commit had already landed, so 25 real test rows were written to production `historical_recommendations` (+50 `recommendation_lifecycle_events` rows), tagged with a `scan_run_id` that only ever existed in the local dev database. Confirmed isolated — zero rows reached `official_picks` or any other downstream table. Disclosed immediately on discovery; left in place at the operator's instruction pending a later decision.

5 new regression tests for the NFL fallback (confirmed to fail on the pre-fix code), plus the save-failure regression tests (5 new tests, including a dialect-aware guard using a mocked `postgresql` connection that asserts no raw `bool` is ever bound as a parameter). Full suite: **1914 passed, 0 failed** (was 1909).

## 2026-08-23 (later same day) — Full EV-engine audit: 4 real bugs found and fixed

### Why

Before adding more features, the operator wanted a comprehensive,
evidence-based audit of the actual betting logic — not an assumption
that existing code (and passing tests) meant it was correct. Explicit
concerns: is Pinnacle actually used correctly, exactly how is fair
probability computed, and are qualification rules unnecessarily
filtering out legitimate +EV bets.

### What was audited (live, not from docs/memory)

Provider map traced through the real runtime call path (worker →
pipeline → parser → scanner → analysis → classification). Pinnacle
tested two ways with the real API keys: direct pinnapi.com (already
built earlier today) and, separately, The Odds API's own `bookmakers=
pinnacle` access — confirmed real but narrower (no MLB player props, no
alt-lines) than the direct integration, so both stay. Fair-value math
read function-by-function, including a direct proof that LOO consensus
correctly excludes the book being evaluated from its own fair price.
All 12 gates in `official_picks.py::classify_recommendation` traced
directly, not just the config constants.

### What was found and fixed

1. **Pinnacle spread sign collapse** — pinnapi genuinely offers both
   hdp directions (home-favored and away-favored) as distinct real
   alt-lines for the same game; `parse_game_odds()` stored `abs(hdp)`,
   so one direction silently overwrote the other. Reproduced live: a
   real group showed 87.6% "EV" from comparing two different real bets.
2. **The same bug, independently, in the PRIMARY SportsGameOdds path**
   — not limited to today's new Pinnacle code. A sportsbook disagreeing
   with the majority on which team is favored (real, live-observed:
   FanDuel had Atlanta Braves +1.5 while five other books had them
   -1.5) silently blended into the same group via `abs(line)`-only
   keys, producing 39-45% bogus EVs on a live scan. Fixed in both
   `player_prop_parser.py` (SGO path) and `odds_api_game_parser.py`
   (Odds-API fallback path) by canonicalizing the group key to the
   away team's own signed line.
3. **The single biggest finding**: `AUTO_SETTLEABLE_MARKET_TYPES` had
   zero entries for any game market (moneyline/spread/runline/total),
   so Gate 1 unconditionally blocked every game-market recommendation
   from Official status — regardless of EV, book count, or Pinnacle
   approval — across all 3 leagues. Separately, `game_settlement.py`'s
   own docstring claimed MLB run-line support that was never actually
   implemented (`GAME_MARKET_TYPES` only matched `game_spread_ou`,
   never MLB's own `game_runline_ou`). Both fixed.
4. **Game markets under-scored on confidence** — `model_scoring.py`
   treated a missing `confidence_score` as neutral-uncertain (0.5) for
   every market, but game markets never go through player-identity
   name-matching at all, so there's no real ambiguity to be uncertain
   about. Now scores full confidence for `player_id=="GAME"` recs.

Also confirmed, directly from code: the "Pinnacle missing → auto-reject"
behavior the operator worried about does **not** exist as feared — Gate
9 already has a working LOO-consensus fallback for markets Pinnacle
genuinely has no data for, built 2026-08-05, predating this session. It
was being masked by bug (3), which blocked game markets before this
gate mattered.

### Verified live, before/after each fix

Re-ran the real local MLB pipeline after each change. Before: EV values
up to 87.6%, a nonsensical `line=16.5` "run line," and 100% of
game-market recs blocked by a settlement-registry error regardless of
their real EV. After: realistic EV range (-0.5% to +3.1%), the
settlement-registry error gone entirely, and the remaining rejections
(Model Score <7.0, O/U EV <3%) are legitimate — a genuinely thin local
slate (SGO exhausted, Odds-API fallback only), not bugs.

21 new regression tests across `tests/test_pinnacle_feed.py`,
`tests/test_mlb_odds_parser.py`, `tests/test_game_market_grouping.py`
(new), `tests/test_game_settlement.py`, `tests/test_phase14_scoring.py`.
Full suite: **1867 passed, 0 failed** (was 1854). Full report:
`docs/EV_ENGINE_AUDIT.md`.

## 2026-08-23 — Pinnacle wired in for MLB, NFL, and WNBA; game markets added

### Why

Operator's standing goal: "I want all the picks that pass our guidelines
to show up to me and customers... i ideally want this model to make
official picks for all 3 leagues." Investigating why Official picks were
near-zero surfaced a real structural gap: `REQUIRE_PINNACLE_FOR_OFFICIAL`
already applied to every league, but `src/pinnacle_feed.py` was hardcoded
to MLB only — NFL and WNBA O/U picks could never pass Gate 9 no matter
how good the edge was. The operator personally verified Pinnacle carries
these markets and pushed back when told otherwise, which was the right
call — Pinnacle (via pinnapi.com) is a real, separate, working feed that
had simply never been extended past MLB in code.

### What changed

- `src/prop_config.py`: replaced the single-league `PINNACLE_FEED_SPORT_ID`/
  `PINNACLE_FEED_LEAGUE` constants with per-league maps —
  `PINNACLE_SPORT_ID_BY_LEAGUE`, `PINNACLE_LEAGUE_NAME_BY_LEAGUE`,
  `PINNACLE_PROP_UNITS_BY_LEAGUE`, `PINNACLE_PROP_SUFFIXES_BY_LEAGUE`,
  `PINNACLE_GAME_MARKET_TYPES_BY_LEAGUE` — found by live-probing pinnapi's
  undocumented `sport_id` scheme (MLB=6, NFL=5, WNBA=3).
- `src/pinnacle_feed.py`: generalized prop parsing/matching to any league
  via the new config maps; added full-game moneyline/spread/total support
  (`parse_game_odds`, `build_pinnacle_game_lookup`, `match_pinnacle_game`,
  `inject_pinnacle_game_reference`) — a market Pinnacle was never used for
  before, even for MLB. Game markets ride the existing `player_id=="GAME"`
  sentinel already shared by the SportsGameOdds and Odds-API game-parsers,
  so no new analysis engine was needed.
- `src/player_prop_scanner.py`: the Pinnacle injection block (already at
  the shared, per-league call site) now fetches and injects both props
  and game odds for every league Pinnacle covers.
- `src/mlb_props_parser.py`: swapped the Odds-API-sourced `batter_hits`
  market for `batter_home_runs` — Pinnacle never prices hits, so
  `batter_hits` had no path to Official regardless of its own book depth;
  `batter_home_runs` does.
- **Real bug caught before shipping**: fetching props and game odds as
  two separate calls per league would hit the shared 10-second
  module-level rate limiter back-to-back and silently drop the second
  fetch almost every time in real usage (both need the same raw
  `/prematch/fixtures` payload). Fixed by caching the raw payload once
  per league (`_get_raw_payload`) and parsing both from it.

### Verified live (2026-08-23, real pinnapi trial key)

MLB: 26 props, 227 game-market entries (16 games). NFL: 0 props (real —
no specials posted this far pre-season, confirmed via a full
`special_category` breakdown) / 368 game-market entries (16 games).
WNBA: 82 props / 60 game-market entries (4 games). Ran a real WNBA prop
(Alanna Smith, Points, 9.5) and a real WNBA game total through the actual
production matching/injection functions end-to-end — both landed real
`"pinnacle"` reference prices on both sides. Full test suite: 1,854
passed. Full market audit: `docs/MARKET_CAPABILITY.md` →
"Pinnacle sharp-reference feed."

## 2026-08-22 (verification pass) — Live end-to-end test caught a real unbounded-events bug in the new props fetch

### Why

Operator asked to actually verify the new MLB/NFL props build works,
not just trust the unit tests — the same "let's make sure" discipline
that's caught real bugs earlier in this session.

### What was found and fixed

Ran a real live `run_scan(league="MLB", fetch_props=True)`: worked
correctly end-to-end — real player identity resolution
(`ESPN_MLB_5123768` etc.), real market mapping, and 3 real ranked
opportunities in the new markets (e.g. "Blake Snell OVER 8.5
strikeouts"). Running the same thing for NFL, however, hung for
several minutes. Root cause: `fetch_player_props()` (the shared fetch
loop in `src/odds_api_props_fetch.py`) called `get_events()` with no
time filter at all — for a full-season sport like NFL that returns
**every** game currently listed (272 games, months out), and the loop
then worked through them one by one, each with its own credit-budget
check and (until the budget ran out) a real API call. This is the exact
same class of bug already found and fixed for the game-odds fetch
earlier the same day (`fetch_game_odds_via_odds_api`'s missing
`-6h/+42h` window) — missed here because the new props path was a
separate code path, and WNBA's own unbounded call happens to already be
near-term-only in practice, which is what let it slip through review
and the initial test suite.

Fixed by filtering discovered events to a `-6h/+42h` window before the
credit-checked fetch loop (applied to all three leagues for
consistency, though only NFL was actually affected in practice). An
explicit `event_id` request still always bypasses the filter, matching
the existing dedup-bypass behavior. Re-ran the NFL scan after the fix:
returned immediately, 0 events (correct — the real next NFL game is
2026-09-10, genuinely outside any near-term window right now, not a
bug). One WNBA test fixture had to be updated to include a realistic
`commence_time` since it had none before and was now (correctly) being
filtered out.

Side benefit: the full test suite dropped from 5-7 minutes back to its
normal ~35 seconds — the same unbounded-loop bug had been silently
inflating test runtime the whole time it was live, not just NFL's real
usage.

Full suite: **1833 passed, 0 failed**.

## 2026-08-22 (later) — MLB/NFL supplemental player props via The Odds API

### Why

Operator noticed the Multi-League Health tab showing only ~337 credits/month
projected against the new 20,000/mo budget — most of the paid-for
capacity was going unused. After widening WNBA's own props cadence
(previous entry below) still left huge headroom, operator asked what it
would cost to give MLB and NFL real player props from the same
provider, not just the existing 429-only game-markets fallback. Verified
live before building anything: The Odds API genuinely carries MLB and
NFL player props on this account (not assumed) — 13 of 14 candidate MLB
markets returned real bookmaker data against an upcoming game, and 6 of
10 NFL candidates did against the earliest available NFL event (19 days
out, so thin but real).

### What changed

- Extracted the row-building/identity-resolution logic that was already
  proven for WNBA into shared `src/odds_api_props_parser.py` (mirrors
  the game-odds extraction from earlier the same day) and the
  discover/dedup/per-event-credit-checked fetch loop into shared
  `src/odds_api_props_fetch.py`. `wnba_odds_parser.py::parse_wnba_player_props`
  and `sports/wnba.py::fetch_and_parse_props` are now thin wrappers over
  these — a real regression-tested refactor, not just new code sitting
  next to the old.
- Generalized `src/player_identity.py`'s ESPN roster client to MLB/NFL:
  added their sport paths, and fixed a real shape difference found live
  — WNBA's roster response is a flat athlete list, MLB/NFL's is grouped
  by position (`{"position": "Pitchers", "items": [...]}`) — the
  original code would have silently returned zero players for both
  leagues without this fix.
- `src/mlb_props_parser.py` / `src/nfl_props_parser.py` (new): register
  only the markets with real observed liquidity from the live check —
  MLB: batter hits/total bases, pitcher strikeouts/outs (4-6 books each);
  NFL: pass yards/rush yards/receptions/reception yards (genuinely
  two-sided; `player_anytime_td` was present but single-sided "Yes"
  pricing on this provider, not Over/Under, so deliberately excluded).
  Every market reuses each league's existing primary-registry
  `market_type` string, so the existing settlement contract
  (`mlb_results.py`/`nfl_results.py`) applies automatically — confirmed
  all 8 markets already have one, zero new settlement code needed.
- `fetch_player_props_via_odds_api()` added to `src/sports/mlb.py` /
  `src/sports/nfl.py`. Unlike the game-markets fallback, this is **not**
  a 429-triggered fallback — it's a genuine second, independent data
  source that runs on its own schedule regardless of SportsGameOdds's
  health. Wired into `player_prop_scanner.run_scan()`'s SportsGameOdds
  branch (MLB/NFL's primary path) as a new merge step, distinct from the
  existing WNBA-style merge in the non-SportsGameOdds branch.
- New, deliberately narrower cadence than WNBA's: MLB games are ~12x
  WNBA's daily volume, so the same per-game pace would have cost
  roughly 12x as much in aggregate (~34,000/mo estimated — more than the
  entire monthly budget from MLB alone). Settled on MLB: 3h window/60min
  throttle, NFL: 4h window/60min throttle (vs WNBA's 6h/30min) —
  refactored `wnba_should_fetch_props` into a shared
  `_should_fetch_player_props` in `src/league_schedule.py` with
  per-league window/throttle constants rather than duplicating the
  function three times. MLB's own game times come from the local
  `games` table (already populated by the daily SportsGameOdds morning
  run) instead of a new live discovery call, so this costs nothing
  against either provider's quota; NFL reuses the kickoff times its
  existing scheduling check already discovers.
- New `mlb-props-scan` / `nfl-props-scan` job types wired into
  `worker.py`'s dispatch table and both the persistent-loop and one-shot
  scheduling paths. Documented, not hidden: because MLB/NFL's primary
  provider IS SportsGameOdds, running this job also re-runs that
  league's SportsGameOdds fetch as a side effect (fetch_props=True only
  adds the merge, it doesn't replace the primary path) — usually
  absorbed by the existing 15-minute SportsGameOdds client cache since
  other MLB/NFL jobs already run frequently in an active window, but not
  a guaranteed-free operation, and said so in the code rather than
  implied otherwise.
- New estimate at this cadence: MLB ~4,300/mo, NFL ~1,100/mo, WNBA
  ~2,800/mo (unchanged) — roughly 8,200/mo total, well inside the
  20,000/mo budget with real headroom left over.

Full suite: **1831 passed, 0 failed**. See `docs/SESSION_HANDOFF.md` for
the full account, including the live liquidity-check data this was
built from.

## 2026-08-22 — WNBA props cadence widened + a real schedule-discovery cache bug fixed

### Why

Operator, looking at the Multi-League Health tab's credit panel (19,833
remaining, only 336.8/month projected), asked directly whether every run
pulls fresh live odds — a fair question given how little of the new
20,000/month budget was being used.

### What was found and fixed

- **Yes for odds fetches, no for WNBA schedule discovery.** Every
  odds-fetch call (`get_odds()`) computes its `commenceTimeFrom`/
  `commenceTimeTo` params from `datetime.now()` on every call, so the
  cache key is effectively unique each time — genuinely fresh every run.
  `get_events()` (WNBA schedule discovery) takes no time-varying params
  at all, and `OddsAPIClient`'s cache has no age-based expiry by default
  — any existing cache file is served forever, no matter how old.
  Reproduced locally: a cache file from 2026-08-20 was still being
  served unconditionally two days later, undercounting real upcoming
  games (6 vs. the real live 7). Fixed with a bounded 5-minute TTL
  (`EVENTS_CACHE_TTL_SECONDS` in `src/odds_api_client.py`) at both call
  sites (`src/worker.py::_discover_wnba_game_times`,
  `src/sports/wnba.py::fetch_and_parse_props`'s own event lookup).
- **WNBA props cadence widened**: the follow-up question ("should that
  make our projected credits be more?") led to checking whether the
  *existing* props scheduler (`wnba_should_fetch_props`, already wired
  into the worker loop since 2026-08-20 — contrary to an earlier,
  incorrect assumption that it had never been scheduled) was just
  conservatively tuned for the old 500-credit budget. It was: 3h pregame
  window, checked once/hour, with a hardcoded 50-credit reserve. Widened
  to a 6h window checked every 30 minutes, and replaced the hardcoded
  reserve with 10% of the real current budget (scales automatically if
  the tier changes again). Synced the per-event dedup window in
  `src/sports/wnba.py` from 1h to 30min to match the new throttle.
- Fixed the Multi-League Health tab's credit-panel label, which still
  said "The Odds API, free tier" — stale on both counts (paid tier now,
  and the budget is shared with the MLB/NFL fallback, not WNBA-exclusive).

Full suite: **1802 passed, 0 failed**.

## 2026-08-22 — SportsGameOdds quota exhaustion: MLB/NFL fallback via The Odds API + shared credit-budget safety

### Why

SportsGameOdds's free-tier monthly object quota was genuinely exhausted
mid-session (verified live via the real `/v2/account/usage` endpoint:
2,501/2,500 entities used), blocking MLB and NFL entirely with no
recovery until next month's reset. Operator asked whether The Odds API
(WNBA's existing provider) could cover all three sports instead of
paying SportsGameOdds's $99/mo tier — verified live it can, for game
markets — and chose to upgrade The Odds API to its $30/mo "20K" tier
(20,000 credits/mo) rather than pay more elsewhere.

### What changed

- Extracted WNBA's game-odds row-building logic (never actually WNBA-
  specific) into a shared `src/odds_api_game_parser.py`; added thin
  per-sport parsers (`src/mlb_odds_parser.py`, `src/nfl_odds_parser.py`)
  and `fetch_game_odds_via_odds_api()` on both `src/sports/mlb.py` and
  `src/sports/nfl.py`. Game markets only (moneyline/spread-or-runline/
  total) — player props would need the same player-identity-resolution
  work WNBA already has, deliberately out of scope.
- Wired as a narrow, explicit fallback in both real call sites
  (`player_prop_scanner.py::run_scan()`, `daily_pipeline.py::
  _stage_fetch_events()`) — engages only on a genuine HTTP 429 with a
  fallback registered for that league; every other failure still raises
  normally.
- Real live end-to-end verification caught two genuine bugs before
  shipping: an `UnboundLocalError` from the fallback branch never
  setting the `events` variable downstream code needed, and an
  unbounded call to The Odds API's `/odds` endpoint returning the
  *entire season* for NFL (272 games, Sept 2026–Jan 2027) instead of
  the near-term slate — fixed with the same `-6h/+42h` window the
  SportsGameOdds path already uses, applied to WNBA too for
  consistency. Also proactively fixed the same Windows cache-filename
  `:` bug in `OddsAPIClient` that `api_client.py` had before.
- **Credit-budget safety**: `DEFAULT_MONTHLY_BUDGET` in
  `src/odds_api_credits.py` was still hardcoded to the old free-tier
  limit (500/mo) after the real account was upgraded to 20,000/mo — now
  env-configurable (`THE_ODDS_API_MONTHLY_BUDGET`), defaulting to
  20000 to match the real current tier. `fetch_game_odds_via_odds_api()`
  on both MLB and NFL now calls the existing `credit_budget_check()`
  before spending and raises a clear, correctly-classified error
  (`EXIT_API_FAILURE`, not a crash) if the shared Odds-API budget is
  genuinely exhausted — otherwise a burst of MLB/NFL fallback usage
  could silently starve WNBA's share of the same account, or vice
  versa.

Live-verified end-to-end twice for MLB (2 real recommendations from 2
real games) and once for NFL (correctly found 0 real games in the
near-term window — a genuine preseason/Week-1 gap, not a bug). Full
suite: **1794 passed, 0 failed** (was 1766 at the start of 2026-08-21).
See `docs/SESSION_HANDOFF.md` for the full account.

## 2026-08-21 / 08-22 — 658-rec settlement backlog + morning-run reliability (catch-up window, failed-job retry)

### Why

Operator asked why MLB showed zero picks on a day with a full slate of
real games, and separately flagged a 658-unresolved-recommendations
warning on the health tab — both traced to real, independent bugs
rather than one root cause.

### What was found and fixed

- **Settlement backlog**: live scan data showed `batting_hits+runs+rbi`
  was the single most common market in the model's top-ranked picks (12
  of the top 15 in a real ranking run), and `mlb_results.py` never had a
  settlement contract for it or the related `batting_runs+rbi` — every
  recommendation for either market sat unresolved forever. Added real
  settlement support (simple sums of already-tracked box-score fields),
  verified against a real completed game (2026-08-21 Braves @ Brewers)
  before writing the regression test. Both markets also added to
  `AUTO_SETTLEABLE_MARKET_TYPES` so they can qualify as Official picks.
  Separately hardened `_check_last_settlement()` to exclude
  recommendations for markets with no settlement contract at all (e.g.
  `first_home_run`) from the backlog count — those are a permanent,
  expected gap, not a growing operational problem.
- **Morning-run catch-up window**: `_check_and_schedule_morning_run()`
  only ever fired inside a fixed 8:30–9:59 AM ET window with no catch-up
  if missed (e.g. a worker restart landing inside those 90 minutes could
  silently lose the entire day). Widened to 8:30 AM through end of day.
- **Failed-job retry**: even after the catch-up-window fix, a *failed*
  job was treated exactly like a successful one, permanently blocking
  retry for the rest of the day. Traced from real production data: MLB's
  8:30 AM run failed after 8 seconds with `exit_code=3` — the exact
  timing signature of exhausting the SportsGameOdds client's 3 retries
  against a rate limit — so the day's `games` table was never populated
  and no pregame-check jobs ever got created either. Added a 60-minute
  cooldown after a failed job, then one retry attempt becomes eligible.
- Also fixed, same investigation: `_run_morning_scan()` was the one job
  runner in `worker.py` still treating exit code 1
  (`EXIT_SUCCESS_NO_RECS`, a normal "ran clean, nothing qualified today"
  result) as a failure — likely mislabeling MLB's own quiet days as
  failed for a while, not just NFL's.

Full suite: **1777 passed, 0 failed**. See `docs/SESSION_HANDOFF.md` for
the full account.

## 2026-08-21 — Multi-League Health production crash chain + Market Intelligence wrong-table fix

### Why

Operator reported real production crashes on the Multi-League Health
tab via screenshots across several rounds, plus a separate concern that
the Market Intelligence tab wasn't showing player props. Each was
diagnosed from the real error text/screenshot, not assumed.

### What was found and fixed

- **Market Intelligence tab** queried the `odds` table, which only ever
  holds game-level markets — player props live in `player_prop_odds`.
  Confirmed via a real local pipeline run: `odds` had 61,615 rows, zero
  props; `player_prop_odds` had 26,385 rows spanning the full 24-market
  registry. Switched the tab to the correct table.
- **`function julianday(text) does not exist`** — real Postgres
  production error. `_check_event_date_sanity()` used SQLite's
  `julianday()` with no PostgreSQL equivalent, breaking the entire
  health report for every league. Fixed by comparing ISO-8601 event
  timestamps as plain text instead of doing date arithmetic in SQL.
- **`tuple index out of range`** — a second crash surfaced immediately
  after the first fix deployed. The Recent Job Activity query had
  literal `%` characters inline in `LIKE` clauses; the DB wrapper always
  passes a params tuple to psycopg2's `execute()` even when empty, so a
  bare `%` with no params triggers unwanted `%`-format substitution.
  Fixed by binding `%` inside parameter values instead of the SQL text.
- **NFL/MLB morning-run jobs mislabeled as FAILED**: reproduced locally
  — the NFL pipeline ran cleanly and correctly found zero qualifying
  opportunities (exit code 1), but the job runner backing both leagues'
  morning runs was the one place in `worker.py` still treating any
  non-zero exit code as failure.
- **"WNBA — 0 games" false reading**: verified live against The Odds API
  directly that 6 real WNBA games existed in the near-term schedule.
  Root cause #1: the shared `games` table is only populated inside
  SportsGameOdds-specific ingest, which WNBA's separate provider path
  never touches — fixed by using the same live schedule-discovery call
  the real WNBA scheduler already relies on. Root cause #2 (recurred
  after #1 deployed): that discovery call runs inside the
  `mlb-vip-dashboard` process, but `THE_ODDS_API_KEY` had only ever been
  added to `mlb-vip-worker`'s env vars in `render.yaml` — added the same
  slot to the dashboard service.

Full suite: **1769 passed, 0 failed**. See `docs/SESSION_HANDOFF.md` for
the full account.

## 2026-08-21 — Website redesign: dark + gold visual identity, structural format match to a reference site

### Why

Operator's contact showed them a competitor site (bigpicksbetting.com)
whose *format* — nav bar, hero structure, CTA buttons, checklist, footer
band, section ordering — they wanted matched, explicitly scoped down to
format/layout rather than a literal feature clone (no search box, no
account system, no unrelated tools) per the operator's own clarification
mid-session.

### What changed

- Both `src/customer_view.py` and `src/control_panel.py` moved from
  their prior navy/mint (customer) and lime/navy (admin) palettes to a
  shared dark + gold identity, with win/loss unified to green/red across
  both sites (previously "win" was gold on the admin dashboard,
  inconsistent with the customer site's green).
- Real equity-curve chart (Altair area chart, Expected vs. Actual) with
  a cumulative-units callout, replacing the plain `st.line_chart`, on
  both sites.
- Hero restructured with an italic serif (Playfair Display) headline, a
  benefits checklist, a two-button CTA row (real in-page anchors, not
  decorative), a top nav bar, and a faint background wordmark — caught
  and fixed a real CSS specificity bug during verification where a later
  `.hero > *` rule silently overrode the watermark's positioning.
- Real Results equity panel reordered to appear immediately under the
  Track Record header (ahead of the pick-by-pick list, now a collapsed
  expander below it); footer band added listing leagues covered and the
  real sportsbooks seen in the data (computed live, not hardcoded).
- Separately, while investigating "our model is still MLB only": the
  admin dashboard's hero always said "MLB Slate" regardless of what
  actually ran — now computed live from the day's official picks;
  `get_official_picks()` never selected `league`/`sport` at all, so
  there was no way to show which league a pick belonged to even once
  NFL/WNBA data existed; and a latent Arrow/pyarrow dtype crash risk (a
  `""` placeholder mixed with real floats) that would trip on any
  pending/unsettled pick — which NFL/WNBA produce constantly right after
  going live.

Every change verified end-to-end via Streamlit `AppTest` against a real
seeded database before committing, not just unit tests. Full suite still
passing throughout. See `docs/SESSION_HANDOFF.md` for the full account.

## 2026-08-20 (SportsGameOdds root cause) — Corrects the prior entry: this was a real code bug, not primarily an account/tier issue

### Why

The immediately preceding session flagged "SportsGameOdds only returns
2024 demo data" as a critical, possibly account-level blocker. Operator
asked for a rigorous re-investigation using real API responses before
accepting that conclusion — it turned out to be wrong about the cause.

### What was found

Fetched the live SportsGameOdds API reference docs directly: **there is
no `date` query parameter on this API at all** — the real filters are
`startsAfter`/`startsBefore`. `src/api_client.py` had been sending a
`date` param the API silently ignores. But the real production call
(`odds_available=True`, no date filter — what `daily_pipeline.py`/
`run_scan()` actually send) already returns real current games, verified
directly (real 2026-08-20 MLB games, real NFL preseason games) — the
`oddsAvailable=true` filter happens to exclude the account's old demo
events. **MLB/NFL recommendation generation was very likely never
actually broken.** What *was* broken: `_discover_nfl_game_times()` (built
the prior session) used `odds_available=False` with no date filter — that
exact combination does return stale 2024 data, meaning NFL's automatic
scheduler would never have fired. A second, independent, more serious bug
found by the same investigation: `_parse_status()` looked for a `"state"`
key that doesn't exist in the real API (real shape: boolean flags `live`/
`started`/`completed`/`ended`/`finalized`/`cancelled`) — every game
status silently defaulted to "scheduled" forever. Redundant time-based
skip logic masked most of the impact, except for one real gap: a
cancelled game with a still-future scheduled time had no safety net.

### What changed

- `src/api_client.py::get_events()`: real `starts_after`/`starts_before`
  params; `date_str` now actually works (translates to a UTC-day window)
  instead of being silently ignored. Also fixed a filesystem bug the fix
  immediately surfaced: the cache-filename builder didn't sanitize `:`,
  which Windows rejects — ISO timestamps contain `:`.
- All 4 real `SportsGameOddsClient.get_events()` call sites now pass
  explicit date windows.
- `_parse_status()` now derives real status from the real boolean fields;
  `_is_game_skippable()` gained `_CANCELLED_STATES` handling.
- `src/league_health.py`: new `_check_event_date_sanity` check — nothing
  else would have caught "scan ran recently, price is fresh, but the
  games are from the wrong year."

Live-verified end-to-end after the fixes: 34 real MLB recs and 25 real
NFL recs against real current/upcoming games, a real simultaneously-live
MLB game correctly excluded, both rendering correctly on the website.
Full suite: **1766 passed, 0 failed** (was 1757). See
`docs/SESSION_HANDOFF.md` for the full account, including which parts of
the prior entry's conclusion this corrects.

## 2026-08-20 (latest) — Real end-to-end deployment validation: render.yaml fix + 4 real bugs found and fixed

### Why

"Tests pass" isn't proof a pipeline works — operator asked for genuine
end-to-end validation against live data before calling anything
deployment-ready, plus the exact Render config change needed to actually
go live.

### What was found and fixed

- **`render.yaml`**: `mlb-vip-worker` had no `THE_ODDS_API_KEY` at all —
  without it, WNBA scheduling silently no-ops forever, never crashing,
  never logging loudly. Added (`sync: false`; operator sets the real
  value in Render's dashboard).
- Ran the real pipeline live against real WNBA games in progress
  tonight: real schedule discovery, real odds collection (612 rows, 9
  real books, 2 capture batches 6 minutes apart), 25 real
  recommendations, verified rendering on the website via `AppTest`
  (zero exceptions). Ran a full settlement+CLV cycle against a real
  completed WNBA game fetched fresh from ESPN.
- **4 real bugs found by this live validation** (none visible from
  `pytest` alone — existing fixtures always populated the `games` table,
  never hit WNBA's empty-lookup case): (1) blank matchup **and a
  bypassed live-game safety check** for any non-SportsGameOdds league —
  the most serious of the four, could have let an already-started game
  get recommended on; (2) `_stage_ingest` hardcoded `"league": "MLB"`
  regardless of `config.league`; (3) `market_settlements.league` always
  defaulted to `'MLB'`, confirmed with a real WNBA settlement; (4)
  `get_settled_recommendations()` never joined `closing_prices`, so CLV
  never reached `performance_summary()`'s callers outside the website.
  All four fixed, all four have regression tests now.
- **Critical open finding, not a code bug**: the SportsGameOdds API key
  in this checkout's `.env` returns the same fixed ~10 historical events
  (dated Feb–Aug 2024) for MLB and NFL regardless of the date requested
  — a "notice" field confirms a tier-limited account, not a broken key.
  WNBA (different provider) is fully live and current. Cannot verify
  whether Render's production key differs — needs operator confirmation.

Full suite: **1757 passed, 0 failed** (was 1752). See
`docs/SESSION_HANDOFF.md` for the full account.

## 2026-08-20 — NFL/WNBA production scheduling, credit-aware WNBA polling, duplicate-pick suppression, per-league job isolation, per-league health

### Why

Everything built earlier in the day (player props, settlement, CLV,
website) was verified and tested but nothing actually ran it on a
schedule. Operator asked for NFL and WNBA to start real production
operation alongside MLB — with credit-aware WNBA polling (free tier is a
hard 500/month), real NFL scheduling (not a "Sunday only" assumption),
league-isolated job/lock/grading behavior, duplicate-pick suppression so
a rescan's price wiggle doesn't inflate the historical record, and
per-league health visibility.

### What changed

- **`src/league_schedule.py`** (new): pure per-league scheduling
  decisions. NFL driven by discovered kickoff times, not a day-of-week
  table. WNBA: free schedule discovery, flat-rate game odds, hard-gated
  player props (3h pregame window, once/hour, credit-reserve-aware).
- **`src/odds_api_credits.py`** (new): persists The Odds API's real
  `x-requests-*` credit headers; `credit_budget_check()` is checked both
  at scheduling time and again inside the actual fetch call. Fixed a real
  bug where a headerless/mocked response's NULL reading could mask a
  genuine prior credit reading.
- **Closed a real integration gap**: `fetch_and_parse_props()` (built
  earlier the same day) was never called from `run_scan()` — WNBA props
  could not have reached a recommendation. Added `fetch_props` to
  `run_scan()`/`PipelineConfig`; added per-event dedup so the scheduler's
  hourly rechecks don't re-spend credits on games just fetched.
- **Duplicate-pick suppression** (`src/official_picks.py`,
  `database/db_manager.py`): a real bug — every rescan froze a NEW
  official pick even on a one-cent price move (fingerprint includes
  `observation_timestamp`). `classify_pick_update()` (implied-probability
  delta threshold, any line change is always material) +
  `freeze_or_update_official_pick()` (supersede-and-replace vs.
  update-tracking-fields-in-place) fix it. Website/admin queries filter
  `pick_status = 'ACTIVE'`.
- **Multi-league job isolation** (`src/worker.py`): pregame lock key is
  now per-league (was a single shared string — a real cross-league
  blocking bug). `_run_catchup_grading` isolates each league's
  result-ingestion in its own try/except. New job types reuse the
  existing job queue/lock infrastructure rather than a parallel one.
- **`src/league_health.py`** (new) + a new Multi-League Health tab in
  `control_panel.py`: per-league PASS/WARN/FAIL on last recommendation/
  settlement, stale markets, qualified opportunities, job activity, and
  (WNBA) credit budget.
- **Live-verified** (read-only): NFL/WNBA schedule discovery against the
  real APIs, and real WNBA credit headers — **436/500 credits remaining**
  as of this session.

Full suite: **1752 passed, 0 failed** (was 1643). One flaky test (real
wall-clock-dependent day-boundary crossing in two WNBA scheduling tests)
found and fixed; suite reran 3x clean afterward. See
`docs/SESSION_HANDOFF.md` for the full account.

## 2026-08-20 — WNBA player props + identity; shared game settlement; line-movement-aware CLV; confidence scoring wired in; multi-sport pick lifecycle website

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
