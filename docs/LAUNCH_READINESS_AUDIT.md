# Launch Readiness Audit — 2026-09-24

Scope: can real, free customers sign up with email, use the model, optionally
connect Kalshi/Polymarket, with the backend running unattended? Evidence is
from code, the test suite (offline), read-only production queries, real
read-only provider probes, and a real Streamlit run. **No real-money order was
placed and no live flag was touched.** Production DB access was read-only.

Companion: `python -m src.production_readiness` (read-only PASS/WARN/FAIL).

## 1. Verdict

**READY WITH WARNINGS — after the manual items in §12 are done.** The code-level
launch blockers found in this audit are fixed (§3). Three operational
conditions outside the code can still hurt customers on day one (§12 items
1-3): the odds-provider quotas, the admin password, and a redeploy so the fixes
actually reach production.

## 2. System map (verified against code)

```
CUSTOMER  visitor -> [free access: picks visible] -> sign-up/login (bcrypt, server-side session,
          opaque cookie) -> dashboard -> EV Picks / Arbitrage / Middling -> history (official_picks
          JOIN settled) -> Auto-Bet (per-user encrypted creds) -> My Performance (own rows only)
MODEL     schedule -> odds (SportsGameOdds; Odds API fallback/props) -> normalize/validate ->
          group -> LOO/Pinnacle fair prob -> EV -> qualification (official_picks rules) ->
          historical_recommendations + official_picks -> site / Discord / Auto-Bet
RESULTS   worker grading job -> MLB StatsAPI / ESPN results -> grade -> market_settlements +
          bet_units -> official_picks.outcome -> history / stats
AUTO-BET  official pick (ACTIVE, game not started) -> user eligibility -> exact Kalshi/Polymarket
          mapping -> order book -> risk -> hybrid execute (PAPER default; LIVE gates OFF)
          -> reconcile (Kalshi fills) -> My Performance
WORKER    python -m src.worker: init schema -> heartbeat thread -> loop {stale-job recovery,
          pending jobs, schedulers per league, arb/middle, Auto-Bet, backups(no-op on PG)}
```
Single source of truth for "official": the ACTIVE row of `official_picks`. Site
history, Discord alerts (fixed this audit), Auto-Bet (fixed), settlement and
stats all use it. The site's "Full Board" is explicitly labeled non-official.

## 3. Findings fixed in this audit (all with tests)

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| 1 | P0 | Operator dashboard had **no authentication** (scan controls, overrides, live-approval panel) | `src/admin_auth.py`: `MLB_ADMIN_PASSWORD`, fails closed in production, constant-time compare, process-wide lockout |
| 2 | P0 | **Settlement stalled ~46h** (prod): grading jobs only created for games the (quota-exhausted) odds provider marked final | `schedule_catchup_grading`: time-based, provider-independent |
| 3 | P0 | **Retry storm**: morning-run queued every minute 8PM-midnight ET (~360 failed runs/3 days); dedup compared a local date to UTC timestamps | local-day UTC window; failed-scan cooldown for NFL/CFB |
| 4 | P0 | New visitors had **no way to create an account** in the default free-access mode (auth form only rendered when accounts required) | sign-up/login/forgot-password expander always available |
| 5 | P0 | Customer page **crashed for everyone** if one settled pick had no `bet_units` row (NULL units) | None-safe sums in `grading.py`, `adaptive_learning.py` |
| 6 | P0 | Discord alerts, customer Auto-Bet and live-scan used **any actionable recommendation**, not official picks | official-only + game-not-started filters; second start-time guard in Auto-Bet dispatcher |
| 7 | P1 | Auth gaps: no rate limits/lockout, no disabled accounts, no password reset, verification tokens never expired, bcrypt 72-byte truncation, timing side channel, signup race | hardened `customer_accounts.py`; DB-backed `auth_rate_events` |
| 8 | P1 | Server-side validation missing on Auto-Bet risk settings/credentials (only UI widget bounds); credential verification unbounded | `src/autobet_validation.py`; per-account rate limits on verify and approvals |
| 9 | P1 | Postgres statement splitter was still a naive `split(";")` (cause of the earlier crash-loop) | comment/string/dollar-quote aware splitter; real `init_db` DDL parsed with PostgreSQL's grammar (pglast) in tests |
| 10 | P1 | Worker: long jobs starved the heartbeat; one dropped/aborted DB connection would fail every loop until restart; no per-job telemetry | heartbeat thread, `_recover_connection`, `JOB_START/JOB_END` logs with RSS before/after |
| 11 | P1 | SGO quota-429 retried 3x (wasteful); failure detail could embed API keys | quota-aware 429; `PIPELINE_FAILURE` sanitized diagnostics persisted on `scan_runs` |
| 12 | P2 | Discord sender retried permanent 403/404 | fail fast |
| 13 | P2 | No health check on web services; no legal/risk/support basics in footer | `healthCheckPath`; footer disclosures + `MLB_SUPPORT_EMAIL` |

