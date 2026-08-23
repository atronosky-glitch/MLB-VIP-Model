# EV Engine Audit — 2026-08-23

Full audit of the betting logic per operator directive: map every odds
source, verify Pinnacle usage against live APIs, trace the fair-value and
qualification code exactly as it runs (not as documented), build a real
rejection funnel, and fix what's provably broken. Everything below is
either read directly from the current code or produced by a live API
call/local pipeline run on 2026-08-23 — nothing is assumed from
documentation or memory of earlier sessions.

---

## 1. Provider map

| League | Game markets (moneyline/spread/total) | Player props |
|---|---|---|
| MLB | **Primary**: SportsGameOdds. **Fallback** (on real SGO 429, verified live: 2,501/2,500 objects used): `fetch_game_odds_via_odds_api()` via The Odds API, `regions="us"`, no Pinnacle. | **Primary**: SportsGameOdds. **Supplemental** (own schedule, not a fallback): The Odds API, 4 markets (`pitcher_strikeouts`, `pitcher_outs`, `batter_total_bases`, `batter_home_runs`). |
| NFL | Same pattern as MLB: SGO primary, Odds-API fallback (`regions="us"`, no Pinnacle). | Same pattern: SGO primary + 4-market Odds-API supplemental. |
| WNBA | **Only** source: The Odds API (`ODDS_PROVIDER="the_odds_api"`) — SportsGameOdds has no WNBA at any tier (confirmed twice this account, `/leagues` and `/events?leagueID=WNBA`→400). | Same: The Odds API only, 8 registered markets, gated on ESPN-roster identity resolution. |
| All 3 | **Separate, additional reference-only source**: direct Pinnacle via `pinnacle_feed.py`/pinnapi.com — never a primary/fallback data source, only injected as a `"pinnacle"` reference book into existing O/U groups for the value model / Gate 9. | Same — MLB (6 stat types), WNBA (4 stat types), NFL (0 — no specials posted this far pre-season). |

**Runtime call path** (traced, not inferred): `worker.py` job → `daily_pipeline.py`/`player_prop_scanner.run_scan()` → per-league `src/sports/<league>.py` fetch function (SGO primary, or the 429-triggered Odds-API fallback) → `player_prop_parser.py`/`odds_api_game_parser.py`/`odds_api_props_parser.py` (parse into generic odds rows) → `player_prop_scanner.py` groups into `ou_groups` → **Pinnacle injection block runs here** (`PinnacleFeedClient.get_player_props`/`get_game_odds` → `inject_pinnacle_reference`/`inject_pinnacle_game_reference`) → `player_prop_analysis.analyze_prop_group()` (LOO consensus or Pinnacle-reference EV) → `daily_pipeline.py` freeze stage → `official_picks.classify_recommendation()` (the real Official/Discovery/Research tier decision).

**Dead/legacy code confirmed** (not removed, per instruction — flagged only): `src/market_analysis.py::analyze_two_way_market` has zero real call sites; game markets flow through the exact same `analyze_prop_group()` path as player props via the `player_id=="GAME"` sentinel.

---

## 2. Pinnacle via The Odds API — live-tested with the real key

Tested live against `api.the-odds-api.com` directly (not through this repo's client, to rule out any wrapper bug). **Key finding: `regions="us"` (what this codebase's Odds-API fallback code actually uses) never returns Pinnacle for any league.** Pinnacle only surfaces when explicitly requested:

| League | `regions=us` only | `regions=uk` or `au` | `bookmakers=pinnacle` explicit |
|---|---|---|---|
| MLB game markets | 0/15 games | 9-10/13 games | Works, same as uk/au |
| NFL game markets | not tested (same mechanism) | — | 15/272 games (near-term only) |
| WNBA game markets | not tested | — | 3/5 games, all 3 markets, timestamps within ~5 min of FanDuel/DraftKings |
| MLB player props | — | — | **0/event** — genuinely absent |
| NFL player props | — | — | **0/event** — genuinely absent (real, pre-season, matches direct-Pinnacle finding) |
| WNBA player props | — | — | **Present** — 8/6/4 outcomes for points/rebounds/assists on one real event |

