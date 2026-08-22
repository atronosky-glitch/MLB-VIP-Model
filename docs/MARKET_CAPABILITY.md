# Market Capability Registry

Per-league summary of what this platform can actually scan and settle,
distinguishing "the data provider's catalog lists this market" from "we
have verified, registered, working support for it." Per the project's
non-negotiable rule, a market is only marked supported here once its real
oddID shape and live liquidity have been checked against a live API
response — never assumed from documentation or another league's pattern.

Generate the machine-readable version at any time:

```python
from src.sports import market_capability_report
import json
print(json.dumps(market_capability_report(), indent=2))
```

Last verified: 2026-08-20, against the live SportsGameOdds v2 API
(`GET /leagues`, `GET /events?leagueID=...`, `GET /markets?leagueID=...`)
and, for WNBA, the live The Odds API v4
(`GET /v4/sports`, `GET /v4/sports/basketball_wnba/odds`,
`GET /v4/sports/basketball_wnba/events/{id}/odds`,
`GET /v4/sports/basketball_wnba/events/{id}/odds?markets=player_points,...`).

**Game-level settlement (moneyline/spread/total) is now shared across all
three leagues** via `src/game_settlement.py` — sport-agnostic grading that
works only from each recommendation's own stored side/line/raw_line plus a
verified final score. This closed the "game markets aren't auto-settled by
any league" gap noted in the per-league sections below as of 2026-08-19;
those notes are left in place with a correction rather than rewritten, so
the historical record of when each gap closed stays intact.

---

## MLB — Supported (production)

Data provider: SportsGameOdds v2 (`leagueID=MLB`). Settlement: MLB StatsAPI
(free, keyless, `src/mlb_results.py`).

24 markets registered in `src/prop_config.py::MARKET_REGISTRY`, spanning
pitcher and batter O/U + YN props plus game moneyline/run-line/total. See
`PROJECT_STATUS.md` → "Current supported markets" for the exact list; some
registered markets are Research-only because `mlb_results.py` doesn't have
a verified settlement contract for them yet (composites, first-home-run,
run-line semantics).

Status: **active production pipeline**, running on Render.

**2026-08-20 caveat — SUPERSEDED same day, corrected below.** An earlier
pass today concluded the local `.env` `SPORTSODDS_API_KEY` was tier-limited
to a fixed ~10-event 2024 historical dataset. Re-investigated after being
asked to stop assuming and use actual API responses/timestamps: that was
wrong. Root cause was two code bugs, not the account or key: (1)
`get_events()` sent the requested date via a `date` query parameter that
doesn't exist on the real API (confirmed against live reference docs —
the real filters are `startsAfter`/`startsBefore`), so date-scoped calls
silently fell back to the provider's default (oldest-first) page; (2)
`_parse_status()` looked for a `"state"` string key the real event-status
object never has (it's boolean flags: `live`/`started`/`completed`/
`ended`/`finalized`/`cancelled`/`delayed`/`hardStart`), so every event's
derived status silently defaulted to `"scheduled"` forever. The actual
production call shape (`odds_available=True`, no date filter) was
returning real current data the whole time and was never affected by bug
(1); bug (2) affected status-dependent logic everywhere. Both fixed and
live-verified: 34 real MLB recommendations and 25 real NFL recommendations
generated end-to-end against real current games, including one real
simultaneously-live MLB game correctly excluded by the freshness/skip
logic. This key is production-capable for live current MLB/NFL data —
no tier upgrade needed. Full findings: `docs/SESSION_HANDOFF.md` →
"SportsGameOdds investigation" (2026-08-20).

## NFL — Supported (production)

Data provider: SportsGameOdds v2 (`leagueID=NFL`) — confirmed identical
event/odds schema to MLB. Settlement: ESPN's public NFL scoreboard/summary
API (free, keyless, `src/nfl_results.py`).

### Registered (`src/sports/nfl.py::MARKET_REGISTRY`, 11 markets)