## 4. Root causes found from real data

* **NFL exit 3 (previously unexplained) — now identified.** Dry-run of the fetch stage:
  `provider=the-odds-api (fallback after SportsGameOdds 429) ... NFL SportsGameOdds fallback skipped
  — Odds API budget exhausted: only 1485 credits remaining, 2000 held in reserve`. SportsGameOdds is
  out of monthly quota (2503/2500) **and** the Odds API credit reserve blocks the fallback. Account
  situation, not a code bug. MLB's fallback did work in the same dry-run (12 games).
* **MLB official picks stopped 2026-09-20** (newest official pick). Same cause.
* **MLB record**: 163 settled official picks, 51-112, -8.4u on 175.3u risked (-4.8% ROI). Calibration is
  good (mean model probability 30.3% vs actual 31.3% win rate), but the sample cannot yet demonstrate
  the claimed ~+9% average EV. **Do not market it as proven profitable.**

## 5. Sports readiness

| | MLB | NFL | WNBA | NCAAF (CFB) |
|---|---|---|---|---|
| Schedule / odds | SGO (quota exhausted) + Odds API fallback (credits low) | same | Odds API | SGO/Odds API |
| Markets in production | props (HR, total bases, hits, RBI, K, outs, ...), moneyline, total, run line | game total (2 picks); moneyline/spread research only | spread, total, points/rebounds/assists/PRA | committed adapter: 5 game markets |
| Official picks (prod) | 178 (163 settled) | 2 (0 settled) | 15 (5 settled) | recs exist (spread), no official picks |
| Settlement | MLB StatsAPI | ESPN | ESPN | ESPN (cfb_results) |
| Discord / site / Auto-Bet | yes (official-only) | yes | yes | site shows NCAAF label |
| Auto-Bet on Kalshi/Polymarket | moneyline/run line/total exact-mapped | spread/total only (moneyline blocked: tie unverified) | moneyline/spread/total | **unsupported** |
| Status | **Degraded: no fresh picks since 09-20 (provider quota)** | **Degraded (same)** | **Working** | **UNFINISHED**: working-tree CFB changes are uncommitted and change the capability test (11 vs 5 markets); committed HEAD passes 26/26 |

The customer hero/footer advertise "NCAAF"; there are no official NCAAF picks. Either finish/enable it or
drop the label (P1, marketing accuracy).

## 6. Worker job inventory (dispatch table in `src/worker.py::_execute_job`; prod evidence = last 3 days)

| Job | Sport | Cadence / trigger | Purpose | Failure behavior | Prod evidence |
|---|---|---|---|---|---|
| morning-run | MLB | daily from 8:30 ET, retry ≥60 min after a failure | full pipeline | failed job; retry after cooldown | **failing** (provider quota); storm fixed |
| pregame-check | MLB | per-game, T-60 min | refresh pregame | per job | no rows (depends on `games`) |
| morning-run-nfl / pregame-check-nfl | NFL | game days, 30-min check, 60-min failure cooldown (new) | NFL pipeline | failed job | **failing** (quota + credit reserve) |
| morning-run-cfb / pregame-check-cfb | NCAAF | game days | CFB pipeline | failed job | morning-run failing; pregame ok 09-21 |
| wnba-odds-scan / wnba-props-scan | WNBA | credit-aware, 20-min check | odds/props | credit gate | odds-scan **completed** (31) |
| mlb-props-scan / nfl-props-scan | MLB/NFL | credit-aware | supplemental props | credit gate | **no runs in 3 days** (credits below reserve) |
| arb-middle-scan | all | every 15 min | arbitrage/middles + Discord | isolated | completed (259) |
| customer-autobet-scan | all | every 5 min | Auto-Bet dispatch + Kalshi reconcile | isolated per customer/platform | completed (538) |
| grading | all | game-final trigger **+ time-based catch-up (new)** | results + grading | isolated per league | none in 3 days (bug, fixed) |
| daily-results-summary | all | 11 PM ET | Discord summary | cooldown | completed (2) |
| backup | all | 3:30 AM ET | SQLite backup | **skipped on PostgreSQL** | n/a |
| adaptive-learning / health-check / schedule-refresh / test-mlb-discord / replay-arbitrage-delivery | — | hourly inline / manual | learning, health, manual tools | swallowed | inline |

