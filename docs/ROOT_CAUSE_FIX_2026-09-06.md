# Root-cause + fix pass — 2026-09-06/07

Follow-up to `docs/PRODUCTION_AUDIT_2026-08-24_2026-09-06.md`. Per operator
instruction: no threshold loosened, no forced diversification, no
methodology change except where a genuine bug required it. Every fix below
is additive/wiring-level; none of them change what qualifies as a
recommendation, an Official pick, or how EV/model score is computed.

---

## Priority 1 — Pitcher Strikeouts (root cause found, **not a code bug**)

Traced real production strikeout groups (Sep 1–6, 4,228 raw rows) through
`analyze_prop_group()` directly — the exact, unmodified function
production uses, fed real `player_prop_odds` rows.

**Finding: the function works correctly.** Strikeouts genuinely does
produce positive-EV candidates (16 in the 3-day sample, ~5.3/day) — but
**100% of them** get caught by the market-agnostic "extreme outlier EV"
safeguard (`OUTLIER_EV_THRESHOLD = 0.1` in `src/player_prop_analysis.py`):
if any one book in a group is >10% away from the group's own consensus,
the *entire group* (every book in it, not just the outlier) is demoted
from `VALID_MARKET` to `NEEDS_REVIEW`, which excludes it from
`actionable`-mode selection.

Compared directly against Home Runs O/U (which does produce recommendations)
over the same window: Home Runs loses a *similar proportion* (47.2%, 206 of
436 positive-EV groups) to the same gate — the mechanism isn't
strikeout-specific. The difference is pure volume: Home Runs still has 230
survivors/day even after losing nearly half; Strikeouts' entire (much
smaller) positive-EV sample happened to fall inside that same ~50% loss
rate, leaving zero.

Concrete example (real data, Sep 5): a strikeout group priced OVER -175 /
UNDER +130 by DraftKings and OVER -115/-105 / UNDER -115/-125 by two other
books — a genuine ~15-point disagreement, not obviously a data error. The
group's paired 3-book leave-one-out consensus is sensitive enough at this
book count that one strongly-disagreeing book pulls every other book's own
fair-probability reference with it, which is a structural property of the
LOO methodology at low book counts, not a bug introduced recently.

**No fix applied.** Loosening `OUTLIER_EV_THRESHOLD`, or changing the
group-quality demotion to only affect the disagreeing book instead of the
whole group, would directly increase strikeout pick volume — exactly what
the operator instructed not to do without it being a clear bug. This is a
real design tradeoff (safety against bad/stale prices vs. losing genuine
sharp disagreement) for the operator to decide on, not something to change
unilaterally.

---

## Priority 2 — Daily Official Pick Cap (real bug, fixed)

**Root cause:** `src/daily_pipeline.py`'s `_stage_freeze` called
`rank_and_select_official_picks(today_recs)` with no `already_selected_today`
argument. Every one of a day's 4–20 separate pipeline invocations (morning
run, each pregame check) re-ranked from an empty "already selected" list,
so `official_daily_max_picks` (default 3) only ever capped a single
invocation — not the calendar day. Production daily counts ran 3–11.
A second, related gap: the candidate query (`today_recs`) had no `league`
filter, so a pipeline run for one league could rank candidates from
whichever other leagues also had same-day recommendations.

**Fix:**
- `src/daily_pipeline.py`: `today_recs` now filters `AND league = ?`
  (`config.league`); `already_today = get_official_picks_today(conn,
  league=config.league)` is now passed as `already_selected_today`.