| Market | Form | Real liquidity observed (2026-08-19 preseason snapshot) |
|---|---|---|
| Moneyline | O/U-shaped (away/home) | Yes — multiple books |
| Spread | O/U-shaped (away/home) | Yes — multiple books |
| Game Total | O/U | Yes — multiple books |
| Passing Yards | O/U | Yes (37 active groups, 6+ books at peak) |
| Passing Touchdowns | O/U | Yes (15 active groups) |
| Passing Interceptions | O/U + Y/N | Yes (24 active groups each form) |
| Rushing Yards | O/U | Yes (57 active groups) |
| Receiving Yards | O/U | Yes (81 active groups) |
| Receptions | O/U | Yes (20 active groups) |
| Anytime Touchdown | O/U + Y/N | Yes (74/82 active groups) |
| Field Goals Made | O/U | Yes (31 active groups) |

Settlement coverage matches the registry exactly (all 11 markets have a
verified extraction path in `src/nfl_results.py`). Game-level markets
(moneyline/spread/total) now auto-settle too, via the shared
`src/game_settlement.py` + `grade_available_game_recommendations()` path
(2026-08-20) — see the note at the top of this document.

### Catalog-available, not registered (insufficient live liquidity as of 2026-08-19)

The provider's full catalog (`GET /markets?leagueID=NFL`, 171 market
groups) includes these, but they had zero or near-zero `activeEvents` /
too few supporting bookmakers to trust for LOO consensus
(`MIN_COMPARISON_BOOKS = 4`) at audit time. Re-verify before registering:

`passing_attempts`, `passing_completions`, `passing_longestCompletion`,
`rushing_attempts`, `rushing_longestRush`, `rushing_touchdowns`,
`receiving_touchdowns`, `receiving_longestReception`, `kicking_totalPoints`,
`extraPoints_kicksMade`, `defense_sacks`, `defense_interceptions`,
`defense_soloTackles`, `defense_assistedTackles`, `defense_combinedTackles`,
`passing+rushing_yards`, `rushing+receiving_yards`, `fantasyScore`,
`turnovers`, `firstTouchdown`, `lastTouchdown`.

Also available with real liquidity but not yet wired in: 1st-half /
1st-quarter / 2nd-half spread, total, and moneyline variants. This is a
reasonable near-term expansion (the provider data is already there),
distinct from the list above where liquidity itself was the blocker.

