"""Streamlit pages for Weather Edge, shared by the customer site (read-only) and the operator
dashboard (``editable=True`` adds watchlist management).

Honesty rules baked into the page: every number comes from recorded rows; empty or stale data is
shown as such (never as zero); outputs are labeled estimates; paper and backtest results are labeled
simulated; nothing on the page can place an order.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from . import performance, store, ui_data
from .config import WeatherEdgeConfig
from .registry import STATIONS

logger = logging.getLogger(__name__)

SAFETY_NOTE = (
    "Analysis, alerts and paper trading only. Probabilities and EV are model estimates built from public "
    "forecast data and the order book at scan time; they can be wrong and are not a guarantee or advice. "
    "This page cannot place orders."
)
_METRIC_LABEL = {"high": "Daily high", "low": "Daily low"}


def _pct(x, digits=1) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _money(x, digits=3) -> str:
    return "—" if x is None else f"${x:,.{digits}f}"


def _opp_frame(rows: list[dict]) -> pd.DataFrame:
    recs = []
    for r in rows:
        recs.append({
            "Platform": r["platform"].title(), "City": r["city"], "Date": r["target_date"],
            "Market": _METRIC_LABEL.get(r["metric"], r["metric"]), "Outcome": f"{r['side']} · {r['bin_label']}",
            "Station": r["station_label"], "Forecast prob": round(r["p_win"] * 100, 1),
            "Market price": round(r["avg_price"], 3), "Fair value": round(r["p_win"], 3),
            "Net EV %": round(r["net_ev_pct"], 1), "Net EV $/contract": round(r["net_ev"], 3),
            "Spread": None if r["spread"] is None else round(r["spread"], 3),
            "Depth $": None if r["liquidity_usd"] is None else round(r["liquidity_usd"], 0),
            "Confidence": r["confidence"], "Updated": (r["created_at"] or "")[:16].replace("T", " "),
            "Status": "Signal" if r["decision"] == "SIGNAL" else f"Rejected: {r['reasons']}",
            "_key": f"{r['platform']}|{r['market_id']}",
        })
    return pd.DataFrame(recs)


def render_overview(ov: dict, cfg: WeatherEdgeConfig, key_prefix: str) -> None:
    st.caption(SAFETY_NOTE)
    if not ov["has_scan"]:
        st.info("No Weather Edge scan has completed yet, so there is nothing to show. "
                "When the scanner runs, scanned markets and opportunities appear here.")
        return
    c = st.columns(4)
    c[0].metric("Markets scanned", f"{ov['contracts_modeled']} modeled", help=f"{ov['contracts_seen']} weather contracts seen in total")
    c[1].metric("Valid opportunities", ov["opportunities"])
    c[2].metric("Avg modeled net EV", "—" if ov["avg_net_ev_pct"] is None else f"{ov['avg_net_ev_pct']:+.1f}%",
                help="Average of the current opportunities' net EV after fees and slippage. Shown only when there are opportunities.")
    age = ov["age_minutes"]
    c[3].metric("Data freshness", "unknown" if age is None else (f"{age:.0f} min ago" if age < 120 else f"{age / 60:.1f} h ago"),
                delta="STALE" if ov["freshness"] == "stale" else None, delta_color="inverse")
    if ov["freshness"] == "stale":
        st.warning("The latest scan is older than expected. Treat any opportunity below as out of date.")
    src = ov.get("sources_ok", {})
    bad = [k for k, v in src.items() if not v]
    if bad:
        st.warning("Data source unavailable in the last scan: " + ", ".join(bad) + ".")
    geo = ov.get("geoblock")
    if geo and geo.get("blocked"):
        st.info("Polymarket reports this server's region as restricted for trading. Polymarket prices are shown "
                "for comparison only; check your own eligibility before acting on any platform.")
    if ov.get("calibrated_buckets", 0) == 0:
        st.warning("No station has enough settled history to be calibrated yet, so the scanner is not raising "
                   "signals. Run the calibration backfill (see the How it works tab).")


def render_opportunities(conn, cfg: WeatherEdgeConfig, key_prefix: str) -> None:
    show_rejected = st.toggle("Include near-misses (positive EV but failed a safety gate)", value=False, key=f"{key_prefix}_rej")
    rows = ui_data.opportunities(conn, include_rejected=show_rejected)
    if not rows:
        st.info("No opportunities in the latest scan. That is a normal outcome: most of the time the market "
                "price already reflects the forecast.")
        return
    with st.expander("Filters", expanded=False):
        f1, f2, f3 = st.columns(3)
        platforms = f1.multiselect("Platform", sorted({r["platform"] for r in rows}), key=f"{key_prefix}_f_plat")
        stations = f2.multiselect("City", sorted({r["station_icao"] for r in rows}), format_func=ui_data.city_name, key=f"{key_prefix}_f_city")
        metrics = f3.multiselect("Market type", sorted({r["metric"] for r in rows}), format_func=lambda m: _METRIC_LABEL.get(m, m), key=f"{key_prefix}_f_metric")
        g1, g2, g3, g4 = st.columns(4)
        lead = g1.slider("Max horizon (days)", 0, max(cfg.max_lead_days, 1), max(cfg.max_lead_days, 1), key=f"{key_prefix}_f_lead")
        min_ev = g2.number_input("Min net EV %", value=0.0, step=1.0, key=f"{key_prefix}_f_ev")
        conf = g3.selectbox("Min confidence", ["any", "medium", "high"], key=f"{key_prefix}_f_conf")
        liq = g4.number_input("Min depth $", value=0.0, step=10.0, key=f"{key_prefix}_f_liq")
    rows = ui_data.apply_filters(
        rows, platforms=platforms or None, stations=stations or None, metrics=metrics or None, max_lead_days=lead,
        min_net_ev_pct=min_ev, min_confidence=None if conf == "any" else conf, min_liquidity_usd=liq)
    if not rows:
        st.info("No opportunities match these filters.")
        return
    sort_by = st.selectbox("Sort by", ["Net EV %", "Forecast prob", "Spread", "Depth $", "Date"], key=f"{key_prefix}_sort")
    df = _opp_frame(rows).sort_values(sort_by, ascending=sort_by in ("Spread", "Date"), na_position="last")
    st.dataframe(df.drop(columns=["_key"]), use_container_width=True, hide_index=True)
    st.caption("Market price is the average fill price for the paper stake, walking the real order book. "
               "Net EV is after exchange fees and slippage. Fair value is the model's probability that this side wins.")
    labels = {f"{r['city']} · {r['target_date']} · {r['side']} {r['bin_label']} ({r['platform']})": (r["platform"], r["market_id"]) for r in rows}
    pick = st.selectbox("Open market detail", ["—"] + list(labels), key=f"{key_prefix}_open")
    if pick != "—":
        st.session_state[f"{key_prefix}_detail"] = labels[pick]
        st.caption("Selected. Open the Market detail tab.")


def render_market_detail(conn, key_prefix: str) -> None:
    sel = st.session_state.get(f"{key_prefix}_detail")
    if not sel:
        st.info("Choose a market under Opportunities → “Open market detail”.")
        return
    d = ui_data.market_detail(conn, *sel)
    if d is None:
        st.warning("That market is no longer recorded.")
        return
    c, pred = d["contract"], d["prediction"]
    st.subheader(c["title"] or c["market_id"])
    left, right = st.columns(2)
    left.markdown(
        f"**Platform:** {c['platform'].title()}  \n**Station:** {c['station_label']}  \n"
        f"**Settlement source:** {c['settlement_source']}  \n**Date:** {c['target_date']} ({c['tz']})  \n**Status:** {c['status']}")
    if pred:
        right.markdown(
            f"**Model P(YES):** {_pct(pred['p_yes'])} (range {_pct(pred['p_yes_low'])}–{_pct(pred['p_yes_high'])})  \n"
            f"**Market-implied P(YES):** {_pct(pred['market_yes_mid'])}  \n"
            f"**Simple-forecast baseline:** {_pct(pred['baseline_p_yes'])}  \n"
            f"**Calibrated:** {'yes' if pred['calibrated'] else 'NO — informational only'}")
    with st.expander("Settlement rules (from the platform)"):
        st.text(c["rules_text"] or "No rules text recorded.")
    snap = d["snapshot"]
    if snap:
        st.markdown("**Forecast sources**")
        rows = [{"Source": f"Ensemble · {k}", "Daily extreme °F": round(v, 1)} for k, v in sorted(d["family_means"].items())]
        if snap["nws_point"] is not None:
            rows.append({"Source": "NWS gridpoint forecast", "Daily extreme °F": round(snap["nws_point"], 1)})
        if snap["observed_so_far"] is not None:
            rows.append({"Source": "Observed so far today", "Daily extreme °F": round(snap["observed_so_far"], 1)})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        if snap["source_note"]:
            st.caption(snap["source_note"])
    if d["event_distribution"]:
        st.markdown("**Probability distribution across this event's outcomes**")
        dist = pd.DataFrame(d["event_distribution"]).set_index("bin")[["model", "market"]]
        dist.columns = ["Model", "Market-implied"]
        st.bar_chart(dist)
    if d["price_history"]:
        hist = pd.DataFrame(d["price_history"])
        hist["captured_at"] = pd.to_datetime(hist["captured_at"], errors="coerce")
        st.markdown("**Recorded price history (YES bid / ask)**")
        st.line_chart(hist.set_index("captured_at")[["yes_bid", "yes_ask"]])
    if d["signals"]:
        st.markdown("**EV breakdown (paper stake, real order book walk)**")
        rows = []
        for s in d["signals"]:
            rows.append({
                "Side": s["side"], "Contracts": s["qty"], "Best ask": s["best_price"], "Avg fill": s["avg_price"],
                "Slippage": None if s["avg_price"] is None else round(s["avg_price"] - s["best_price"], 4),
                "Fee $": s["fee"], "Total cost $": s["total_cost"], "Win prob": round(s["p_win"], 4),
                "Break-even prob": round(s["break_even_prob"], 4), "Gross EV/contract": round(s["gross_ev"], 4),
                "Net EV/contract": round(s["net_ev"], 4), "Net EV %": round(s["net_ev_pct"], 2),
                "Robust net EV/contract": None if s["conservative_net_ev"] is None else round(s["conservative_net_ev"], 4),
                "Decision": s["decision"], "Reasons": s["reasons"],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("“Robust” re-evaluates the probability assuming the calibration is off by about one standard error; "
                   "an edge that disappears there is treated as not trustworthy.")
    cal = d["calibration"]
    if cal:
        st.caption(f"Calibration used: bias {cal['bias_f']:+.2f}°F, error spread {cal['residual_std_f']:.2f}°F, "
                   f"{cal['n']} settled samples ({cal['source']}).")
    else:
        st.caption("No calibration history for this platform/station yet; a wide prior was used and signals are blocked.")


def render_performance(conn) -> None:
    st.markdown("#### Paper trading (simulated fills at recorded executable prices)")
    perf = performance.paper_performance(conn)
    ov = perf["overall"]
    if ov["n"] == 0:
        st.info(f"No settled paper trades yet ({perf['open_positions']['n']} open). Results appear here only after "
                "recorded signals settle; nothing is estimated or back-filled.")
    else:
        c = st.columns(4)
        c[0].metric("Settled trades", ov["n"])
        c[1].metric("Win rate", f"{ov['win_rate'] * 100:.0f}%")
        c[2].metric("Paper P/L", f"${ov['pnl']:+,.2f}", help="Simulated; not real money.")
        c[3].metric("ROI on cost", "—" if ov["roi_pct"] is None else f"{ov['roi_pct']:+.1f}%")
        st.caption(f"Max drawdown ${ov['max_drawdown']:,.2f}. Predicted avg net EV/contract {ov['avg_predicted_net_ev_per_contract']:+.3f} "
                   f"vs realized {ov['avg_realized_pnl_per_contract']:+.3f}.")
        if not ov["reliable"]:
            st.warning("Fewer than 30 settled trades: these figures are too noisy to conclude anything.")
        for title, key in (("By lead time (days)", "by_lead_days"), ("By city", "by_station"),
                           ("By platform", "by_platform"), ("By market type", "by_metric")):
            rows = [{"Group": k, "Trades": v["n"], "Win rate %": round(v["win_rate"] * 100, 1), "P/L $": round(v["pnl"], 2),
                     "ROI %": None if v["roi_pct"] is None else round(v["roi_pct"], 1)} for k, v in perf[key].items() if v["n"]]
            if rows:
                with st.expander(title):
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Forecast accuracy (resolved predictions)")
    sc = performance.prediction_scores(conn)
    o = sc["overall"]
    if o["n"] == 0:
        st.info("No predictions have resolved yet, so calibration, Brier score and log loss are not available.")
    else:
        c = st.columns(3)
        c[0].metric("Resolved predictions", o["n"])
        c[1].metric("Brier (model)", f"{o['brier_model']:.3f}", help="Lower is better. 0.25 = always guessing 50%.")
        c[2].metric("Log loss", f"{o['logloss_model']:.3f}")
        if "brier_market" in o:
            st.caption(f"On the {o['n_vs_market']} predictions that had a market price: model Brier "
                       f"{o['brier_model_same_rows']:.3f} vs market-implied {o['brier_market']:.3f}"
                       + (f" vs simple-forecast baseline {o['brier_baseline']:.3f}" if "brier_baseline" in o else "") + ".")
        if not o["reliable"]:
            st.warning("Fewer than 30 resolved predictions: scores are too noisy to trust.")
        if sc["reliability"]:
            st.markdown("**Calibration (predicted vs observed frequency)**")
            rel = pd.DataFrame(sc["reliability"])
            st.dataframe(rel, use_container_width=True, hide_index=True)

    st.markdown("#### Reconstructed backtest")
    bts = store.latest_backtests(conn, 1)
    if not bts:
        st.info("No backtest has been run. It is simulated from archived forecasts and historical prices and is "
                "always labeled as such (see How it works).")
    else:
        res = json.loads(bts[0]["summary_json"])
        st.warning(res.get("label", "Simulated"))
        st.write({k: res.get(k) for k in ("days_simulated", "days_skipped_insufficient_history", "n_bets", "win_rate",
                                           "roi_pct", "pnl", "max_drawdown", "brier_model", "brier_market", "brier_baseline_raw_forecast")})
        for cav in res.get("caveats", []):
            st.caption("• " + cav)


def render_watchlist(conn, key_prefix: str, editable: bool, cfg: WeatherEdgeConfig) -> None:
    rows = store.watchlist(conn)
    if rows:
        st.dataframe(pd.DataFrame([{"City": ui_data.city_name(r["station_icao"]), "Market": r["metric"],
                                    "Alert min net EV %": r["min_net_ev_pct"]} for r in rows]),
                     use_container_width=True, hide_index=True)
    else:
        st.info("The watchlist is empty, so alerts go out for any signal at or above the global threshold "
                f"({cfg.alert_min_net_ev_pct:.0f}% net EV).")
    st.caption("Alerts use a dedicated Discord webhook, fire at most once per contract/side per day, and never "
               + ("go to the sports alert channels." if cfg.discord_webhook_url else "send while no webhook is configured."))
    if not editable:
        st.caption("The watchlist is managed by the site operator.")
        return
    c = st.columns(4)
    icao = c[0].selectbox("City", sorted(STATIONS), format_func=ui_data.city_name, key=f"{key_prefix}_w_city")
    metric = c[1].selectbox("Market", ["high", "low"], key=f"{key_prefix}_w_metric")
    thr = c[2].number_input("Min net EV %", value=float(cfg.alert_min_net_ev_pct), step=1.0, key=f"{key_prefix}_w_thr")
    with c[3]:
        st.write("")
        if st.button("Add / update", key=f"{key_prefix}_w_add"):
            store.watch(conn, icao, metric, thr, datetime.now(timezone.utc))
            st.rerun()
    if rows:
        rm = st.selectbox("Remove", ["—"] + [f"{r['station_icao']}|{r['metric']}" for r in rows], key=f"{key_prefix}_w_rm")
        if rm != "—" and st.button("Remove selected", key=f"{key_prefix}_w_rmbtn"):
            store.unwatch(conn, *rm.split("|"))
            st.rerun()


METHODOLOGY = """
**What this does.** For daily high/low temperature markets it builds a probability for every outcome range
from weather-model ensembles (GEFS and ECMWF, each member is one possible future), shifts and widens them by
the error history measured at that station and lead time, then compares the result with the **executable**
price: the real order book is walked for a paper stake and exchange fees are included. A signal needs positive
net EV above your threshold, an edge that survives a calibration-error stress test, a tight spread, enough depth,
fresh data, and a calibrated station.