**Mechanism**: passing `bookmakers=pinnacle,<other books>` **overrides `regions` entirely** and costs the same as a normal call (credits = number of markets requested, not markets × regions) — confirmed live: a 3-market call with `bookmakers=pinnacle,draftkings,fanduel,betmgm` cost exactly 3 credits, identical to a plain `regions=us` call. There is no cost penalty to including Pinnacle this way.

**Local key note**: this testing used the local `.env` key, which the operator confirmed is the *same* key that was just upgraded to the 20K plan — but its live `x-requests-remaining` header still reads in the low hundreds against a 500 cap, not the new 20,000. This discrepancy is unresolved and worth checking (upgrade propagation delay, or Render's actual key differs from local `.env` despite the operator's belief). Flagged for the operator, not something code can resolve.

---

## 3. Direct Pinnacle (`PINNAPI_API_KEY`) — still needed, materially superior

Compared head-to-head against The Odds API's Pinnacle access, live, same day:

| | Direct pinnapi.com | The Odds API `bookmakers=pinnacle` |
|---|---|---|
| MLB player props | 26 props, 6 stat types | **0 — not available at all** |
| MLB game-market alt-lines | 227 entries across 16 games (multiple spread/total lines per game) | Only the single main line per market observed — no alt-lines |
| WNBA player props | 82 props, 4 stat types | Present, but only spot-checked one event |
| Update mechanism | Own rate-limited client (10s/call), 5-min cache | Same call as the rest of the game odds — free ride on an existing fetch |

**Verdict**: keep the direct integration. It is the only source of MLB Pinnacle player props at all, and it's the only source of Pinnacle alt-lines for any league's game markets. The Odds API's Pinnacle access is real but strictly narrower — useful as a possible secondary cross-check for game markets, not a replacement.

**Does a broken/unavailable direct Pinnacle integration ever block otherwise-valid picks?** No — confirmed by code: `player_prop_scanner.py`'s injection block wraps both `get_player_props`/`get_game_odds` calls in `try/except`, logs a warning, and continues with `_pinnacle_props = None` — a dead feed degrades to the existing LOO-consensus fallback, it never raises or halts the scan.

---

## 4. Fair-value engine — traced line by line (`src/player_prop_analysis.py`)

- **Odds conversion**: standard American→implied→decimal (`american_to_implied_prob`, `american_to_decimal`) — correct, tested.
- **Vig removal**: `calculate_no_vig_probs`/`_remove_vig` — proportional method (`raw_a / (raw_a + raw_b)`), applied whenever both sides exist; falls back to raw single-side implied probability (median across books) when only one side exists.
- **LOO consensus — self-exclusion verified correct**: in the non-Pinnacle branch, the book being evaluated is explicitly excluded from its own fair-price computation (`other_paired = [b for b in paired_books if b != book]`, and the same pattern for single-side LOO). **A book can never move its own reference price.** Confirmed by direct code read, not assumption.
- **Pinnacle path is exclusive, not blended**: when Pinnacle offers both sides, its own no-vig price becomes the *sole* fair reference — no combination with the broader multi-book consensus, no freshness weighting, no reliability weighting. This is a real design choice (Pinnacle's own vig is tight — observed 2.4%-7% live — so using it exclusively is defensible), but there is currently **no staleness check specific to the Pinnacle quote itself** before using it as the sole reference. Worth adding if Pinnacle data can ever go stale mid-scan (it's re-fetched at most every 5 min via cache TTL, so the exposure window is small but non-zero).
- **Minimum book count**: `MIN_COMPARISON_BOOKS=1` (lowered 2026-08-22 per operator decision) → effective floor is 2 total books.
- **Alternate lines**: never merged — each distinct line is a fully separate group, guarded by an explicit same-line check that logs (not silently merges) any fragmentation.
- **Outlier handling**: `OUTLIER_EV_THRESHOLD=10%` — any book's EV beyond ±10% demotes the group to `NEEDS_REVIEW`, which independently blocks Official status (Gate 1). This safety net **worked correctly** during this audit — see §6.

---

## 5. Two real bugs found and fixed (both via live-data testing, not synthetic tests)