## 7. Render services (from `render.yaml`)

| Service | Type | Start | Plan | DB | Health check |
|---|---|---|---|---|---|
| mlb-vip-customer | web | `streamlit run src/customer_view.py` | starter | mlb-postgres | `/_stcore/health` (new) |
| mlb-vip-dashboard | web | `streamlit run src/control_panel.py` | starter | mlb-postgres | `/_stcore/health` (new) |
| mlb-vip-worker | worker | `python -m src.worker` | 1c-2g | mlb-postgres | heartbeat row + `production_readiness` |
| mlb-postgres | Postgres | — | basic-256mb | — | — |

Renaming services off the "mlb-" prefix is pending (do it in the Render dashboard first, then `render.yaml`).

## 8. Environment variables (secret ★)

| Variable | Service | Required | Purpose / failure behavior |
|---|---|---|---|
| DATABASE_URL ★ | all | yes | PostgreSQL. Unset in production => local SQLite (readiness FAILs) |
| MLB_ENVIRONMENT | all | yes (`production`) | enables fail-closed admin gate |
| MLB_ADMIN_PASSWORD ★ | dashboard | **yes** | dashboard refuses to render if unset in production |
| POLYMARKET_CREDENTIAL_ENCRYPTION_KEY ★ | customer + worker (identical) | yes for Auto-Bet | Fernet key; connect fails safely without it |
| SPORTSODDS_API_KEY ★ | worker, dashboard | yes | SportsGameOdds; 429 => fallback/fail |
| THE_ODDS_API_KEY ★ | worker, dashboard | yes | WNBA, props, fallback |
| PINNAPI_API_KEY ★ | worker/dashboard | yes | Pinnacle reference (official gate) |
| MLB_DISCORD_WEBHOOKS ★ / _ARB_MIDDLE ★ / _MIDDLE ★ | worker | for alerts | none => alerts skipped, claims released |
| SENDGRID_API_KEY ★, SENDGRID_FROM_EMAIL, SITE_BASE_URL | customer | for verify/reset emails | unset => accounts work, emails silently not sent (WARN) |
| MLB_SUPPORT_EMAIL | customer | optional | footer support contact |
| MLB_CUSTOMER_FREE_ACCESS | customer | optional (default true) | false => login required |
| MLB_CUSTOMER_ACCESS_TOKEN ★ | customer | optional | legacy shared-link access |
| KALSHI_AUTOBET_SERVER_MAX_ORDER_USD / POLYMARKET_US_AUTOBET_SERVER_MAX_ORDER_USD | worker | optional (default 100) | server-side hard cap on unattended orders |
| LIVE_TRADING_ENABLED, KALSHI_LIVE_ENABLED, POLYMARKET_US_LIVE_ENABLED | worker | **leave unset/false** | live execution gates (all default OFF) |
| THE_ODDS_API_MONTHLY_BUDGET | worker | optional (default 20000) | credit accounting |
| MLB_SHADOW_MODE | all | label only | not consulted by any delivery path (misleading dashboard pill; P2) |

## 9. Backups / recovery

* Production data lives in Render Postgres (`mlb-postgres`, basic-256mb). The app's backup job is a
  deliberate no-op on PostgreSQL. **Whether Render's managed backups are enabled/retained, and whether a
  restore has ever been tested, cannot be verified from code.** Treat as an open P0-operational item.
* Recovery runbook: (1) Render dashboard → mlb-postgres → Backups → restore to a NEW instance;
  (2) point `DATABASE_URL` of all three services at it; (3) redeploy; the worker re-runs `init_db`
  (idempotent) and grading catch-up; (4) run `python -m src.production_readiness`.
* Customer-critical tables: `customer_accounts`, `customer_sessions`, `customer_*_accounts` (encrypted
  creds), `customer_autobet_executions`, `official_picks`, `historical_recommendations`,
  `market_settlements`. Losing the encryption key makes stored platform credentials unrecoverable
  (customers re-connect).

## 10. Security review

* Auth: bcrypt, opaque server-side sessions (30d, deleted on logout/expiry/disable), normalized
  email + UNIQUE index, generic login failure, DB-backed rate limits, single-use expiring reset tokens
  (only a hash stored), sessions invalidated on reset. Identity comes only from the server-side
  session; no query param carries identity (tested).
* Isolation (IDOR): every customer-facing function is scoped by the session's account id (tested for
  settings, credentials/status, executions, performance, pending approvals, sessions).
