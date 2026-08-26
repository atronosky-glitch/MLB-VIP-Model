"""Pinnacle via The Odds API's own ``pinnacle`` bookmaker — added
2026-08-26 after an operator audit found the paid Odds-API plan (bought
specifically for Pinnacle access) was never actually reaching it: every
existing Odds-API call in this codebase hardcodes ``regions="us"``, and
Pinnacle is classified under ``eu``, never ``us``.

This targets it directly with ``bookmakers=pinnacle`` rather than
requesting a whole region — cheaper (The Odds API's own docs: up to 10
explicitly-named books count as a single region for quota purposes;
confirmed live 2026-08-26, a 3-market ``bookmakers=pinnacle`` call cost
exactly 3 credits, same as one region) and avoids pulling in dozens of
irrelevant European books.

Produces the exact same ``PinnacleProp``/``PinnacleGameOdds`` dataclasses
``src/pinnacle_feed.py``'s direct pinnapi.com feed produces (imported
from there, not redefined), so the existing ``build_pinnacle_lookup``/
``match_pinnacle``/``inject_pinnacle_reference`` (and the game-market
equivalents) work completely unchanged regardless of which feed produced
the data. Those injection functions already skip a side that already has
a "pinnacle" entry (``if "pinnacle" in gdata[side]: continue``), which is
what makes calling this source first and direct pinnapi.com second both
correct (proper priority order) and safe (a failure in either source
can never remove or corrupt data the other already supplied) with zero
changes to that merge logic.

Live-verified 2026-08-26 (see docs/DECISIONS.md for the full table):
MLB moneyline/spread/total (15/15 real events that day), all 4
registered MLB prop markets (batter_home_runs, batter_total_bases,
pitcher_strikeouts, pitcher_outs). WNBA moneyline/spread/total (2/4 real
events — real, partial coverage, not a bug) and 3 of 8 registered prop
markets (player_points, player_rebounds, player_assists) on the one
event tested that day; the 4 combo markets and player_threes were not
observed — plausible per-event/per-book variation, not necessarily
permanent, so nothing here hardcodes "unavailable" for them (only
``PINNACLE_PROP_UNITS_BY_LEAGUE``'s existing per-league unit map, already
missing those exact same combo entries for the direct pinnapi.com feed
too, limits what can ever be matched). NFL not meaningfully testable yet
(no real games before the 2026-09-10 opener). Pinnacle does NOT offer
alternate lines via The Odds API (confirmed live) — direct pinnapi.com
remains the only source for those.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from . import prop_config as cfg
from .market_analysis import american_to_decimal
from .odds_api_client import OddsAPIClient, OddsAPIKeyError
from .pinnacle_feed import PinnacleGameOdds, PinnacleProp

logger = logging.getLogger(__name__)

SOURCE_NAME = "odds_api_pinnacle"

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_NO_API_KEY = "no_api_key"
STATUS_LEAGUE_NOT_CONFIGURED = "league_not_configured"
STATUS_HTTP_ERROR = "http_error"
STATUS_NETWORK_ERROR = "network_error"
STATUS_PARSE_ERROR = "parse_error"
STATUS_NO_PINNACLE_POSTED = "no_pinnacle_currently_posted"

# league -> Odds-API sport_key. Reuses each league adapter's own already-
# verified constant rather than redeclaring it, so this can never drift
# from the game-odds/props fallback that already uses the same values.
def _sport_key_for_league(league: str) -> str | None:
    from .sports import get_league
    try:
        league_mod = get_league(league)
    except Exception:
        return None
    return getattr(league_mod, "ODDS_API_SPORT_KEY", None)


# league -> {odds-api market key -> our market_type}. Reuses each
# league's existing player-props parser mapping (the same one the
# primary US-books props fetch already uses) rather than a second,
# separately-maintained copy.
def _prop_market_type_map_for_league(league: str) -> dict[str, str]:
    if league == "MLB":
        from .mlb_props_parser import _PROP_MARKET_TYPE
        return _PROP_MARKET_TYPE
    if league == "WNBA":
        from .wnba_odds_parser import _PROP_MARKET_TYPE
        return _PROP_MARKET_TYPE
    if league == "NFL":
        from .nfl_props_parser import _PROP_MARKET_TYPE
        return _PROP_MARKET_TYPE
    return {}


def _parse_commence_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_iso_epoch(raw: str | None) -> float | None:
    dt = _parse_commence_time(raw)
    return dt.timestamp() if dt is not None else None


def _find_pinnacle_bookmaker(event: dict) -> dict | None:
    for b in event.get("bookmakers") or []:
        if b.get("key") == cfg.ODDS_API_PINNACLE_BOOKMAKER_KEY:
            return b
    return None


def _parse_game_odds_response(events_data: list[dict], league: str) -> list[PinnacleGameOdds]:
    """Parse The Odds API's per-event bookmakers[] shape into
    PinnacleGameOdds objects, one per (event, market) with a real
    pinnacle entry. Signed spread convention matches pinnapi's own
    (home team's own signed point — negative = home favorite) directly,
    since that's exactly what The Odds API already returns per side —
    no sign flip needed here (unlike converting a group's own unsigned
    line to look a Pinnacle entry up, which inject_pinnacle_game_reference
    already handles for either source)."""
    market_types = cfg.PINNACLE_GAME_MARKET_TYPES_BY_LEAGUE.get(league, {})
    if not market_types:
        return []
    results: list[PinnacleGameOdds] = []
    for ev in events_data:
        home_team = ev.get("home_team", "")
        away_team = ev.get("away_team", "")
        pin = _find_pinnacle_bookmaker(ev)
        if not pin or not home_team or not away_team:
            continue
        last_updated = _parse_iso_epoch(pin.get("last_update"))
        for market in pin.get("markets") or []:
            mkey = market.get("key")
            outcomes = market.get("outcomes") or []
            if mkey == "h2h" and "moneyline" in market_types:
                home_o = next((o for o in outcomes if o.get("name") == home_team), None)
                away_o = next((o for o in outcomes if o.get("name") == away_team), None)
                if not home_o or not away_o or home_o.get("price") is None or away_o.get("price") is None:
                    continue
                results.append(PinnacleGameOdds(
                    home_name=home_team, away_name=away_team,
                    market_type=market_types["moneyline"], line=None,
                    home_decimal=american_to_decimal(home_o["price"]),
                    away_decimal=american_to_decimal(away_o["price"]),
                    over_decimal=None, under_decimal=None,
                    last_updated=last_updated, source=SOURCE_NAME,
                ))
            elif mkey == "spreads" and "spread" in market_types:
                home_o = next((o for o in outcomes if o.get("name") == home_team), None)
                away_o = next((o for o in outcomes if o.get("name") == away_team), None)
                if not home_o or not away_o or home_o.get("point") is None:
                    continue
                results.append(PinnacleGameOdds(
                    home_name=home_team, away_name=away_team,
                    market_type=market_types["spread"], line=float(home_o["point"]),
                    home_decimal=american_to_decimal(home_o["price"]),
                    away_decimal=american_to_decimal(away_o["price"]),
                    over_decimal=None, under_decimal=None,
                    last_updated=last_updated, source=SOURCE_NAME,
                ))
            elif mkey == "totals" and "total" in market_types:
                over_o = next((o for o in outcomes if o.get("name") == "Over"), None)
                under_o = next((o for o in outcomes if o.get("name") == "Under"), None)
                if not over_o or not under_o or over_o.get("point") is None:
                    continue
                results.append(PinnacleGameOdds(
                    home_name=home_team, away_name=away_team,
                    market_type=market_types["total"], line=float(over_o["point"]),
                    home_decimal=None, away_decimal=None,
                    over_decimal=american_to_decimal(over_o["price"]),
                    under_decimal=american_to_decimal(under_o["price"]),
                    last_updated=last_updated, source=SOURCE_NAME,
                ))
    return results


def _parse_props_response(event_data: dict, prop_market_type_map: dict, league: str) -> list[PinnacleProp]:
    """Parse one event's per-event-odds response into PinnacleProp
    objects. unit is derived via the same our-market-type -> pinnapi-unit
    reverse map match_pinnacle() itself uses (PINNACLE_PROP_UNITS_BY_LEAGUE)
    so a prop built from The Odds API is matchable by the exact same
    lookup/matching code the direct pinnapi.com feed already relies on —
    a market_type with no unit entry there (e.g. WNBA's combo props) is
    skipped, same as it would be for that other source."""
    unit_map = cfg.PINNACLE_PROP_UNITS_BY_LEAGUE.get(league, {})
    market_type_to_unit = {v: k for k, v in unit_map.items()}
    home_team = event_data.get("home_team", "")
    away_team = event_data.get("away_team", "")
    pin = _find_pinnacle_bookmaker(event_data)
    if not pin or not home_team or not away_team:
        return []
    results: list[PinnacleProp] = []
    for market in pin.get("markets") or []:
        market_type = prop_market_type_map.get(market.get("key"))
        unit = market_type_to_unit.get(market_type) if market_type else None
        if unit is None:
            continue
        # Confirmed live 2026-08-26: unlike the game-odds endpoint (which
        # carries "last_update" at the bookmaker level), the per-event
        # props response only carries it per-market — fall back to the
        # bookmaker-level value if a future response shape adds it there.
        last_updated = _parse_iso_epoch(market.get("last_update") or pin.get("last_update"))
        by_player: dict[str, dict[str, dict]] = {}
        for outcome in market.get("outcomes") or []:
            player = outcome.get("description")
            side = (outcome.get("name") or "").strip().lower()
            point = outcome.get("point")
            price = outcome.get("price")
            if not player or side not in ("over", "under") or point is None or price is None:
                continue
            by_player.setdefault(player, {})[side] = {"point": point, "price": price}
        for player, sides in by_player.items():
            over = sides.get("over")
            under = sides.get("under")
            if not over or not under or over["point"] != under["point"]:
                continue
            results.append(PinnacleProp(
                home_name=home_team, away_name=away_team, player_name=player,
                unit=unit, line=float(over["point"]),
                over_decimal=american_to_decimal(over["price"]),
                under_decimal=american_to_decimal(under["price"]),
                over_american=int(over["price"]), under_american=int(under["price"]),
                last_updated=last_updated, source=SOURCE_NAME,
            ))
    return results


def derive_props_targets(
    ou_groups: dict, event_map: dict, league: str,
    min_books: int = 1, now: datetime | None = None,
) -> dict[str, set[str]]:
    """{event_id: {odds-api market keys}} worth a Pinnacle props check —
    derived from THIS scan's own already-formed comparable groups, added
    2026-08-26 to stop spending Pinnacle credits on event/market
    combinations with no usable comparison-book data (unsupported
    market, event already started) in the first place.

    Deliberately filters on DATA AVAILABILITY only, computed before any
    fair-value/EV math runs on these groups — never on the resulting
    edge — so every market genuinely eligible for evaluation still gets
    a fresh Pinnacle comparison; this must never become a filter for
    "only check Pinnacle on bets that already look good," which would
    bias away from exactly the edges a real Pinnacle disagreement might
    reveal.

    Deliberately does NOT require the group to already meet
    MIN_COMPARISON_BOOKS on its own (bug found and fixed 2026-08-26,
    caught before it could ever bite at the current MIN_COMPARISON_BOOKS
    default): src.player_prop_analysis._classify_market computes its
    book-count gate from over_prices/under_prices AFTER Pinnacle has
    already been injected into that same dict — i.e. Pinnacle itself
    counts toward MIN_COMPARISON_BOOKS in the real model, and can be the
    one book that completes a side that otherwise had none at all, or
    pushes a thin group over the threshold. Requiring the threshold to
    already be met BEFORE fetching Pinnacle would make this prefilter
    incorrectly exclude exactly the groups Pinnacle itself would make
    eligible. *min_books* is accepted for interface stability (a caller
    may reasonably pass cfg.MIN_COMPARISON_BOOKS) but is intentionally
    unused for exclusion — only whether a group has ANY real data at
    all (guaranteed by simply appearing in ou_groups) gates inclusion.
    """
    now = now or datetime.now(timezone.utc)
    prop_market_type_map = _prop_market_type_map_for_league(league)
    market_type_to_key = {v: k for k, v in prop_market_type_map.items()}
    targets: dict[str, set[str]] = {}
    for gd in ou_groups.values():
        if gd.get("player_id") == "GAME":
            continue  # game markets handled separately (game-odds fetch)
        market_key = market_type_to_key.get(gd.get("market_type"))
        if market_key is None:
            continue  # not one of this league's Odds-API-registered prop markets
        if not gd.get("over") and not gd.get("under"):
            continue  # no real data on either side — nothing for Pinnacle to complete
        event_id = gd.get("event_id")
        if not event_id:
            continue
        ev_info = event_map.get(event_id) or {}
        start_time = _parse_commence_time(ev_info.get("start_time"))
        if start_time is not None and start_time < now:
            continue  # already started — not a live pregame bet anymore
        targets.setdefault(event_id, set()).add(market_key)
    return targets


class OddsAPIPinnacleClient:
    """Fetch Pinnacle game odds and player props via The Odds API's
    targeted ``bookmakers=pinnacle`` parameter, with the same disk-cache
    (via OddsAPIClient's own TTL-based cache — a different cache key per
    distinct param set, so this can never collide with or overwrite the
    existing us-region fetches) and the same shared credit-budget
    discipline the primary props fetch already uses.
    """

    def __init__(self, cache_ttl_seconds: float | None = None):
        ttl = (
            float(cache_ttl_seconds)
            if cache_ttl_seconds is not None
            else float(cfg.ODDS_API_PINNACLE_CACHE_TTL_SECONDS)
        )
        self._client = OddsAPIClient(max_cache_age=ttl)
        self.last_fetch_status: dict[str, str] = {}
        self.last_props_status: dict[str, str] = {}

    def get_game_odds(self, league: str, conn=None) -> list[PinnacleGameOdds] | None:
        if not cfg.ODDS_API_PINNACLE_ENABLED:
            self.last_fetch_status[league] = STATUS_DISABLED
            return None
        market_types = cfg.PINNACLE_GAME_MARKET_TYPES_BY_LEAGUE.get(league)
        sport_key = _sport_key_for_league(league)
        if not market_types or not sport_key:
            self.last_fetch_status[league] = STATUS_LEAGUE_NOT_CONFIGURED
            return None
        job_type = f"{league.lower()}_pinnacle_game_odds"
        if conn is not None:
            try:
                from src.odds_api_credits import credit_budget_check, GAME_ODDS_COST
                allowed, reason = credit_budget_check(conn, GAME_ODDS_COST)
            except Exception:
                allowed, reason = True, "budget check unavailable"
            if not allowed:
                logger.warning(
                    "Odds-API Pinnacle game-odds fetch skipped for %s — credit budget: %s",
                    league, reason,
                )
                self.last_fetch_status[league] = STATUS_NO_PINNACLE_POSTED
                return None
        try:
            data, from_cache = self._client.get_odds(
                sport_key=sport_key, markets="h2h,spreads,totals",
                bookmakers=cfg.ODDS_API_PINNACLE_BOOKMAKER_KEY,
            )
        except OddsAPIKeyError:
            self.last_fetch_status[league] = STATUS_NO_API_KEY
            return None
        except requests.exceptions.RequestException as exc:
            status = STATUS_HTTP_ERROR if isinstance(exc, requests.exceptions.HTTPError) else STATUS_NETWORK_ERROR
            self.last_fetch_status[league] = status
            logger.warning("Odds-API Pinnacle game-odds fetch failed for %s: %s", league, exc)
            return None
        if conn is not None:
            try:
                from src.odds_api_credits import record_client_quota
                record_client_quota(
                    conn, self._client, endpoint="odds_pinnacle_game",
                    job_type=job_type, cache_hit=from_cache,
                )
            except Exception:
                logger.debug("Could not record Odds-API Pinnacle game-odds quota usage", exc_info=True)
        try:
            games = _parse_game_odds_response(data, league)
        except Exception:
            logger.exception("Odds-API Pinnacle game-odds parse failed for %s", league)
            self.last_fetch_status[league] = STATUS_PARSE_ERROR
            return None
        self.last_fetch_status[league] = STATUS_OK if games else STATUS_NO_PINNACLE_POSTED
        return games or None

    def get_player_props(self, league: str, conn=None) -> list[PinnacleProp] | None:
        if not cfg.ODDS_API_PINNACLE_ENABLED:
            self.last_props_status[league] = STATUS_DISABLED
            return None
        prop_market_type_map = _prop_market_type_map_for_league(league)
        sport_key = _sport_key_for_league(league)
        if not prop_market_type_map or not sport_key:
            self.last_props_status[league] = STATUS_LEAGUE_NOT_CONFIGURED
            return None
        job_type = f"{league.lower()}_pinnacle_props"
        try:
            events, _ = self._client.get_events(sport_key=sport_key)
        except OddsAPIKeyError:
            self.last_props_status[league] = STATUS_NO_API_KEY
            return None
        except requests.exceptions.RequestException as exc:
            self.last_props_status[league] = STATUS_NETWORK_ERROR
            logger.warning("Odds-API Pinnacle props event discovery failed for %s: %s", league, exc)
            return None

        # Same near-term pregame window as the primary props fetch (see
        # src/odds_api_props_fetch.py) — get_events() has no time filter
        # of its own, and a full-season sport like NFL would otherwise
        # mean iterating months of irrelevant events.
        now = datetime.now(timezone.utc)
        window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=42)
        near_term = [
            e for e in events
            if (dt := _parse_commence_time(e.get("commence_time"))) is not None
            and window_start <= dt <= window_end
        ]

        market_keys = ",".join(prop_market_type_map)
        planned_cost = len(prop_market_type_map)
        all_props: list[PinnacleProp] = []
        for ev in near_term:
            if conn is not None:
                try:
                    from src.odds_api_credits import credit_budget_check
                    allowed, reason = credit_budget_check(conn, planned_cost)
                except Exception:
                    allowed, reason = True, "budget check unavailable"
                if not allowed:
                    logger.warning(
                        "Odds-API Pinnacle props fetch stopped at %d/%d events for %s — credit budget: %s",
                        len(all_props), len(near_term), league, reason,
                    )
                    break
            try:
                data, from_cache = self._client.get_event_odds(
                    ev["id"], sport_key=sport_key, markets=market_keys,
                    bookmakers=cfg.ODDS_API_PINNACLE_BOOKMAKER_KEY,
                )
            except requests.exceptions.RequestException:
                continue
            if conn is not None:
                try:
                    from src.odds_api_credits import record_client_quota
                    record_client_quota(
                        conn, self._client, endpoint="odds_pinnacle_props",
                        job_type=job_type, cache_hit=from_cache,
                    )
                except Exception:
                    logger.debug("Could not record Odds-API Pinnacle props quota usage", exc_info=True)
            try:
                all_props.extend(_parse_props_response(data, prop_market_type_map, league))
            except Exception:
                logger.exception(
                    "Odds-API Pinnacle props parse failed for %s event %s", league, ev.get("id"),
                )
        self.last_props_status[league] = STATUS_OK if all_props else STATUS_NO_PINNACLE_POSTED
        return all_props or None

    def get_player_props_for_targets(
        self, league: str, targets: dict[str, set[str]], conn=None,
    ) -> list[PinnacleProp] | None:
        """Same as get_player_props, but scoped to an explicit
        {event_id: {odds-api market keys}} map instead of discovering
        every near-term event and requesting all registered markets for
        each — added 2026-08-26 after computing that MLB alone was
        spending Pinnacle credits on event/market combinations with no
        usable comparison-book data to compare against in the first
        place.

        *targets* should come from the SAME scan's already-formed
        comparable groups (see src.player_prop_scanner's derivation),
        not a separate discovery pass — this is what avoids selection
        bias toward already-attractive-looking bets: it filters on
        whether a market has ANY real, paired book data to compare
        against (computed before any EV/fair-value math runs), never on
        the computed edge itself. Every event/market genuinely eligible
        for evaluation still gets a fresh Pinnacle comparison; only
        combinations with nothing to compare against are skipped.
        """
        if not cfg.ODDS_API_PINNACLE_ENABLED:
            self.last_props_status[league] = STATUS_DISABLED
            return None
        sport_key = _sport_key_for_league(league)
        if not sport_key:
            self.last_props_status[league] = STATUS_LEAGUE_NOT_CONFIGURED
            return None
        job_type = f"{league.lower()}_pinnacle_props"
        if not targets:
            self.last_props_status[league] = STATUS_NO_PINNACLE_POSTED
            return None

        all_props: list[PinnacleProp] = []
        prop_market_type_map = _prop_market_type_map_for_league(league)
        for event_id, market_keys_set in targets.items():
            if not market_keys_set:
                continue
            market_keys = ",".join(sorted(market_keys_set))
            planned_cost = len(market_keys_set)
            if conn is not None:
                try:
                    from src.odds_api_credits import credit_budget_check
                    allowed, reason = credit_budget_check(conn, planned_cost)
                except Exception:
                    allowed, reason = True, "budget check unavailable"
                if not allowed:
                    logger.warning(
                        "Odds-API Pinnacle props (targeted) fetch stopped for %s — credit budget: %s",
                        league, reason,
                    )
                    break
            try:
                data, from_cache = self._client.get_event_odds(
                    event_id, sport_key=sport_key, markets=market_keys,
                    bookmakers=cfg.ODDS_API_PINNACLE_BOOKMAKER_KEY,
                )
            except OddsAPIKeyError:
                self.last_props_status[league] = STATUS_NO_API_KEY
                return all_props or None
            except requests.exceptions.RequestException:
                continue
            if conn is not None:
                try:
                    from src.odds_api_credits import record_client_quota
                    record_client_quota(
                        conn, self._client, endpoint="odds_pinnacle_props",
                        job_type=job_type, cache_hit=from_cache,
                    )
                except Exception:
                    logger.debug("Could not record Odds-API Pinnacle props quota usage", exc_info=True)
            try:
                all_props.extend(_parse_props_response(data, prop_market_type_map, league))
            except Exception:
                logger.exception(
                    "Odds-API Pinnacle props (targeted) parse failed for %s event %s", league, event_id,
                )
        self.last_props_status[league] = STATUS_OK if all_props else STATUS_NO_PINNACLE_POSTED
        return all_props or None
