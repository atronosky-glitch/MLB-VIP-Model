# Weather Edge

Analysis of **weather prediction markets** on Kalshi and Polymarket: it estimates outcome probabilities
from real forecast data, compares them with the **executable** market price (real order book, exchange
fees, slippage) and surfaces opportunities with positive modeled net expected value.

**Scope: analysis, alerts and paper trading only.** Nothing in `src/weather_edge/` places an order, holds a
trading credential, or calls any non-GET endpoint on a market platform. A test
(`tests/test_weather_edge_ui_ops.py::TestSafetyInvariants`) enforces this.

All probabilities and EV figures are **model estimates**. They can be wrong. Nothing here is a guarantee of
profit, and no track record exists until the system has recorded and settled its own predictions.

## What is modeled (and what is not)

| Market | Status |
|---|---|
| Daily **high / low temperature** (°F), Kalshi `KXHIGH*` / `KXLOW*` series | Modeled |
| Daily high / low temperature on Polymarket ("Highest/Lowest temperature in … on …?") | Modeled (informational, see "Polymarket" below) |
| Rain / snow / precipitation / monthly / hurricane / Celsius markets | **Discovered and listed as "not modeled"**, never priced |

Anything that cannot be matched to a verified settlement station, whole-degree bins and an understood fee
schedule is stored with a reason (`weather_contracts.unmodeled_reason`) and excluded from signals.

## Verified provider facts (checked against the live APIs on 2026-10-12)

**Kalshi** (public market data needs no credentials)
- Base `https://external-api.kalshi.com/trade-api/v2`; series via `GET /series?category=Climate and Weather`, markets via
  `GET /markets?series_ticker=…`, depth via `GET /markets/{ticker}/orderbook`, history via candlesticks.
- Order books list resting **bids** per side; a YES buyer pays `1 − best NO bid`.
- Fee: `ceil_to_cent(0.07 × multiplier × contracts × P × (1 − P))` (series metadata `fee_type: quadratic`).
- Temperature markets settle on whole-degree values from **The Weather Company**, at the station named in the
  rules (e.g. `CLINYC` → Central Park). **This is not NWS/NOAA**, which is why calibration is fitted against Kalshi's
  own settled values.

**Polymarket** (public Gamma + CLOB reads need no credentials)
- Discovery `https://gamma-api.polymarket.com/events?tag_slug=weather`, books `https://clob.polymarket.com/book?token_id=`
  (levels are **not** guaranteed sorted; this code sorts them).