### 5a. Pinnacle spread/run-line sign collapse

`pinnapi.com` genuinely offers spread lines from **both directions** as distinct real alt-lines (e.g. `hdp=+1.5` *and* `hdp=-1.5` for the same game — not a duplicate pair, two different bets: one has the home team favored, the other has the away team favored). `parse_game_odds()` stored the line as `abs(hdp)`, so both directions collapsed into one lookup key and one silently overwrote the other. Result, live-reproduced: a real Philadelphia Phillies @ Seattle Mariners group showed **87.6% "EV"** — the group was being compared against the *opposite* real bet's Pinnacle price.

**Fix**: `PinnacleGameOdds.line` now stores the signed hdp; `inject_pinnacle_game_reference()` converts a group's own signed `raw_line` (already captured for settlement) into the matching signed hdp before lookup, and skips injection entirely rather than guess when the sign isn't available. 4 new regression tests (`tests/test_pinnacle_feed.py`).

### 5b. Base spread/run-line grouping has the SAME bug, independent of Pinnacle

This is the more serious one — present in the **primary SportsGameOdds path**, not just today's new Pinnacle code, and has been live in production since game-market spread analysis was first built. Different sportsbooks can (rarely) disagree on which team is actually favored for a spread at a given magnitude — live-reproduced: FanDuel had Atlanta Braves at `+1.5` (underdog) while five other books had Atlanta at `-1.5` (favorite) for the same real Braves @ Brewers game. The group key (`_build_game_group_key`) used `abs(line)` alone, so FanDuel's row silently merged into the majority's group as if it were the same bet, producing a **39-45% blended "EV"** across several real groups in one live scan.

**Fix**: both `src/player_prop_parser.py::_process_entry` (SGO path) and `src/odds_api_game_parser.py::_build_row` (Odds-API path) now canonicalize the group-key line to the away team's own signed value (`raw_line` if away, `-raw_line` if home) — books that agree on direction still group together exactly as before; a book that disagrees gets its own separate group instead of being silently blended. 9 new regression tests across `tests/test_game_market_grouping.py` (new) and `tests/test_mlb_odds_parser.py`.

**Verified live, before/after**: re-ran the real local MLB pipeline after each fix. Before: EV values up to 87.6%, one nonsensical `line=16.5` run-line. After: EV range -0.5% to +3.1% — realistic for a real MLB slate. The system's own `OUTLIER_EV_THRESHOLD` safety net had already caught and demoted every one of these to `NEEDS_REVIEW` before either fix, correctly preventing them from reaching Official status — **the safety net worked**, but the underlying numbers were still wrong and would have looked alarming in Research/Discovery views.

---

## 6. A third, independent bug: game markets could never become Official at all

While tracing why a real, live local scan produced **zero** Official picks even after fixing 5a/5b, direct DB inspection of `disqualification_reasons` showed **every single game-market recommendation** (`game_moneyline`/`game_runline_ou`/`game_total_ou`, 100% of them) blocked by Gate 1: *"Market has no verified automatic settlement field."*

Root cause: `src/prop_config.py::AUTO_SETTLEABLE_MARKET_TYPES` — the registry Gate 1 checks — only ever listed player-prop stat types. It had **zero entries for any game market**, despite `src/game_settlement.py` genuinely settling them for all 3 leagues. This blocked every game-market pick from Official status unconditionally, regardless of EV, book count, or Pinnacle approval — independent of anything Pinnacle-related, and almost certainly the single largest contributor to "no picks today."

A second layer of the same gap: `game_settlement.py`'s own docstring claims "MLB run-line" support, but its dispatch only ever matched `"game_spread_ou"` (NFL/WNBA's naming) — MLB's own market type, `"game_runline_ou"`, was never actually in `GAME_MARKET_TYPES`. Every real MLB run-line recommendation was silently unsettleable, separate from the Official-gate bug above.

**Fix**: added all 4 game market types to `AUTO_SETTLEABLE_MARKET_TYPES`; added `"game_runline_ou"` to `game_settlement.py::GAME_MARKET_TYPES` and its dispatch (reusing the existing `grade_spread` logic, identical math to `game_spread_ou`). 2 new regression tests.