**Kalshi vs Polymarket are different bets.** Kalshi's New York high settles on The Weather Company at Central
Park; Polymarket's settles on NOAA at LaGuardia. Contracts are only treated as equivalent when station, source,
date, unit, time zone and outcome range all match; otherwise they are modeled independently.

**What it does not do.** It does not place orders, hold credentials, or predict rain/snow/monthly markets yet
(those are listed as not modeled). Polymarket trading is restricted in some regions including the US; this page
only reads public prices.

**Honest limits.** Forecasts are estimates. Calibration needs settled history, so new stations start blocked.
Backtests use one archived deterministic forecast and top-of-book prices from hourly candles, are labeled
simulated, and are not a track record. Paper results use recorded prices but assume your order fills.
"""


def render_weather_edge(open_conn, *, editable: bool = False, cfg: WeatherEdgeConfig | None = None,
                        key_prefix: str = "we") -> None:
    cfg = cfg or WeatherEdgeConfig.from_env()
    st.header("Weather Edge")
    try:
        conn = open_conn()
    except Exception:
        st.error("Weather data is temporarily unavailable.")
        return
    try:
        render_overview(ui_data.overview(conn, cfg), cfg, key_prefix)
        t_opp, t_det, t_perf, t_watch, t_how = st.tabs(
            ["Opportunities", "Market detail", "Performance", "Watchlist & alerts", "How it works"])
        with t_opp:
            render_opportunities(conn, cfg, key_prefix)
        with t_det:
            render_market_detail(conn, key_prefix)
        with t_perf:
            render_performance(conn)
        with t_watch:
            render_watchlist(conn, key_prefix, editable, cfg)
        with t_how:
            st.markdown(METHODOLOGY)
    except Exception:
        logger.exception("Weather Edge page failed")
        st.error("Weather Edge hit an unexpected error. The scan data is unaffected.")
    finally:
        try:
            conn.close()
        except Exception:
            pass
