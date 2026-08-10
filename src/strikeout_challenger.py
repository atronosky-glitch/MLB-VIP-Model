"""Independent pitcher-strikeout challenger model.

This is a shadow-only statistical baseline. It never reads sportsbook odds,
changes qualification, or produces production picks. It converts verified MLB
season strikeout/batter-faced data into a conservative Poisson projection and
provides chronological evaluation metrics.
"""

from __future__ import annotations

import math
import functools
import requests
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


CHALLENGER_VERSION = "strikeout_challenger_v1"
DEFAULT_PRIOR_K_RATE = 0.225
DEFAULT_PRIOR_BATTERS = 50.0


@dataclass(frozen=True)
class StrikeoutProjection:
    expected_strikeouts: float
    over_probability: float
    under_probability: float
    push_probability: float
    fair_probability: float
    version: str = CHALLENGER_VERSION

    def to_dict(self) -> dict:
        return {
            "challenger_expected_strikeouts": round(self.expected_strikeouts, 4),
            "challenger_over_probability": round(self.over_probability, 6),
            "challenger_under_probability": round(self.under_probability, 6),
            "challenger_push_probability": round(self.push_probability, 6),
            "challenger_version": self.version,
        }


def _poisson_probability(mean: float, value: int) -> float:
    if value < 0:
        return 0.0
    return math.exp(-mean) * (mean ** value) / math.factorial(value)


def _poisson_cdf(mean: float, maximum: int) -> float:
    return sum(_poisson_probability(mean, value) for value in range(maximum + 1))


def project_strikeouts(
    *,
    strikeouts: float,
    batters_faced: float,
    games_started: float,
    line: float,
    side: str,
    opponent_k_rate: float | None = None,
    prior_k_rate: float = DEFAULT_PRIOR_K_RATE,
    prior_batters: float = DEFAULT_PRIOR_BATTERS,
) -> StrikeoutProjection | None:
    """Project a strikeout line using no sportsbook inputs.

    The pitcher rate is shrunk toward a league prior. Expected batters faced
    is the pitcher's verified season average per start. An opponent rate may
    be supplied later once its historical data contract is available.
    """
    if strikeouts < 0 or batters_faced <= 0 or games_started <= 0 or line < 0:
        return None
    pitcher_rate = (strikeouts + prior_k_rate * prior_batters) / (batters_faced + prior_batters)
    rate = (pitcher_rate + opponent_k_rate) / 2 if opponent_k_rate is not None else pitcher_rate
    if not 0 <= rate <= 1:
        return None
    mean = rate * (batters_faced / games_started)
    lower = math.floor(line)
    exact = _poisson_probability(mean, lower) if line == lower else 0.0
    under = _poisson_cdf(mean, lower - 1 if line == lower else lower)
    over = 1.0 - under - exact
    side_upper = side.upper()
    fair = over if side_upper == "OVER" else under if side_upper == "UNDER" else None
    if fair is None:
        return None
    return StrikeoutProjection(mean, max(0.0, over), max(0.0, under), exact, fair)


def evaluate_challenger(records: Iterable[dict], *, min_sample: int = 30) -> dict:
    """Evaluate chronological shadow predictions without changing production."""
    usable = [
        record for record in records
        if record.get("challenger_fair_probability") is not None
        and record.get("outcome") in ("WIN", "LOSS")
    ]
    usable.sort(key=lambda row: row.get("timestamp", ""))
    if not usable:
        return {"sample_size": 0, "status": "INSUFFICIENT_DATA"}
    brier = sum(
        (float(row["challenger_fair_probability"]) - (1.0 if row["outcome"] == "WIN" else 0.0)) ** 2
        for row in usable
    ) / len(usable)
    return {
        "sample_size": len(usable),
        "brier_score": round(brier, 6),
        "status": "SUFFICIENT" if len(usable) >= min_sample else "INSUFFICIENT_DATA",
        "chronological": True,
        "version": CHALLENGER_VERSION,
    }


@functools.lru_cache(maxsize=128)
def project_current_player(player_name: str, line: float, side: str) -> dict | None:
    """Build a shadow projection from MLB's current season stats.

    This is deliberately independent of sportsbook odds. Ambiguous player
    searches or missing pitching stats return ``None`` and do not affect picks.
    """
    try:
        search = requests.get(
            "https://statsapi.mlb.com/api/v1/people/search",
            params={"names": player_name}, timeout=15,
        ).json().get("people", [])
        pitchers = [p for p in search if (p.get("primaryPosition") or {}).get("type") == "Pitcher"]
        if len(pitchers) != 1:
            return None
        player_id = pitchers[0].get("id")
        stats_payload = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats",
            params={"stats": "season", "group": "pitching", "season": datetime.now().year},
            timeout=15,
        ).json()
        splits = stats_payload.get("stats", [{}])[0].get("splits", [])
        stat = next((s.get("stat", {}) for s in splits if s.get("gameType") == "R"), None)
        if not stat:
            return None
        projection = project_strikeouts(
            strikeouts=float(stat.get("strikeOuts")),
            batters_faced=float(stat.get("battersFaced")),
            games_started=float(stat.get("gamesStarted")),
            line=float(line), side=side,
        )
        return projection.to_dict() if projection else None
    except (requests.RequestException, TypeError, ValueError, KeyError, IndexError):
        return None
