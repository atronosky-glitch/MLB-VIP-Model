"""Game-level market matching: does a recommendation have an equivalent
contract on a prediction-market provider, and how confident are we?

Scope (2026-09-12): moneyline only for now. game_spread_ou/game_runline_ou/
game_total_ou rows are recognized but always score 0.0 on the line
dimension until line-matching is implemented as the next unit of work in
this same stage -- see the Stage 2 plan. Player-prop market types are out
of scope entirely (neither provider has meaningful player-prop coverage
for this model's leagues today).

Pure, DB-free, network-free module -- persistence lives in
src/execution/market_match_store.py, provider-specific market parsing
lives on each provider class (KalshiProvider.parse_game_event,
PolymarketUSProvider.parse_game_event), so this module never has a
provider-conditional branch.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.execution.base import Market, RawGameEvent

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


def find_best_match(
    rec: RecommendationEvent,
    candidates: list[ProviderEvent],
    min_confidence: float = MIN_MARKET_MATCH_CONFIDENCE_DEFAULT,
) -> MatchResult | None:
    """The best-scoring candidate, or None if there are no candidates or
    none clears *min_confidence*. Never returns a MatchResult below the
    threshold -- there is no "low confidence match" value to fall back
    on by mistake."""
    if not candidates:
        return None
    results = [score_match(rec, prov) for prov in candidates]
    best = max(results, key=lambda r: r.confidence)
    if best.confidence < min_confidence:
        return None
    return best