Status: **active production pipeline**, running on Render since
2026-08-20 (`c141475`) — `src/league_schedule.py::nfl_should_run_daily_scan`/
`nfl_should_run_pregame_check` (driven by real discovered kickoff times)
and `src/worker.py`'s `morning-run-nfl`/`pregame-check-nfl` job types are
both deployed and generating real recommendations, including surviving a
real SportsGameOdds quota exhaustion via the fallback below. Pinnacle
sharp-reference pricing is unverified for NFL (`pinnacle_feed.py` is
hardcoded to baseball's sport ID); NFL runs on LOO market-median
consensus only, the same fallback path MLB itself uses whenever Pinnacle
is absent.

### Supplemental player props via The Odds API (added 2026-08-22)

Alongside the SportsGameOdds-sourced registry above, MLB and NFL now
also pull a small set of player props from The Odds API — the same
provider WNBA uses, and the same one the game-markets 429-fallback
below uses. This is genuinely additive, not a fallback: it runs on its
own schedule regardless of SportsGameOdds's health, merged into the
same scan (`fetch_player_props_via_odds_api()` on each league's
`src/sports/<league>.py`, wired into `player_prop_scanner.run_scan()`).

Registered after a live liquidity check (not assumed) against a real
event on each side — see `src/mlb_props_parser.py`/
`src/nfl_props_parser.py`'s docstrings for the full snapshot:

| League | Market | Odds-API key | Books observed | Reused market_type |
|---|---|---|---|---|
| MLB | Pitcher Outs | `pitcher_outs` | 6 | `pitching_outs_ou` |
| MLB | Batter Total Bases | `batter_total_bases` | 5 | `batting_totalBases_ou` |
| MLB | Pitcher Strikeouts | `pitcher_strikeouts` | 5 | `pitching_strikeouts_ou` |
| MLB | Batter Hits | `batter_hits` | 4 | `batting_hits_ou` |
| NFL | Passing Yards | `player_pass_yds` | 2 (19 days pre-kickoff) | `passing_yards_ou` |
| NFL | Rushing Yards | `player_rush_yds` | 2 (19 days pre-kickoff) | `rushing_yards_ou` |
| NFL | Receptions | `player_receptions` | 2 (19 days pre-kickoff) | `receiving_receptions_ou` |
| NFL | Receiving Yards | `player_reception_yds` | 2 (19 days pre-kickoff) | `receiving_yards_ou` |

MLB's book counts were checked against a real near-term game, so they're
a genuine liquidity read. NFL's were checked against the earliest
available event (19 days before kickoff at check time) — real, but
thin-because-early, not a mature liquidity read; re-verify closer to
kickoff before trusting these counts for anything beyond "the market
exists." `player_anytime_td` was also confirmed live but is single-sided
"Yes" pricing on this provider (not Over/Under), so it's deliberately
not registered — the same "different market shape" reason WNBA's
first-basket/double-double/triple-double are excluded above.

Every registered market reuses each league's EXISTING primary-registry
`market_type` string rather than inventing a new one, so the existing
settlement contract already applies — confirmed all 8 (4 MLB + 4 NFL)
already have one (`AUTO_SETTLEABLE_MARKET_TYPES` for MLB,
`_SIMPLE_STAT_FIELDS` for NFL). Zero new settlement code was needed.

Cadence is deliberately narrower than WNBA's own props pace: MLB alone
averages roughly 12x WNBA's daily game count, so the same per-game
frequency would cost proportionally more in aggregate. MLB: 3h pregame
window, checked at most once/60min. NFL: 4h window, once/60min. See
the "Update 2026-08-22" cost note below for the full budget math across
all three leagues at this cadence.

## WNBA — Supported (verified live, game markets + player props + settlement)

SportsGameOdds (the provider MLB/NFL use) does **not** offer WNBA at any
tier — verified twice against the live account (`GET /leagues` omits it,
`GET /events?leagueID=WNBA` returns HTTP 400) and against SportsGameOdds's
own public pricing page (free through $299/mo Pro, 53+ leagues, still no
WNBA). Upgrading that subscription does not solve this.

**The Odds API (the-odds-api.com) does cover WNBA**, confirmed live
2026-08-19 with a free API key (`THE_ODDS_API_KEY`, free tier, 500
credits/month):

- `GET /v4/sports/basketball_wnba/odds` — real multi-book game odds. A
  live check found 5 games, 9 bookmakers on one sample game (fanduel,
  draftkings, betmgm, bovada, betrivers, betus, betonlineag, mybookieag,
  lowvig) — comparable book depth to MLB. Cost: `len(markets) *
  len(regions)` credits per call — 3 credits for h2h+spreads+totals in the
  US region.
- `GET /v4/sports/basketball_wnba/events` — free (0 credits) event list.
- `GET /v4/sports/basketball_wnba/events/{id}/odds` — player props
  (`player_points`/`player_rebounds`/`player_assists`/`player_threes`)
  confirmed live on 3-4 books, real players and lines.

### Registered (`src/sports/wnba.py::MARKET_REGISTRY`, 3 markets)

| Market | Odds API market key | Books observed |
|---|---|---|
| Moneyline | `h2h` | 9 |
| Spread | `spreads` | 7-9 |
| Game Total | `totals` | 6-9 |

Verified end-to-end through the full scanner pipeline
(`run_scan(league="WNBA", ...)`) against real live data: 5 games → 210
approved odds rows → 31 O/U groups → 25 ranked opportunities with real EV
values. `daily_pipeline.py --league WNBA` works too (its early
SportsGameOdds-specific ingest stage cleanly no-ops for a non-SportsGameOdds
league; the real fetch happens inside the scan stage).

### Player props — registered (2026-08-20), gated on verified identity

The Odds API gives **no stable player ID** for props, only a free-text
name in `outcome.description` — so before any prop market could be
registered, `src/player_identity.py` was built: resolves a prop's raw
name against ESPN's free WNBA roster API, scoped to the two teams actually
playing in that event (not a league-wide search, to minimize collision
risk), producing HIGH (exact normalized match) / MEDIUM (suffix-stripped
or initial+last match) / LOW (ambiguous) / UNRESOLVED (no match)
confidence. Only HIGH/MEDIUM-confidence props are ever emitted as odds
rows (`src/wnba_odds_parser.py::parse_wnba_player_props()`) — a prop with
uncertain identity is excluded before it can become a recommendation, not
flagged after the fact.

With that identity layer in place, live responses were inspected market by
market (never assumed) to determine exactly which markets are safe to
support:

| Market | Odds API market key | Registered |
|---|---|---|
| Points | `player_points` | Yes |
| Rebounds | `player_rebounds` | Yes |
| Assists | `player_assists` | Yes |
| Threes made | `player_threes` | Yes |
| Points + Rebounds + Assists | `player_points_rebounds_assists` | Yes |
| Points + Rebounds | `player_points_rebounds` | Yes |
| Points + Assists | `player_points_assists` | Yes |
| Rebounds + Assists | `player_rebounds_assists` | Yes |
| First basket | `player_first_basket` | No — confirmed live, different market shape (single winner, not O/U/YN), deferred |
| Double-double | `player_double_double` | No — same reason |
| Triple-double | `player_triple_double` | No — same reason |

Registered via `src/sports/wnba.py::fetch_and_parse_props()` — a deliberate
**opt-in**, not bundled into the default `fetch_and_parse()` game-odds
call, because props are billed per-event (~8 credits/event) versus 3
credits for the entire game-odds bulk call (see cost note below).

### Settlement — built (2026-08-20)

`src/wnba_results.py` settles both WNBA game markets and all 8 registered
player-prop markets from ESPN's free public WNBA API
(`site.api.espn.com/apis/site/v2/sports/basketball/wnba/...`), verified
live against a real completed game (Dallas Wings 70 @ Golden State
Valkyries 78, event 401857151). WNBA's boxscore is one flat stat category
per team (`MIN, PTS, FG, 3PT, FT, REB, AST, TO, STL, BLK, OREB, DREB, PF,
+/-`) — simpler than MLB/NFL's nested structure. Postponed/cancelled/
suspended games settle as VOID via ESPN's `status.type.name` STATUS_*
vocabulary, same pattern as NFL.

### Cost note for continuous scanning (updated 2026-08-20 with real scheduler math)

`src/league_schedule.py` now implements the actual credit-aware cadence
this section used to only estimate; `src/odds_api_credits.py` persists
The Odds API's real `x-requests-*` headers, so the numbers below are
either live-verified or computed from the exact same cost constants the
scheduler itself uses (`GAME_ODDS_COST=3`, `PROPS_COST_PER_EVENT=8`).

**Live-verified as of 2026-08-20**: 436/500 credits remaining this
billing cycle (64 already used from this and earlier sessions' live
testing) — confirmed via a real, non-cached `/events` call's response
headers.

**Game markets** (`wnba_should_fetch_game_odds`, flat 3 credits/call
regardless of slate size): a conservative once-daily cadence costs ~90
credits/month, comfortably free-tier-sustainable. The scheduler's actual
ramped cadence (wider interval far from tip-off, tighter near it) lands
in roughly the 4-6 fetches/day range on an active game day — 360-540
credits/month if every day had games (WNBA doesn't play every day, so
real usage is lower) — still within budget on a typical schedule, worth
monitoring via the Multi-League Health tab if it isn't.

**Player props** (`wnba_should_fetch_props`, 8 credits/event, gated to a
3-hour pregame window, throttled to once/hour): added per-event dedup
this session (`_recently_captured_prop_event_ids`) so the scheduler's
hourly rechecks don't re-spend credits on games already covered — but
even with that, a single busy multi-game WNBA day (several games, each
checked a few times across its own pregame window) can consume
**80-120+ credits in one day**. Against a 500/month budget (~16.7/day
average), this means a sustained daily props cadence is **not**
comfortably free-tier-sustainable — `credit_budget_check()`'s reserve
(10% of the monthly budget held back) is the real backstop that
prevents this from silently exhausting the account, not the scheduling
cadence alone. `fetch_and_parse_props()` remains opt-in
(`fetch_props=True` on `run_scan()`/`PipelineConfig`), not part of the
default scan.

**Verdict on the $30/mo tier**: not needed for game markets under any
realistic cadence. Needed only if the operator wants player props
running on a *sustained daily* cadence across most/all of a full WNBA
season — an occasional or manually-triggered props scan stays well
within the free tier. Nothing purchased; this is presented for an
operator decision, not acted on.

### Update 2026-08-22 — account upgraded to 20,000 credits/month; now also shared by an MLB/NFL fallback

The cost analysis above (free tier, 500 credits/month) is now
historical — kept for the reasoning, not the numbers. Two things changed
the same day:

1. **SportsGameOdds's own free-tier monthly object quota was genuinely
   exhausted** (verified live via the real `/v2/account/usage` endpoint:
   2,501/2,500 entities used), blocking MLB and NFL entirely. Rather than
   pay SportsGameOdds's Rookie tier ($99/mo, 100k objects), the operator
   upgraded **The Odds API** — this section's provider, previously WNBA-
   only — to its paid "20K" tier (**20,000 credits/month, $30/mo**),
   specifically so MLB and NFL could fall back to it for game markets
   when SportsGameOdds's own quota runs out
   (`fetch_game_odds_via_odds_api()` on `src/sports/mlb.py`/
   `src/sports/nfl.py` — see `docs/SESSION_HANDOFF.md`'s 2026-08-21/22
   entry for the full build).
2. That means **this budget is no longer WNBA-exclusive** — MLB and NFL
   now spend from the same account whenever SportsGameOdds returns a 429.
   `src/odds_api_credits.py::DEFAULT_MONTHLY_BUDGET` was updated from the
   hardcoded 500 to reflect the real 20,000/month tier (env-configurable
   via `THE_ODDS_API_MONTHLY_BUDGET` if it changes again), and
   `credit_budget_check()` is now called inside the MLB/NFL fallback
   fetch functions too, not just WNBA's scheduler — a burst of MLB/NFL
   fallback usage during a SportsGameOdds outage can't silently starve
   WNBA's share of the same account, or vice versa.

At 20,000 credits/month, every scenario modeled above (game markets
~90-540/month, even a sustained daily player-props cadence at
80-120+/day ≈ 2,400-3,600/month) fits comfortably inside the new budget
with room to spare for MLB/NFL fallback usage on top — the free-tier
sustainability concern this section originally raised no longer applies
at the current tier. Re-verify the real remaining balance via the
Multi-League Health tab or `/account/usage` rather than trusting this
note indefinitely, the same discipline that caught the original 500/mo
numbers going stale.

### Update 2026-08-22 (later same day) — MLB/NFL got real props too; final combined estimate

Same day, after confirming the account had far more headroom than any
single league needed: MLB and NFL got their own supplemental player
props via this same provider (see each league's section above). Final
projected combined usage at the cadence actually shipped:

| Source | Monthly estimate |
|---|---|
| WNBA (game odds + props, 6h/30min cadence) | ~2,800 |
| MLB (props only, 3h/60min cadence, 4 markets) | ~4,300 |
| NFL (props only, 4h/60min cadence, 4 markets) | ~1,100 |
| **Total** | **~8,200** |

Well inside the 20,000/mo budget, with the 2,000-credit (10%) reserve
still intact on top. MLB's per-game cadence was deliberately kept far
narrower than WNBA's — WNBA's own 6h/30min pace applied to MLB's ~12x
higher daily game volume would alone have cost an estimated ~34,000/mo,
more than the entire budget. This is why `src/league_schedule.py`'s
props scheduling constants are per-league, not one shared global.

## Future leagues (architecture ready, not started)

SportsGameOdds already supports NBA, NHL, NCAAF, NCAAB, MLS, and UEFA
Champions League on this account per the same `/leagues` check — no
data-access gap like WNBA's for any of them. Adding any of them follows
the same pattern NFL did: verify the live event/odds
schema and market catalog, build a `src/sports/<league>.py` adapter with a
liquidity-vetted registry, add a settlement adapter if a free/verified
results source exists, and add tests before registering it.