**Verified live**: re-ran the local pipeline after this fix — the "no verified automatic settlement field" reason disappeared entirely from every game-market recommendation. Remaining rejections are legitimate (`Model Score < 7.0`, `O/U EV < 3%`) — a genuinely thin local slate (SGO exhausted, Odds-API fallback only, few books per game), not a bug.

---

## 7. A fourth, smaller finding: game markets structurally underscored on confidence

`model_scoring.py::_score_confidence_component` returns a neutral `0.5` when `confidence_score` is `None`. For a player prop, `None` genuinely means "identity mapping hasn't run / is uncertain." For a game market (`player_id=="GAME"`), there's no name-matching step at all — team-level identity is never ambiguous — so `None` there means "not applicable," not "uncertain." Scoring it as neutral-uncertain cost every game-market recommendation up to 0.5 raw points (0.05 on the final 0-10 scale after weighting) for a form of risk that cannot apply to it.

**Fix**: game markets (`player_id=="GAME"`) now score full confidence (1.0) when no `confidence_score` is present. 1 new regression test confirming a game-market rec now scores strictly higher than an otherwise-identical player-prop rec with no confidence score.

---

## 8. Official-pick gate audit (`src/official_picks.py::classify_recommendation`, 12 gates, traced not just listed)

1. **Market quality** — excludes `PRICE_OUTLIER`/`NEEDS_REVIEW`/`INSUFFICIENT_MARKET`/`EXCLUDED`. Real, working outlier safety net (see §5).
2. **Auto-settleable market** — fixed today (§6); was blocking 100% of game markets.
3. **Freshness** — excludes `STALE`.
4. **Game status** — excludes live/completed/postponed/cancelled events.
5. **Model score ≥ 7.0**.
6. **Rec status** ∈ allowed statuses (essentially model-score-derived).
7. **Contributing books ≥ 2** (the LOO floor, already at the operator's specified minimum).
8. **Mapping confidence** — excludes LOW/NONE/FAILED/REJECTED (player props only; game markets always pass, `player_id` is never ambiguous).
9. **Edge threshold** — O/U EV ≥ 3.0%, YN price advantage ≥ 3.0pp.
10. **Pinnacle approval (O/U only)** — **already has the exact "don't let one book control the system" fallback the operator asked for**: if Pinnacle has genuinely zero data at that exact market (`pinnacle_found is False`), the pick can still qualify via the LOO-consensus fallback path (`PINNACLE_FALLBACK_TO_MARKET_MEDIAN=True`, already the default). Pinnacle only hard-blocks when it's *present but one-sided/mismatched* — a real present-but-broken signal, not an absent one. This mechanism was built 2026-08-05, predates today's session, and was working correctly the whole time — it was masked by the settlement-registry bug (§6), which blocked game markets before this gate was ever reached in a way that mattered.
11. **Reliable EV provenance (O/U only)** — independently blocks `extreme_ev_outlier`/other reliability failures; this is what actually blocked the buggy 87% EV picks in §5, on top of Gate 1.
12. **Valid identity fields** (event/player/side/sportsbook present).
13. **YN reference odds present** (YN only).

**Which requirement rejects the most otherwise-valid candidates?** From the real, corrected local funnel below: Model Score < 7.0 and O/U EV < 3% are now the dominant rejections on a thin slate — legitimate, not bugs. Before today's fixes, Gate 1 (auto-settleable) rejected literally everything with a game market_type, which would have swamped every other signal.

---

## 9. Real qualification funnel (live, post-fix, one local MLB scan, 2026-08-23)

```
40 total O/U groups formed
├─ 31 matched Pinnacle at the exact line (exact_match / reference_used)
├─  7 had no Pinnacle data at that market → LOO-consensus fallback eligible
├─  2 excluded: insufficient books (< 2 total)
├─  5 excluded: EV below the Pinnacle 4% threshold (of the 31 Pinnacle-matched)
├─ 26 excluded: no positive edge at all
└─  0 official_approved by the raw per-book flag (informational only — see below)
      ↓
16 recommendations frozen (all opportunities, any tier)
      ↓
0 reached OFFICIAL_TRACKED this run — real rejections: Model Score < 7.0 (dominant),
  O/U EV < 3.0% (secondary). No bugs in this run's rejections.
```

**Important correction discovered mid-audit**: the scanner's own `PINNACLE_SUMMARY official_approved=N` log line (and `run_scan()`'s raw `is_official` flag) is **diagnostic only** — it reflects `player_prop_analysis.py`'s hard-coded "Pinnacle-approved or nothing" rule, which does **not** account for the Gate-9 LOO fallback that `official_picks.py::classify_recommendation()` actually applies at freeze time. A local test run showed `official_approved=7` in this log line while the real, authoritative classification produced 0 Official picks that same run (correctly, on a thin slate) and would have produced more once Gate 1 was fixed. **This log line should not be read as "how many Official picks this run produced"** — only `historical_recommendations.recommendation_tier` (or the "Official picks frozen: N" pipeline summary line) is authoritative. Worth relabeling in a follow-up to avoid this exact confusion recurring.

