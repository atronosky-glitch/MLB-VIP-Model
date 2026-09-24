"""Game-level market matching: does a recommendation have an equivalent
contract on a prediction-market provider, and how confident are we?

Scope: game-level markets only (moneyline / spread / total). Player-prop
market types are out of scope entirely.

Two matching paths:
  * Legacy fuzzy scoring (score_match) for providers with only a
    best-effort parse (Polymarket US): moneyline only, confidence-gated.
  * EXACT resolution (resolve_strict_side) for providers whose contracts
    carry verified structure (Kalshi, 2026-09-23): league, market type,
    Eastern event date (+ start time when known), both teams by exact
    canonical identity (src/execution/team_codes.py), line, and side
    semantics must all match, and the result says which side (YES/NO) of
    the contract equals the recommendation. No fuzzy tier; any doubt is
    "no match", and competing exact matches are ambiguous -> no match.

Pure, DB-free, network-free module -- persistence lives in
src/execution/market_match_store.py, provider-specific market parsing
lives on each provider class (KalshiProvider.parse_game_event,
PolymarketUSProvider.parse_game_event), so this module never has a
provider-conditional branch.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.execution.base import Market, RawGameEvent
from src.execution.kalshi_mapping import EASTERN
from src.execution.team_codes import canonical_team_name

GAME_LEVEL_MARKET_TYPES = frozenset({
    "game_moneyline", "game_spread_ou", "game_runline_ou", "game_total_ou",
})

# game_spread_ou and game_runline_ou are the same logical market (a
# signed line around 0) under different league naming conventions;
# game_total_ou is its own logical type.
_LOGICAL_MARKET_TYPE = {
    "game_moneyline": "moneyline",
    "game_spread_ou": "spread",
    "game_runline_ou": "spread",
    "game_total_ou": "total",
}

MIN_MARKET_MATCH_CONFIDENCE_DEFAULT = 0.98

_TEAM_MATCH_WEIGHT = 0.45
_MARKET_TYPE_WEIGHT = 0.20
_LINE_WEIGHT = 0.25
_DATE_WEIGHT = 0.10

# High enough that an otherwise-perfect match (market type, line, date
# all 1.0) still clears MIN_MARKET_MATCH_CONFIDENCE=0.98 on a fuzzy team
# match alone: 0.96*0.45 + 0.20 + 0.25 + 0.10 = 0.982. This is the one
# arithmetic relationship in this module that must hold for the
# "Athletics" vs "Kansas City Athletics" case to resolve automatically
# at all -- if TEAM_MATCH_WEIGHT or this constant changes, recheck it.
_TEAM_MATCH_FUZZY_SCORE = 0.96
_DATE_PROXIMITY_MINUTES = 90
_DATE_DECAY_HOURS = 24


# ── Normalized representations ──────────────────────────────────────


@dataclass(frozen=True)
class RecommendationEvent:
    recommendation_id: str
    league: str
    market_type: str
    home_team: str
    away_team: str
    side: str
    line: float | None
    event_start_time: datetime | None
    # SIGNED line for the recommended side (favorite negative). Only
    # meaningful for spread/run-line rows; historical_recommendations.line
    # is not guaranteed signed, raw_line is (see grading.grade_spread).
    raw_line: float | None = None


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    market_id: str
    home_team: str | None
    away_team: str | None
    market_type: str | None
    side: str | None
    line: float | None
    event_start_time: datetime | None
    raw_market: Market
    # Exact-identity fields (see base.RawGameEvent); strict_identity=False
    # keeps the legacy fuzzy scoring path for providers without them.
    league: str | None = None
    yes_team: str | None = None
    no_team: str | None = None
    event_id: str | None = None
    event_date: date | None = None
    strict_identity: bool = False


@dataclass(frozen=True)
class MatchResult:
    recommendation_id: str
    provider: str
    provider_market_id: str
    confidence: float
    team_score: float
    market_type_score: float
    line_score: float
    date_score: float
    provider_title: str
    # "YES"/"NO": which side of the provider's contract is the exact
    # equivalent of the recommendation. Set only by strict-identity
    # matching; None means "legacy provider, evaluator decides".
    provider_side: str | None = None


def build_recommendation_event(row: dict[str, Any]) -> RecommendationEvent | None:
    """Build a RecommendationEvent from a historical_recommendations row.

    Returns None for anything not a recognized game-level market_type --
    player-prop rows are never candidates for this stage.
    """
    market_type = row.get("market_type")
    if market_type not in GAME_LEVEL_MARKET_TYPES:
        return None

    matchup = row.get("matchup") or ""
    away, sep, home = matchup.partition(" @ ")
    if not sep:
        return None

    return RecommendationEvent(
        recommendation_id=row["recommendation_id"],
        league=row.get("league") or "",
        market_type=market_type,
        home_team=home.strip(),
        away_team=away.strip(),
        side=row.get("side") or "",
        line=row.get("line"),
        event_start_time=_parse_timestamp(row.get("event_start_time")),
        raw_line=row.get("raw_line"),
    )


def provider_event_from_market(
    provider_name: str, market: Market, parsed: RawGameEvent | None,
) -> ProviderEvent | None:
    """Combine a provider's parse_game_event() output with the Market it
    came from into a full ProviderEvent. None in, None out -- a market
    that didn't parse is never a candidate."""
    if parsed is None:
        return None
    return ProviderEvent(
        provider=provider_name,
        market_id=market.id,
        home_team=parsed.home_team,
        away_team=parsed.away_team,
        market_type=parsed.market_type,
        side=parsed.side,
        line=parsed.line,
        event_start_time=parsed.event_start_time,
        raw_market=market,
        league=parsed.league,
        yes_team=parsed.yes_team,
        no_team=parsed.no_team,
        event_id=parsed.event_id,
        event_date=parsed.event_date,
        strict_identity=parsed.strict_identity,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Team-name normalization ─────────────────────────────────────────
#
# Mirrors src/pinnacle_feed.py's normalize_team_name/_token_overlap/
# _match_teams -- kept as an independent copy (not an import) so this
# module has no import-time dependency on the Pinnacle feed, which is a
# live, separately-changing SGO/Pinnacle integration with its own
# load_dotenv() side effect and unrelated lookup-table machinery. If the
# two copies drift, that's an accepted tradeoff for keeping the
# execution layer decoupled from an unrelated odds feed.


def _repair_mojibake(text: str) -> str:
    """Recover accents from latin-1 bytes that were decoded as UTF-8."""
    if "�" not in text:
        return text
    try:
        return text.encode("latin-1", errors="replace").decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def normalize_name(text: str) -> str:
    """Lowercase, strip diacritics and collapse whitespace."""
    text = _repair_mojibake(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


def normalize_team_name(text: str) -> str:
    """Team names may be 'Royals' on one side and 'Kansas City Royals' on the other."""
    return normalize_name(text)


def token_overlap(a: str, b: str) -> float:
    """Fraction of the shorter token set that appears in the longer one."""
    ta = {t for t in a.split() if t}
    tb = {t for t in b.split() if t}
    if not ta or not tb:
        return 0.0
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(smaller & larger) / len(smaller)


def _fuzzy_team_match(a: str, b: str) -> bool:
    """a and b are already normalize_team_name'd. True if one is a
    substring of the other, every token of the shorter is present in
    the longer, OR (Polymarket-slug case) one is a short abbreviation
    that prefixes a token of the other, e.g. "ten" vs "tennessee"."""
    if a == b:
        return True
    if a in b or b in a:
        return True
    if token_overlap(a, b) >= 1.0:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) <= 4 and any(tok.startswith(shorter) for tok in longer.split()):
        return True
    return False


def match_teams(rec_home: str, rec_away: str, prov_home: str | None, prov_away: str | None) -> bool:
    """True if the provider's two team names correspond (in either
    orientation) to the recommendation's two team names, allowing
    fuzzy/abbreviation matches on each side."""
    if prov_home is None or prov_away is None:
        return False
    rh, ra = normalize_team_name(rec_home), normalize_team_name(rec_away)
    ph, pa = normalize_team_name(prov_home), normalize_team_name(prov_away)
    same_orientation = _fuzzy_team_match(rh, ph) and _fuzzy_team_match(ra, pa)
    swapped_orientation = _fuzzy_team_match(rh, pa) and _fuzzy_team_match(ra, ph)
    return same_orientation or swapped_orientation


def _teams_are_exact(rec_home: str, rec_away: str, prov_home: str | None, prov_away: str | None) -> bool:
    if prov_home is None or prov_away is None:
        return False
    rec_set = {normalize_team_name(rec_home), normalize_team_name(rec_away)}
    prov_set = {normalize_team_name(prov_home), normalize_team_name(prov_away)}
    return rec_set == prov_set


# ── Scoring ──────────────────────────────────────────────────────────


def _team_score(rec: RecommendationEvent, prov: ProviderEvent) -> float:
    if _teams_are_exact(rec.home_team, rec.away_team, prov.home_team, prov.away_team):
        return 1.0
    if match_teams(rec.home_team, rec.away_team, prov.home_team, prov.away_team):
        return _TEAM_MATCH_FUZZY_SCORE
    return 0.0


def _market_type_score(rec: RecommendationEvent, prov: ProviderEvent) -> float:
    rec_logical = _LOGICAL_MARKET_TYPE.get(rec.market_type)
    if rec_logical is None or prov.market_type is None:
        return 0.0
    return 1.0 if rec_logical == prov.market_type else 0.0


def _line_score(rec: RecommendationEvent, prov: ProviderEvent) -> float:
    logical = _LOGICAL_MARKET_TYPE.get(rec.market_type)
    if logical == "moneyline":
        return 1.0  # not applicable -- treated as a non-issue, not evidence of a match
    if rec.line is None or prov.line is None:
        return 0.0
    return 1.0 if abs(rec.line - prov.line) < 1e-6 else 0.0


def _date_score(rec: RecommendationEvent, prov: ProviderEvent) -> float:
    if prov.event_start_time is None:
        return 1.0  # no evidence either way -- don't penalize a parse with no date field
    if rec.event_start_time is None:
        return 0.0
    delta_hours = abs((rec.event_start_time - prov.event_start_time).total_seconds()) / 3600.0
    if delta_hours <= _DATE_PROXIMITY_MINUTES / 60.0:
        return 1.0
    if delta_hours >= _DATE_DECAY_HOURS:
        return 0.0
    return 1.0 - (delta_hours / _DATE_DECAY_HOURS)


def score_match(rec: RecommendationEvent, prov: ProviderEvent) -> MatchResult:
    """Score how well *prov* matches *rec*. Always returns a MatchResult
    (never None) -- the raw score is useful for logging/tests regardless
    of whether it would clear any threshold. Threshold enforcement is
    find_best_match's job, not this function's."""
    team = _team_score(rec, prov)
    market_type = _market_type_score(rec, prov)
    line = _line_score(rec, prov)
    date = _date_score(rec, prov)
    confidence = (
        team * _TEAM_MATCH_WEIGHT
        + market_type * _MARKET_TYPE_WEIGHT
        + line * _LINE_WEIGHT
        + date * _DATE_WEIGHT
    )
    return MatchResult(
        recommendation_id=rec.recommendation_id,
        provider=prov.provider,
        provider_market_id=prov.market_id,
        confidence=confidence,
        team_score=team,
        market_type_score=market_type,
        line_score=line,
        date_score=date,
        provider_title=prov.raw_market.title,
    )


# ── Exact (strict-identity) contract resolution ─────────────────────
#
# For providers whose contracts carry verified structure (Kalshi), a
# recommendation maps to a contract only if EVERY dimension matches
# exactly -- league, market type, event date (and start time where the
# provider gives one), both teams, line, and side semantics. There is no
# fuzzy tier here: anything that does not match exactly is "no match".

_START_TIME_TOLERANCE = timedelta(minutes=60)

# Leagues where a game can END TIED, so a moneyline contract's settlement on
# a tie (both "wins" contracts NO? a refund? a void?) matters. Neither
# Kalshi's nor Polymarket US's tie handling has been verified from contract
# rules or settlement data, and it must not be inferred from sportsbook
# grading (where a tie is a PUSH). Moneyline for these leagues is therefore
# UNSUPPORTED for prediction-market execution until verified. Spread/total
# markets are unaffected: their lines are x.5 (verified) so a tie cannot push.
# MLB (extra innings) and WNBA (overtime) cannot tie.
TIE_POSSIBLE_MONEYLINE_LEAGUES = frozenset({"NFL"})


def _is_half_integer(value: float) -> bool:
    doubled = value * 2
    return abs(doubled - round(doubled)) < 1e-9 and round(doubled) % 2 == 1


def _eastern_date(dt: datetime) -> date | None:
    if EASTERN is None:
        return None
    return dt.astimezone(EASTERN).date()


def resolve_strict_side(rec: RecommendationEvent, prov: ProviderEvent) -> tuple[str | None, str]:
    """Which side (YES/NO) of *prov*'s contract exactly equals *rec*, or
    (None, reason). Reasons are stable short codes (used by tests and
    diagnostics)."""
    logical = _LOGICAL_MARKET_TYPE.get(rec.market_type)
    if logical is None or logical != prov.market_type:
        return None, "market_type_mismatch"
    if not prov.league or (rec.league or "").upper() != prov.league.upper():
        return None, "league_mismatch"

    # Event identity: the same Eastern calendar day, and for providers
    # that expose a scheduled start, the same start (guards doubleheaders).
    if rec.event_start_time is None or prov.event_date is None:
        return None, "date_unknown"
    if _eastern_date(rec.event_start_time) != prov.event_date:
        return None, "date_mismatch"
    if prov.event_start_time is not None:
        if abs(rec.event_start_time - prov.event_start_time) > _START_TIME_TOLERANCE:
            return None, "start_time_mismatch"

    home = canonical_team_name(rec.league, rec.home_team)
    away = canonical_team_name(rec.league, rec.away_team)
    if home is None or away is None or home == away:
        return None, "unknown_team"
    if {home, away} != {prov.home_team, prov.away_team}:
        return None, "teams_mismatch"

    side = (rec.side or "").upper()

    if logical == "total":
        if prov.line is None or rec.line is None or abs(rec.line - prov.line) > 1e-9:
            return None, "line_mismatch"
        if not _is_half_integer(prov.line):
            return None, "line_not_half_point"
        if side == "OVER":
            return "YES", "ok"
        if side == "UNDER":
            return "NO", "ok"
        return None, "side_unrecognized"

    if side not in ("HOME", "AWAY"):
        return None, "side_unrecognized"
    rec_team, opp_team = (home, away) if side == "HOME" else (away, home)

    if logical == "moneyline" and prov.league.upper() in TIE_POSSIBLE_MONEYLINE_LEAGUES:
        return None, "tie_settlement_unverified"

    if logical == "moneyline":
        # YES only when the contract's YES team IS the rec-side team. A
        # venue with one two-outcome market per game (Polymarket US)
        # states its NO side's team explicitly (no_team) -- that is a
        # first-class outcome, so NO is exact there. Venues with one
        # market PER TEAM (Kalshi) leave no_team None: the opposing
        # team's market is deliberately NOT used via NO (a tie would make
        # it a different bet), and every event lists both teams' markets.
        if prov.yes_team == rec_team:
            return "YES", "ok"
        if prov.no_team is not None and prov.no_team == rec_team:
            return "NO", "ok"
        return None, "wrong_team_market"

    # spread: rec_team covers iff margin(rec_team) + raw_line > 0
    raw_line = rec.raw_line
    if raw_line is None or prov.line is None:
        return None, "line_unknown"
    if raw_line == 0 or not _is_half_integer(raw_line):
        return None, "line_not_half_point"   # pick'em / whole numbers can push
    if abs(raw_line) != prov.line:
        return None, "line_mismatch"
    if raw_line < 0:
        # laying points: covers iff wins by MORE than |line|
        if prov.yes_team == rec_team:
            return "YES", "ok"
        return None, "wrong_team_market"
    # getting points: covers iff the OPPONENT does NOT win by more than
    # line (exact complement, safe only because the line is x.5)
    if prov.yes_team == opp_team:
        return "NO", "ok"
    return None, "wrong_team_market"


def find_best_match(
    rec: RecommendationEvent,
    candidates: list[ProviderEvent],
    min_confidence: float = MIN_MARKET_MATCH_CONFIDENCE_DEFAULT,
) -> MatchResult | None:
    """The best-scoring candidate, or None if there are no candidates or
    none clears *min_confidence*. Never returns a MatchResult below the
    threshold -- there is no "low confidence match" value to fall back
    on by mistake.

    Strict-identity candidates (Kalshi) are resolved exactly: they either
    resolve to a YES/NO side (confidence 1.0) or are dropped. If exact
    resolution finds more than one distinct contract or event the result
    is ambiguous and NO match is returned."""
    if not candidates:
        return None

    strict_hits: list[tuple[MatchResult, ProviderEvent]] = []
    legacy_results: list[MatchResult] = []
    for prov in candidates:
        if prov.strict_identity:
            side, _reason = resolve_strict_side(rec, prov)
            if side is None:
                continue
            strict_hits.append((MatchResult(
                recommendation_id=rec.recommendation_id,
                provider=prov.provider,
                provider_market_id=prov.market_id,
                confidence=1.0,
                team_score=1.0, market_type_score=1.0, line_score=1.0, date_score=1.0,
                provider_title=prov.raw_market.title,
                provider_side=side,
            ), prov))
        else:
            legacy_results.append(score_match(rec, prov))

    if strict_hits:
        if len({p.market_id for _r, p in strict_hits}) > 1 or len({p.event_id for _r, p in strict_hits}) > 1:
            return None  # ambiguous: never pick between competing exact matches
        best = strict_hits[0][0]
        return best if best.confidence >= min_confidence else None

    if not legacy_results:
        return None
    best = max(legacy_results, key=lambda r: r.confidence)
    if best.confidence < min_confidence:
        return None
    return best