- Weather taker fee: `contracts × 0.05 × P × (1 − P)`, 5 decimal places, taker only (from each market's `feeSchedule`).
- Resolution: NOAA observations at the station in `resolutionSource` (NYC → LaGuardia `KLGA`).
- **Eligibility:** polymarket.com restricts trading from several regions including the United States. This module only
  *reads* public data and never attempts to bypass anything; the UI shows Polymarket's own geoblock answer and labels
  Polymarket prices as comparison-only for ineligible users. Check your own eligibility and the platform terms before acting.

**Kalshi NYC ≠ Polymarket NYC.** Different station (KNYC vs KLGA) and different source (The Weather Company vs NOAA).
`matching.py` only treats two contracts as equivalent when metric, date, station, source family, unit, time zone and the exact
outcome range all match, and reports the reasons when they do not.

**Forecast sources**
- Open-Meteo Ensemble API: GEFS (31 members) + ECMWF IFS (51 members), hourly 2 m temperature in the station's local time zone.
  **The free tier is licensed for non-commercial use only.** For a commercial site set `WEATHER_EDGE_OPENMETEO_KEY`
  (a paid key switches to the commercial host) or leave the module disabled.
- NWS `api.weather.gov` (requires a User-Agent; set `WEATHER_EDGE_USER_AGENT` to include a contact): hourly gridpoint
  forecast (independent cross-check) and station observations (used for same-day markets).
- Open-Meteo Previous Runs API: the forecast that was actually available N days before, used for calibration and the backtest.

## How a signal is produced

1. **Discover** open temperature contracts on both platforms; store every contract with its rules, station, source and fee model.
2. **Forecast** each (station, metric, local date) once: per-member daily extreme over the station's local calendar day,
   plus the NWS deterministic value. Same-day markets fold in the observed high/low so far, or are skipped if observations are unavailable.
3. **Probability**: ensemble members are shifted by the calibrated bias and smoothed so the spread matches the *measured* forecast
   error for that station/metric/lead (never narrower than the data supports). Bin probability = CDF(hi + 0.5) − CDF(lo − 0.5) on
   whole degrees, so every outcome of an event comes from one distribution and the set sums to 1.
4. **Robustness**: the probability is re-evaluated with the bias off by ±1 standard error and the error spread ±15%; the worst case is the "robust" probability.
5. **Executable price**: top-of-book screen first, then the real order book for candidates. The paper stake walks the book
   (`src/execution/orderbook_math.walk_book`), so slippage and partial fills are real. Fees are added.
6. **EV** per $1-payout contract: `net EV = p_win − (cost + fee)/contracts`; `net EV % = net EV / cost per contract`. NO positions use `1 − p` and the NO-side asks. Break-even probability = cost per contract. Last-trade price is never used.
7. **Gates** (all must pass, reasons are stored for rejections): market open; quote fresh; forecast fresh; **station calibrated** (≥ 30 settled samples on that platform); depth known; fully fillable; entry price within [0.05, 0.95]; net EV % ≥ threshold; robust EV > 0; spread ≤ limit; depth ≥ limit; no open paper trade on that contract; per-event exposure cap (outcomes of one event are correlated).
8. **Record**: forecast snapshot, market snapshot, prediction (before any outcome exists), signal, paper trade. All idempotent.

Confidence is a coarse label (low/medium/high) summarizing data quality (calibration sample size, source agreement, lead time, robustness). It is not a probability.

## Validation and the track record

- Predictions, prices and signals are written when made and never edited. Outcomes are attached later by `settle`.
- `settle` uses the platform's own final result; if the official value contradicts the yes/no result the contract is marked `disputed` and nothing is settled from it.
- **Performance page** shows paper P/L, win rate, ROI, drawdown, Brier score, log loss, reliability table, broken down by lead time, city, platform and market type — only from recorded rows, with a "fewer than 30 samples" warning. Paper results are simulated fills at recorded prices and are labeled as such.
- **Backtest** (`backtest` command) is a *reconstruction*: archived deterministic forecast (Previous Runs) + historical Kalshi candlestick bid/ask + Kalshi settled results, calibration fitted only on days ≤ D−2, real Kalshi fees. Limits (also printed with every result): historical ensemble members are not archived, so it tests the calibrated deterministic version of the model; candles give top-of-book only, so fills assume the stake is available. It is labeled simulated and is **not** evidence of future profit.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WEATHER_EDGE_ENABLED` | `false` | Master switch for the worker jobs and the customer-site menu entry |
| `WEATHER_EDGE_SCAN_INTERVAL_MINUTES` | `30` (min 5) | Scan cadence; also the prediction/signal de-duplication bucket (`WEATHER_EDGE_DECISION_BUCKET_MINUTES`, default 30) |
| `WEATHER_EDGE_MIN_NET_EV_PCT` | `5.0` | Minimum net EV (% of cost) for a signal |
| `WEATHER_EDGE_REQUIRE_ROBUST_EV` | `true` | Also require positive EV under the calibration stress test |
| `WEATHER_EDGE_REQUIRE_CALIBRATION` | `true` | Block signals on uncalibrated station/platform buckets |
| `WEATHER_EDGE_MIN_ENTRY_PRICE` / `_MAX_ENTRY_PRICE` | `0.05` / `0.95` | Average fill price range (tails are where model error is largest) |
| `WEATHER_EDGE_MAX_SPREAD` | `0.08` | Max bid/ask spread (dollars) on the side bought |
| `WEATHER_EDGE_MIN_DEPTH_USD` | `50` | Minimum resting ask depth on the side bought |
| `WEATHER_EDGE_PAPER_STAKE_USD` | `25` | Paper stake walked through the book |
| `WEATHER_EDGE_MAX_CONTRACTS` | `500` | Contract cap per simulated order |
| `WEATHER_EDGE_MAX_EVENT_EXPOSURE_USD` | `100` | Open paper cost allowed per event (correlated outcomes) |
| `WEATHER_EDGE_MAX_LEAD_DAYS` | `3` | Ignore events further out than this |
| `WEATHER_EDGE_MAX_FORECAST_AGE_MINUTES` | `180` | Forecast staleness limit |
| `WEATHER_EDGE_MAX_QUOTE_AGE_SECONDS` | `180` | Quote staleness limit |
| `WEATHER_EDGE_ALERTS_ENABLED` | `true` | Discord alerts (still need a webhook) |
| `WEATHER_EDGE_ALERT_MIN_NET_EV_PCT` | `8.0` | Alert threshold (per-watchlist overrides apply) |
| `WEATHER_EDGE_DISCORD_WEBHOOK_URL` | unset | **Secret.** Dedicated webhook; sports channels are never used |
| `WEATHER_EDGE_OPENMETEO_KEY` | unset | **Secret.** Commercial Open-Meteo key (required for commercial use) |
| `WEATHER_EDGE_USER_AGENT` | built-in | Sent to NWS/others; include a contact address |
| `WEATHER_EDGE_KALSHI_BASE`, `_POLYMARKET_GAMMA`, `_POLYMARKET_CLOB`, `_NWS_BASE` | official hosts | Overrides for testing |

No platform trading credential is needed or read by this module. Secrets are read from the environment only, never stored in the database, logged, or sent to the browser.

## Database (additive migrations, created by `init_db`, SQLite and PostgreSQL)

`weather_scan_runs`, `weather_contracts`, `weather_forecast_snapshots`, `weather_market_snapshots`, `weather_predictions`,
`weather_signals`, `weather_paper_trades`, `weather_calibration` (per platform/station/metric/lead bucket), `weather_alert_log`,
`weather_watchlist`, `weather_backtests`. All use `CREATE TABLE IF NOT EXISTS`; existing tables are untouched. The DDL lives in
`src/weather_edge/store.py` and is shared by `init_db` and the test fixture.

## Operations

- **Worker**: jobs `weather-edge-scan` (every `SCAN_INTERVAL` minutes) and `weather-edge-settle` (every 6 h) are queued only when `WEATHER_EDGE_ENABLED=true`; both are no-ops otherwise. Overlap is prevented by the existing `_create_job_if_not_queued`; writes are idempotent by natural keys (prediction/signal bucket keys, unique paper-trade per signal, alert key per contract/side/day).
- **Provider compliance**: every call is paced (≥ 0.15–0.6 s apart), has a 20 s timeout, retries 429/5xx/network errors with exponential backoff honoring `Retry-After`, and never logs query strings. One failing provider/station degrades the scan (status `partial`); a total outage is recorded as `failed`.
- **CLI**: `python -m src.weather_edge.cli scan | settle | backfill-calibration --days N | backtest --days N | status`.
- **Pages**: customer site → Weather Edge (read-only; shown only when `WEATHER_EDGE_ENABLED=true` on that service); operator dashboard → Weather Edge tab (adds watchlist management; admin-gated like the rest of the dashboard).

## Deployment steps

1. Deploy (no manual migration; tables are created on start).
2. In Render set `WEATHER_EDGE_USER_AGENT` (with a contact) on the worker. Decide on Open-Meteo licensing (commercial key or keep non-commercial use).
3. Run once from a shell with the production env: `python -m src.weather_edge.cli backfill-calibration --days 120`. **Until stations have ≥ 30 settled samples the scanner records predictions but raises no signals** (this is deliberate).
4. Optional: create a dedicated Discord channel and set `WEATHER_EDGE_DISCORD_WEBHOOK_URL`.
5. Set `WEATHER_EDGE_ENABLED=true` on the worker (and the customer service to show the menu entry).
6. Watch `weather_scan_runs` / the dashboard tab for a few days before relying on anything; run `backtest` for a sanity check.

## Known limitations and open items

- Calibration against Kalshi uses a *deterministic* archived forecast (no historical ensemble archive is available), so the fitted error spread is conservative for ensemble forecasts.
- Polymarket has no settlement-value history in this system, so it stays uncalibrated (informational, no signals) until its own results accumulate; given the US trading restriction this is the safe default.
- Same-day extremes use hourly observations; the platform's final reading can differ slightly. Same-day markets without observations are skipped.
- Monthly/precipitation/snow/hurricane markets are listed but not modeled.
- No position sizing beyond a fixed paper stake and per-event cap; no real orders by design. A future live-order integration would have to be a separate, explicitly feature-flagged module with its own permission checks.