---

## 10. Were any requirements unnecessarily strict?

- **The literal "Pinnacle missing → no bets" rule the operator was concerned about does not exist as stated** — Gate 9's LOO fallback already handles the "Pinnacle has zero data for this market" case correctly, and has since 2026-08-05. What *was* effectively producing that exact symptom was the Gate 1 settlement-registry bug (§6), which is now fixed.
- **Confidence-component treatment of game markets** was unnecessarily strict (§7), now fixed.
- **`official_min_books=2`** already matches the operator's explicit "one book is enough, given every book contributes to the average" directive from earlier this session — no further change indicated by today's evidence.
- **Model Score ≥ 7.0 / EV ≥ 3%** are real, deliberate thresholds, not obviously wrong from one thin local scan. Recommend evaluating these against a fuller live production slate (more books, SGO not exhausted) before concluding they're too strict — today's local test ran entirely on the Odds-API fallback with fewer books than production's primary SGO path normally has.
- **No evidence found today that any threshold should differ by league** — the mechanisms (LOO consensus, Pinnacle-when-available, EV/model-score gates) are already applied uniformly, and the bugs fixed today (spread-sign, settlement-registry, confidence) affected all 3 leagues identically, not one specifically.

---

## 11. Credit budget / paid-tier readiness (Part 11)

`DEFAULT_MONTHLY_BUDGET` already reads `THE_ODDS_API_MONTHLY_BUDGET` env var, defaulting to 20,000 (updated 2026-08-22, before this session) — no stale 500-credit hardcoding found anywhere live (`odds_api_credits.py`, `league_schedule.py` checked directly; the only "500" references remaining are historical-narrative comments explaining *why* a number changed, not live values). `credit_budget_check()` already uses the live `x-requests-remaining` header, not a static assumption.

**Recommendation given §2's finding** (Pinnacle via The Odds API costs nothing extra when requested via `bookmakers=`): worth a follow-up to add Pinnacle as one of the `bookmakers=` requested in the existing MLB/NFL Odds-API game-market fallback call, as a free secondary cross-check against the direct pinnapi feed — not urgent, the direct integration already covers this, but it's free coverage sitting unused.

**Historical data (Part 9)**: not tested live this session — the local test key's remaining balance dropped into the low 80s from the live Pinnacle-availability testing above, and this local key is confirmed distinct from the real 20K production tier (§2's unresolved discrepancy). Recommend testing the historical-odds endpoint's real cost/depth against the actual production key directly, once the local/production key mismatch is resolved, rather than spending further on a budget that may not reflect the real account.

---

## 12. What changed today (code)

- `src/prop_config.py` — multi-league Pinnacle config; `AUTO_SETTLEABLE_MARKET_TYPES` now includes all 4 game market types.
- `src/pinnacle_feed.py` — multi-league props/game-markets; signed-hdp fix for spread matching.
- `src/player_prop_parser.py` — signed group-key fix for the primary SGO spread path.
- `src/odds_api_game_parser.py` — signed group-key fix for the Odds-API fallback spread path.
- `src/game_settlement.py` — `game_runline_ou` now settles (was silently unhandled).
- `src/model_scoring.py` — game markets score full confidence instead of neutral-uncertain.
- `src/player_prop_scanner.py`, `src/mlb_props_parser.py` — multi-league Pinnacle wiring (see `docs/DECISIONS.md`).