* Admin: gated (finding 1).
* Secrets: `.env` is git-ignored and untracked; no secret literals in tracked files. **The
  SportsGameOdds API key value exists in two early commits (`f0dfa29`, `ed7dbbe`, in
  `tests/test_stage1.py`), present on origin/main history. Rotate it.** Confirm the GitHub repo is private.
  (The `.env` file was also read into an AI-assistant session during this work; if that transcript is
  retained outside your control, rotate the production DB password too.)
* Input validation: server-side bounds/NaN/inf/type checks on all Auto-Bet settings and credentials.
* Live gates: all default OFF; customer "live" toggle cannot override server flags (tested E2E).

## 11. Test evidence

* Focused suites for every change above; end-to-end suites: `tests/test_e2e_customer.py` (real site via
  AppTest: sign-up → login → picks → history → empty My Performance → logout → login; A/B isolation;
  admin locked), `tests/test_e2e_model_pipeline.py` (analysis → qualification → persistence → official pick
  → Discord once + restart → grading → stats), `tests/test_e2e_autobet_paper.py` (both platforms, paper,
  restart-safe, official-only, started-game guard, live-flag override attempt).
* PostgreSQL: **no PostgreSQL server was available for integration tests.** SQLite passing does not
  prove Postgres correctness. Mitigations: pglast grammar validation of the real DDL, read-only queries
  against the real production database, and the production worker/customer services already running on it.
  Recommended: run `python -m src.production_readiness` from a Render shell after deploy.

## 12. Manual actions (need you)

1. **Provider quotas (P0-operational).** SportsGameOdds: 2503/2500 monthly entities (resets on its billing
   cycle or upgrade). Odds API: ~1485 credits left, below the 2000-credit safety reserve, so the fallback
   is refused. Until fixed, MLB/NFL produce no new official picks. Decide: upgrade a plan, wait for the
   cycle, or lower `THE_ODDS_API_MONTHLY_BUDGET` reserve deliberately.
2. Set `MLB_ADMIN_PASSWORD` on **mlb-vip-dashboard** (it will show "locked" until set).
3. **Redeploy all three services** from `main` (the worker fixes are what restart settlement).
4. Rotate the SportsGameOdds key; confirm the repo is private.
5. Verify Render Postgres backups are on and test a restore (§9).
6. Set `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `SITE_BASE_URL` (verification/reset emails), and
   `MLB_SUPPORT_EMAIL`.
7. Legal review of the Privacy Policy / Terms drafts (`customer_view.py`) and the marketing-consent text
   before collecting phone numbers for marketing.
8. Decide NCAAF: finish + commit the CFB work, or remove the label from the hero/footer.

## 13. First-customer checklist

1. `python -m src.production_readiness` (from the worker shell) → no FAIL.
2. Open the site on a phone; create an account; confirm verification email arrives (if configured).
3. Confirm current official picks render (needs #12.1 fixed) and history shows recent settlements.
4. Log out/in; open My Performance (empty state); open Auto-Bet (nothing connected).
5. Confirm the dashboard asks for the admin password.

## 14. First 24 hours

* Worker logs: `DATABASE_BACKEND service=worker ...` (must be the production host), `JOB_START/JOB_END`
  lines (status, duration, `rss_before_mb`/`rss_after_mb`), `PIPELINE_FAILURE ...`, `Scheduled catch-up
  grading job`, and no `Worker loop error` repeats. Memory: watch `rss_mb` on the heartbeat vs the 2 GB plan.
* Database: `worker_heartbeat` (age < 5 min, `last_failure_*`), `market_settlements` newest `settled_at`
  advancing, `scan_runs.metadata_json.failure` for any failed scan.
* Discord: one message per new official pick; no repeats after a redeploy.
* Customers: sign-up count vs `SIGNUP_MAX_PER_HOUR` (30/h site-wide ceiling); `auth_rate_events` growth.

## 15. Open issues

**P0 (operational, not code):** provider quotas (§12.1); admin password (§12.2); redeploy (§12.3).
**P1:** NCAAF label vs reality; Postgres backups unverified; SGO key in history; email not configured;
legal review; no real-Postgres integration tests in CI (add a Postgres service to CI).
**P2:** "SHADOW MODE" pill is not enforced anywhere; `settled` history query has no LIMIT (fine at
thousands of rows, paginate if it grows); Discord message per pick could batch; rename Render services.
**P3:** Kalshi live reconciliation field names unverified on a live account (controlled live test);
Polymarket has no open game markets today; NFL moneyline Auto-Bet disabled (tie settlement unverified).