- `database/db_manager.py::get_official_picks_today` extended with an
  optional `league` filter and `player_id`/`event_id` columns (needed by
  `rank_and_select_official_picks`'s dedup/game/player/market-type
  counters — previously missing from this query's output).

Every call is idempotent and derives the day's state entirely from the
database (`date(op.selected_at) = date('now')`, `pick_status='ACTIVE'`),
so the cap holds across every separate invocation and survives a worker
restart with zero in-memory state. Verified with 6 new tests in
`tests/test_phase15_official_picks.py::TestDailyOfficialPickCapAcrossRuns`
(and 2 more on `get_official_picks_today` itself), including an explicit
restart simulation via two independent file-backed connections.

No existing historical picks were touched or removed.

---

## Priority 3 — SportsGameOdds entity cap

**Live-verified right now** (`GET /account/usage`, read-only):

```
tier: amateur
per-month: max-entities 2500, current-entities 2501   ← over cap
per-minute: max-requests 10
```

This is the same 2,501/2,500 figure `TODO.md` recorded as "resolved" on
2026-08-22 — it has not moved since, including across the 2026-09-01
calendar-month boundary. **Whether/when this resets is not something this
audit can determine from outside SportsGameOdds's own billing system** —
the "per-month" label implies a monthly cycle, but the count sitting
frozen at the same value across a real calendar-month rollover means
either the cycle isn't calendar-aligned (a subscription-anniversary date)
or this tier's cap doesn't function as a resetting quota at all. **Needs
operator confirmation directly with SportsGameOdds** — this document will
be updated once known.

### Currently flowing (raw data arriving daily, as of 2026-09-07)

| Market | Last seen |
|---|---|
| Home Runs O/U (`batting_homeRuns_ou`) | today |
| Total Bases O/U (`batting_totalBases_ou`) | today |
| Moneyline (`game_moneyline`) | today |
| Pitcher Outs O/U (`pitching_outs_ou`) | yesterday |
| Run Line (`game_runline_ou`) | yesterday |
| Game Total (`game_total_ou`) | 2 days ago |

### Currently dark (zero raw rows; last real data shown)

| Market | Last real data |
|---|---|
| Pitcher Strikeouts O/U (`pitching_strikeouts_ou`) | 2026-08-06 |
| Batter Hits O/U (`batting_hits_ou`) | 2026-08-06 |
| Batter RBI O/U (`batting_RBI_ou`) | 2026-08-06 |
| Batter Doubles O/U (`batting_doubles_ou`) | 2026-08-06 |
| Batter Singles O/U (`batting_singles_ou`) | 2026-08-06 |
| Batter Hits+Runs+RBI O/U (`batting_hits+runs+rbi_ou`) | 2026-08-06 |
| Batter Doubles Y/N (`batting_doubles_yn`) | 2026-08-19 |
| Batter Home Runs Y/N (`batting_homeRuns_yn`) | 2026-08-20 |
| Batter Hits+Runs+RBI Y/N (`batting_hits+runs+rbi_yn`) | 2026-08-21 |
| Batter Singles Y/N (`batting_singles_yn`) | 2026-08-21 |
| Batter RBI Y/N (`batting_RBI_yn`) | 2026-08-21 |
| Batter Hits Y/N (`batting_hits_yn`) | 2026-08-21 |
| Batter Stolen Bases Y/N (`batting_stolenBases_yn`) | 2026-08-21 |
| Pitcher Hits Allowed, Walks Allowed, Earned Runs, Win; Batter Runs, Runs+RBI, Walks, Triples, First HR | never (zero rows in this database's entire history) |

The pattern (low-player-count markets like HR/TB/K/Outs/game odds survive;
full-lineup markets like Hits/RBI/Singles/Doubles/StolenBases/Walks go
dark) is consistent with — but not provably caused by — an entity-count
cap silently truncating full-roster coverage while leaving markets that
need far fewer distinct player entities per game untouched. Not proven,
because this cannot be confirmed from inside this database alone; flagged
as the leading hypothesis, not a certainty.

### What recovers automatically vs. what won't

If/when this cap resets or the plan is upgraded, every market in
`src/prop_config.py::MARKET_REGISTRY` (all 24, none removed by this
session's work) will resume producing recommendations automatically —
nothing in the selection/qualification code was changed to compensate for
or route around the current gap, per instruction. Nothing about this is
self-healing on a fixed timer that this codebase can observe or control;
it depends entirely on the provider's own quota behavior.

### Dashboard fix (done)

`src/control_panel.py`'s "Market Intelligence" tab (Tab 6) previously only
ever listed markets that had at least one raw row that day — a market
going completely dark from a provider gap was invisible, indistinguishable
from simply not being shown. Now:

- Every row gets a `Status` column: **No data from provider** / **Data
  received, no bet qualified** / **Producing recommendations**, computed
  by the new `src.qualification_funnel.classify_market_data_status()`
  (pure function, unit-tested — `tests/test_qualification_funnel.py`).
- Registry markets with zero raw rows today (previously absent from the
  table entirely) are now explicitly listed as **No data from provider**.

This is purely a reporting change — it does not alter which
recommendations qualify, how EV/model score is computed, or which markets
get scanned.

---

## Priority 4 — Missing CLV (real bug, fixed)

**Root cause:** `database.db_manager.capture_closing_prices()` only
populates the canonical `closing_prices` table (the one CLV reporting and
`src/customer_view.py` actually read from) when called with
`snapshot_kind="final"`. Every real production call site — the daily
pipeline's own morning-run and pregame-check stages — calls it with
`snapshot_kind="morning"` (the default) or `"pregame"`, both of which only
record `CLOSING_SNAPSHOT` lifecycle evidence
(`recommendation_lifecycle_events`), never the canonical table.
`snapshot_kind="final"` was previously used only in tests
(`tests/test_phase19a_lifecycle.py`), never in `src/`.

Verified against production: `closing_prices` has **0 rows, ever** (its
`MIN`/`MAX(created_at)` are both `NULL`), while
`recommendation_lifecycle_events` has **9,168** real `CLOSING_SNAPSHOT`
events — the CLV computation (`calculate_clv`) has been running correctly
this whole time; its result just never reached the table anything reads
from.

**Fix:** `src/automatic_grading.py` — grading (`grade_available_recommendations`
and `grade_available_game_recommendations`) is the first point in the
pipeline where a recommendation is both settled and its game is confirmed
over, so it's the correct trigger for the one true "final" snapshot. Added
`_capture_final_closing_price(conn, rec)`, called right after each
successful settlement (including VOID), which calls the existing,
unmodified `capture_closing_prices(conn, [rec], snapshot_kind="final")`.
That function is already idempotent per `recommendation_id` (an existing
`closing_prices` row short-circuits it), so this is safe on every grading
pass, including re-runs, and a closing-price lookup failure for one
recommendation is caught and logged — it can never block that
recommendation's settlement.

Verified with 3 new tests in `tests/test_automatic_grading.py`: a closing
price is actually captured on grading, a second grading pass doesn't
duplicate it, and a simulated capture failure doesn't block settlement.
Backfilling CLV for already-settled historical picks (the 13 in the audit
window, and any earlier ones) was not attempted here — those already-final
games have odds history in `player_prop_odds` if it's still retained, but
running that backfill is a separate, explicit action for the operator to
request.

---

## Priority 5 — "Today's Research" timezone bug (real bug, fixed)

**Root cause:** both `src/customer_view.py`'s `research` query and
`database.db_manager.get_research_picks_today()` filtered with
`date(scan_timestamp) = date('now')` — a UTC calendar day. The product's
own configured timezone (`MLB_TIMEZONE`/`MLB_SCHEDULER_TIMEZONE`, default
`America/New_York`) implies "today" should mean the Eastern calendar day.
Any viewer between roughly 8:00 PM and midnight Eastern would see the list
computed against tomorrow's (mostly empty) UTC date instead of the rest of
today's real research picks.

**Fix:** added `database.db_manager.get_today_in_configured_timezone()`
(reads `MLB_SCHEDULER_TIMEZONE`/`MLB_TIMEZONE`, same env vars and default
`src/worker.py` already uses for scheduling) and swapped both call sites'
`date('now')` for a bound parameter using this value. **Scoped
deliberately narrow**: every other `date('now')` in the codebase — the
Official-pick daily cap (Priority 2), scan freshness, `league_health.py`,
`tracker.py`, `control_panel.py`'s admin views — is untouched and stays on
the UTC-day boundary it already used, per instruction not to alter
Official Picks behavior.

Verified with 5 new tests (`tests/test_research_timezone.py`): the exact
UTC-midnight boundary case (2026-09-06 02:30 UTC = 2026-09-05 22:30 ET),
a non-boundary sanity check, both env-var overrides, and an integration
test proving `get_research_picks_today` actually uses the new helper.

---

## Test results

Targeted: 6 new test files/classes, all passing (see each section above).
Full suite: see the session's final report for the exact pass count and
any pre-existing/unrelated findings.

## Files changed

- `database/db_manager.py` — `get_official_picks_today` (league filter +
  player_id/event_id columns), `get_today_in_configured_timezone` (new),
  `get_research_picks_today` (uses the new timezone helper).
- `src/daily_pipeline.py` — `_stage_freeze`'s official-pick ranking call
  now league-scoped and cross-run-aware.
- `src/automatic_grading.py` — `_capture_final_closing_price` wired into
  both grading functions' settle paths.
- `src/qualification_funnel.py` — `classify_market_data_status`,
  `build_market_coverage_report`, `MARKET_STATUS_LABELS` (new).
- `src/control_panel.py` — Market Intelligence tab: Status column +
  zero-data registry rows.
- `src/customer_view.py` — research query uses the new timezone helper.
- Tests: `tests/test_phase15_official_picks.py`,
  `tests/test_qualification_funnel.py`, `tests/test_research_market_inventory.py`,
  `tests/test_customer_view.py`, `tests/test_automatic_grading.py`,
  `tests/test_research_timezone.py` (new).

## Deploy

All 5 fixes touch code paths that only run in the Render worker/pipeline
(`daily_pipeline.py`, `automatic_grading.py`) and the two Streamlit
dashboards (`control_panel.py`, `customer_view.py`) — a deploy is required
for any of this to take effect in production; nothing here is
config-only. Not deployed as part of this session — commit pushed,
awaiting the operator's manual Render deploy.