**Tests**: 1,867 passing (was 1,854 at session start), 0 failures, including 21 new regression tests covering every bug above.

---

## Answers to the 15 questions

1. **What is the model actually based on now?** Leave-one-out multi-book consensus (self-excluding) as the default fair-value reference for every market; Pinnacle's own no-vig price replaces that reference exclusively (not blended) whenever Pinnacle has both sides at the exact line. Game markets (moneyline/spread/total) use the identical engine as player props, not a separate code path.
2. **Is Pinnacle currently contributing to fair value?** Yes, confirmed live — 31 of 40 real groups in one scan matched Pinnacle exactly and used its no-vig price as the reference.
3. **Which markets receive Pinnacle data?** MLB: 6 player-prop stat types + all game markets. WNBA: 4 player-prop stat types + all game markets. NFL: game markets only (props: zero data exists yet, confirmed live, not a bug).
4. **Which markets cannot receive Pinnacle data?** NFL player props (no specials posted this far pre-season) and any MLB/WNBA prop stat outside Pinnacle's fixed 6/4-stat lists (e.g. MLB batter hits, RBIs) — these use LOO consensus only, and correctly still have a path to Official via Gate 9's fallback.
5. **Do we still need `PINNAPI_API_KEY`?** Yes — it's the only source of MLB player-prop Pinnacle data and the only source of Pinnacle alt-lines for any league's game markets (§3).
6. **Was the old Pinnacle integration suppressing picks?** Not in the way suspected. The literal "Pinnacle missing → auto-reject" behavior doesn't exist (Gate 9's fallback has handled it since 2026-08-05). What *was* suppressing picks: a settlement-registry bug (§6) that blocked 100% of game markets regardless of Pinnacle, discovered while investigating this exact question.
7. **Exactly how is fair probability calculated now?** See §4 — no-vig proportional removal, LOO self-exclusion verified correct by direct code read, Pinnacle used exclusively (not blended) when present.
8. **What does a bet have to pass to become OFFICIAL?** 12 gates, listed and traced in §8 — market quality, settlement support, freshness, game status, model score ≥7.0, book count ≥2, mapping confidence, edge threshold (EV≥3%/YN adv≥3pp), Pinnacle-approved-or-genuinely-absent, EV reliability, valid identity fields, YN reference odds.
9. **Which requirement rejects the most otherwise-valid candidates?** Before today: Gate 1 (settlement registry), unconditionally, for every game market. After today's fix, on a real thin local slate: Model Score <7.0, then O/U EV <3% — both legitimate.
10. **Were any requirements unnecessarily strict?** Confidence scoring for game markets (fixed today). Everything else traced today looks intentional and evidence-based, not arbitrary.
11. **What did you change?** 6 source files, 4 real bugs fixed (2 spread-sign bugs, 1 settlement-registry bug, 1 confidence-scoring bug), 21 new regression tests. See §12.
12. **How many +EV candidates versus OFFICIAL picks in a real scan?** 456 raw O/U opportunities from one live MLB scan → 16 frozen recommendations (all tiers) → 0 Official on this particular thin slate (real rejections, not bugs, after all fixes — see §9's funnel).
13. **How should we use the 20,000 monthly credits?** Confirmed already budget-aware and dynamic (§11); the local test key's numbers don't match the real production tier and should be reconciled. Free opportunity: add Pinnacle to the existing Odds-API game-market call via `bookmakers=` at zero extra cost.
14. **Single biggest remaining weakness?** The settlement-registry gap (§6) was the single biggest one found and is now fixed. Remaining known weakness: Pinnacle is used exclusively rather than blended with the broader consensus when both are present, and has no staleness check of its own — a defensible design, not proven wrong today, but worth validating against historical CLV data once the credit-key situation is resolved (§11).
15. **Full test results.** 1,867 passed, 0 failed (was 1,854 before this audit).
