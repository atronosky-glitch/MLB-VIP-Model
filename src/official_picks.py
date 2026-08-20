"""Official Pick Qualification, Ranking, and Daily Selection.

Every recommendation is classified into one of three tiers:
  - OFFICIAL_TRACKED: meets all qualification thresholds, will be graded
  - DISCOVERY_TRACKED: high-quality but below official thresholds, private research only
  - RESEARCH_ONLY: stored and graded for threshold calibration, but not promoted

Daily selection limits apply after qualification:
  - Maximum 3 official picks per calendar day
  - Maximum 1 official pick per game
  - No duplicate player/market/side/line combinations

All thresholds are in OfficialPickConfig — never hardcoded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import prop_config as cfg

logger = logging.getLogger(__name__)

# ── Tier constants ─────────────────────────────────────────────────

TIER_OFFICIAL = "OFFICIAL_TRACKED"
TIER_DISCOVERY = "DISCOVERY_TRACKED"
TIER_RESEARCH = "RESEARCH_ONLY"
RULES_VERSION = "official_pick_rules_v2"


# ── Configuration ──────────────────────────────────────────────────


@dataclass(frozen=True)
class OfficialPickConfig:
    """All qualification thresholds in one place."""

    # Official tier
    official_min_model_score: float = 7.0
    official_min_ou_ev_pct: float = 3.0
    official_min_yn_price_adv_pp: float = 3.0
    official_min_books: int = 2
    official_allowed_statuses: tuple[str, ...] = (
        "QUALIFIED", "STRONG_EDGE", "POSITIVE_EDGE",
        "STRONG_PRICE_OUTLIER", "PRICE_OUTLIER", "MARGINAL_PRICE_OUTLIER",
    )
    official_daily_max_picks: int = 3
    official_max_per_game: int = 1
    official_max_per_player: int = 1
    official_rules_version: str = RULES_VERSION

    # Discovery tier (private research only)
    discovery_min_model_score: float = 6.0
    discovery_min_books: int = 2
    discovery_allowed_statuses: tuple[str, ...] = (
        "QUALIFIED", "STRONG_EDGE", "POSITIVE_EDGE",
        "STRONG_PRICE_OUTLIER", "PRICE_OUTLIER", "MARGINAL_PRICE_OUTLIER",
    )


DEFAULT_CONFIG = OfficialPickConfig()


# ── Qualification Result ───────────────────────────────────────────


@dataclass
class QualificationResult:
    """Outcome of classifying a single recommendation."""

    tier: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    disqualification_reasons: list[str] = field(default_factory=list)
    contributing_book_count: int = 0
    contributing_books: str = ""
    applicable_edge_metric: str = ""
    applicable_edge_threshold: float = 0.0
    model_score_threshold: float = 7.0
    rules_version: str = RULES_VERSION

    def to_dict(self) -> dict:
        return {
            "recommendation_tier": self.tier,
            "qualification_passed": 1 if self.passed else 0,
            "qualification_reasons": "; ".join(self.reasons),
            "disqualification_reasons": "; ".join(self.disqualification_reasons),
            "contributing_book_count": self.contributing_book_count,
            "contributing_books": self.contributing_books,
            "applicable_edge_metric": self.applicable_edge_metric,
            "applicable_edge_threshold": self.applicable_edge_threshold,
            "model_score_threshold": self.model_score_threshold,
            "qualification_rules_version": self.rules_version,
            "qualification_timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ── Classification logic ───────────────────────────────────────────

_EXCLUDED_QUALITY_STATUSES = frozenset({
    "PRICE_OUTLIER", "STRONG_PRICE_OUTLIER", "MARGINAL_PRICE_OUTLIER",
    "NEEDS_REVIEW", "INSUFFICIENT_MARKET", "EXCLUDED",
})

_LIVE_COMPLETED_STATUSES = frozenset({
    "live", "inprogress", "in_progress", "started", "in-progress",
    "final", "finished", "completed", "closed", "ended",
    "postponed", "suspended", "cancelled",
})

_POSTPONED_CANCELLED_STATUSES = frozenset({
    "postponed", "cancelled", "suspended",
})


def classify_recommendation(
    rec: dict,
    config: OfficialPickConfig | None = None,
) -> QualificationResult:
    """Classify a recommendation into OFFICIAL_TRACKED, DISCOVERY_TRACKED, or RESEARCH_ONLY.

    Classification order:
    1. Check all official gates → OFFICIAL_TRACKED
    2. Check relaxed discovery gates → DISCOVERY_TRACKED
    3. Everything else → RESEARCH_ONLY
    """
    if config is None:
        config = DEFAULT_CONFIG

    reasons: list[str] = []
    disq: list[str] = []

    # ── Gather fields ──────────────────────────────────────────

    model_score = rec.get("model_score")
    rec_status = (rec.get("rec_status") or "").strip()
    market_quality = (rec.get("market_quality") or "").strip()
    freshness = (rec.get("freshness_status") or "").strip().upper()
    event_status = (rec.get("event_status") or "").strip().lower()
    n_books = rec.get("n_consensus_books") or 0
    market_form = (rec.get("market_form") or "").strip().lower()
    ev_pct = rec.get("ev_pct")
    yn_adv = rec.get("yn_implied_prob_adv")
    mapping_conf = (rec.get("mapping_confidence") or "").strip().upper()
    market_type = (rec.get("market_type") or "").strip()

    # Collect contributing books from the opp if available
    contributing_books_str = rec.get("contributing_books", "")
    contributing_book_count = n_books

    # ── Gate 1: Market quality ─────────────────────────────────

    if market_quality in _EXCLUDED_QUALITY_STATUSES:
        disq.append(f"Market quality '{market_quality}' is not eligible for official status")

    if market_type and not cfg.is_auto_settleable_market(market_type):
        disq.append("Market has no verified automatic settlement field")

    # ── Gate 2: Freshness ──────────────────────────────────────

    if freshness == "STALE":
        disq.append("Data is stale")

    # ── Gate 3: Game status ────────────────────────────────────

    if event_status in _LIVE_COMPLETED_STATUSES:
        disq.append(f"Game is not scheduled (status={event_status})")

    # ── Gate 4: Model score ────────────────────────────────────

    if model_score is None:
        disq.append("Model score is not available")
    elif model_score < config.official_min_model_score:
        disq.append(
            f"Model Score {model_score:.1f} < {config.official_min_model_score}"
        )

    # ── Gate 5: Rec status ─────────────────────────────────────

    if rec_status not in config.official_allowed_statuses:
        disq.append(
            f"Status '{rec_status}' not in allowed {list(config.official_allowed_statuses)}"
        )

    # ── Gate 6: Contributing books ─────────────────────────────

    if contributing_book_count < config.official_min_books:
        disq.append(
            f"Only {contributing_book_count} contributing book(s); "
            f"requires {config.official_min_books}"
        )

    # ── Gate 7: Mapping confidence ─────────────────────────────

    if mapping_conf in ("LOW", "NONE", "FAILED", "REJECTED"):
        disq.append(f"Mapping confidence '{mapping_conf}' is too low")

    # ── Gate 8: Edge metric ────────────────────────────────────

    if market_form == "ou":
        edge_val = ev_pct if ev_pct is not None else 0.0
        threshold = config.official_min_ou_ev_pct
        applicable_metric = "ev_pct"
        if edge_val < threshold:
            disq.append(
                f"O/U EV {edge_val:.2f}% < {threshold}%"
            )
    elif market_form == "yn":
        edge_val = yn_adv if yn_adv is not None else 0.0
        threshold = config.official_min_yn_price_adv_pp
        applicable_metric = "yn_implied_prob_adv"
        if edge_val < threshold:
            disq.append(
                f"Y/N Price Advantage {edge_val:.2f} pp < {threshold} pp"
            )
    else:
        edge_val = 0.0
        threshold = 0.0
        applicable_metric = "unknown"
        disq.append(f"Unknown market form '{market_form}'")

    # ── Gate 9: Pinnacle approval (O/U official picks only) ───────

    pinnacle_approved = rec.get("pinnacle_approved")
    if market_form == "ou" and getattr(cfg, "REQUIRE_PINNACLE_FOR_OFFICIAL", False):
        if not pinnacle_approved:
            # LOO fallback is allowed only when no Pinnacle entry exists at
            # the exact market. A present but one-sided/mismatched Pinnacle
            # record remains blocked rather than being treated as unsupported.
            loo_fallback = (
                rec.get("pinnacle_found") is False
                and getattr(cfg, "PINNACLE_FALLBACK_TO_MARKET_MEDIAN", True)
            )
            if not loo_fallback:
                disq.append("Pinnacle approval required for official status")
                logger.warning(
                    "OFFICIAL_BLOCKED_REQUIRE_PINNACLE player=%s market=%s line=%s "
                    "reason=%s",
                    rec.get("player_id", "?"), rec.get("market_type", "?"),
                    rec.get("line", "?"),
                    "missing_pinnacle" if pinnacle_approved is None else "pinnacle_threshold_failed",
                )
            else:
                reasons.append("Qualified via exact-market LOO fallback; Pinnacle unavailable")

    # ── Gate 10: Reliable EV provenance (O/U only) ───────────────
    # Older direct callers may omit the field; the production pipeline marks
    # every new recommendation explicitly before classification.
    if market_form == "ou" and rec.get("reliable_ev_checked"):
        if not rec.get("reliable_ev"):
            details = ", ".join(rec.get("reliable_ev_reasons") or ["validation_failed"])
            disq.append(f"EV reliability gate failed ({details})")

    # ── Gate 11: Valid identity fields ──────────────────────────

    if not rec.get("event_id"):
        disq.append("Missing event_id")
    if not rec.get("player_id"):
        disq.append("Missing player_id")
    if not rec.get("side"):
        disq.append("Missing side")
    if not rec.get("sportsbook"):
        disq.append("Missing sportsbook")

    # ── Gate 12: Reference odds for YN ────────────────────────

    if market_form == "yn" and not rec.get("yn_reference_odds"):
        disq.append("Missing YN reference odds")

    # ── Determine tier ─────────────────────────────────────────

    passed = len(disq) == 0

    if passed:
        if market_form == "ou":
            reasons.append(
                f"Qualified: Model Score {model_score:.1f}, "
                f"EV {ev_pct:.2f}%, {n_books} books, "
                f"{freshness} data, {rec_status} status."
            )
        else:
            reasons.append(
                f"Qualified: Model Score {model_score:.1f}, "
                f"Price Advantage {yn_adv:.2f} pp, {n_books} books, "
                f"{freshness} data, {rec_status} status."
            )
        tier = TIER_OFFICIAL
    else:
        # ── Discovery tier (relaxed gates) ──────────────────────
        discovery_disq: list[str] = []

        if freshness == "STALE":
            discovery_disq.append("Data is stale")
        if event_status in _LIVE_COMPLETED_STATUSES:
            discovery_disq.append(f"Game is not scheduled (status={event_status})")
        if model_score is None:
            discovery_disq.append("Model score is not available")
        elif model_score < config.discovery_min_model_score:
            discovery_disq.append(
                f"Model Score {model_score:.1f} < {config.discovery_min_model_score}"
            )
        if contributing_book_count < config.discovery_min_books:
            discovery_disq.append(
                f"Only {contributing_book_count} book(s); "
                f"discovery requires {config.discovery_min_books}"
            )
        if mapping_conf in ("LOW", "NONE", "FAILED", "REJECTED"):
            discovery_disq.append(f"Mapping confidence '{mapping_conf}' is too low")
        if rec_status not in config.discovery_allowed_statuses:
            discovery_disq.append(f"Status '{rec_status}' not in discovery-allowed statuses")
        if market_type and not cfg.is_auto_settleable_market(market_type):
            discovery_disq.append("Market has no verified automatic settlement field")
        if not rec.get("event_id"):
            discovery_disq.append("Missing event_id")
        if not rec.get("player_id"):
            discovery_disq.append("Missing player_id")
        if not rec.get("side"):
            discovery_disq.append("Missing side")
        if not rec.get("sportsbook"):
            discovery_disq.append("Missing sportsbook")

        if len(discovery_disq) == 0:
            tier = TIER_DISCOVERY
            reasons.append(
                f"Discovery: Model Score {model_score:.1f}, "
                f"{n_books} books, {freshness} data — "
                f"does not count toward official record."
            )
            # Preserve official disqualification reasons for auditability.
        else:
            tier = TIER_RESEARCH
            reasons.append("Not official: " + "; ".join(disq[:5]))

    return QualificationResult(
        tier=tier,
        passed=passed,
        reasons=reasons,
        # Keep the official-gate failures for Discovery rows so production
        # audits can distinguish Pinnacle blocks from other failures.
        disqualification_reasons=disq if tier != TIER_OFFICIAL else [],
        contributing_book_count=contributing_book_count,
        contributing_books=contributing_books_str,
        applicable_edge_metric=applicable_metric if passed else "",
        applicable_edge_threshold=threshold if passed else 0.0,
        model_score_threshold=config.official_min_model_score,
        rules_version=RULES_VERSION,
    )


# ── Daily selection and ranking ────────────────────────────────────


def _pick_key(rec: dict) -> str:
    """Unique key for deduplication: player + market + side + line."""
    return "|".join([
        str(rec.get("player_id", "")),
        str(rec.get("market_type", "")),
        str(rec.get("side", "")),
        str(rec.get("line", "")),
    ])


def _game_key(rec: dict) -> str:
    """Game-level key for per-game limiting."""
    return str(rec.get("event_id", ""))


def rank_and_select_official_picks(
    qualified_recs: list[dict],
    config: OfficialPickConfig | None = None,
    already_selected_today: list[dict] | None = None,
) -> list[dict]:
    """Rank qualified recommendations and select up to daily_max_picks.

    Applies:
    1. Sort by model_score desc, edge_metric desc, book_count desc, freshness
    2. Daily max limit (default 3)
    3. Per-game limit (default 1)
    4. No duplicate player/market/side/line
    5. Already selected today carry forward

    Returns list of dicts with 'official_rank' added.
    """
    if config is None:
        config = DEFAULT_CONFIG

    selected: list[dict] = []
    seen_picks: set[str] = set()
    game_counts: dict[str, int] = {}
    player_counts: dict[str, int] = {}

    # Pre-populate from already-selected picks today
    if already_selected_today:
        for pick in already_selected_today:
            pk = _pick_key(pick)
            gk = _game_key(pick)
            seen_picks.add(pk)
            game_counts[gk] = game_counts.get(gk, 0) + 1
            player_id = str(pick.get("player_id", ""))
            if player_id:
                player_counts[player_id] = player_counts.get(player_id, 0) + 1
            selected.append(pick)

    # Sort eligible candidates
    def sort_key(rec):
        ms = rec.get("model_score") or 0.0
        # Rank by the observed opportunity, not the minimum threshold that
        # happened to qualify it. Thresholds are policy, not edge magnitude.
        edge = rec.get("ev_pct") if (rec.get("market_form") or "").lower() == "ou" else rec.get("yn_implied_prob_adv")
        edge = edge or 0.0
        books = rec.get("n_consensus_books") or 0
        freshness_penalty = 0 if (rec.get("freshness_status") or "").upper() == "FRESH" else 1
        return (-ms, -edge, -books, freshness_penalty)

    if any("recommendation_tier" in rec for rec in qualified_recs):
        qualified_recs = [
            rec for rec in qualified_recs
            if rec.get("recommendation_tier") == TIER_OFFICIAL
            and rec.get("qualification_passed", 0)
        ]
    candidates = sorted(qualified_recs, key=sort_key)

    for rec in candidates:
        if len(selected) >= config.official_daily_max_picks:
            break

        pk = _pick_key(rec)
        gk = _game_key(rec)
        player_id = str(rec.get("player_id", ""))

        # Skip duplicates
        if pk in seen_picks:
            continue

        # Per-game limit
        if game_counts.get(gk, 0) >= config.official_max_per_game:
            continue

        if player_id and player_counts.get(player_id, 0) >= config.official_max_per_player:
            continue

        seen_picks.add(pk)
        game_counts[gk] = game_counts.get(gk, 0) + 1
        if player_id:
            player_counts[player_id] = player_counts.get(player_id, 0) + 1

        rank = len(selected) + 1
        rec["official_rank"] = rank
        selected.append(rec)

    return selected


# ── Duplicate-pick suppression ───────────────────────────────────────
#
# A scanner that reruns every few minutes must not freeze a new official
# pick every time a price wiggles a cent — that inflates the historical
# record with duplicated signal for what is, in reality, one wager. See
# the module docstring's tier system for how a recommendation becomes
# eligible in the first place; everything below decides whether a newly
# eligible recommendation for an already-picked bet is the SAME pick
# (suppress) or a MATERIAL_UPDATE (freeze a new pick, supersede the old).

PICK_STATUS_ACTIVE = "ACTIVE"
PICK_STATUS_SUPERSEDED = "SUPERSEDED"

UPDATE_DUPLICATE = "DUPLICATE"
UPDATE_MATERIAL = "MATERIAL_UPDATE"


def compute_bet_slot_key(rec: dict) -> str:
    """Identity key for "the same logical bet" across rescans.

    Deliberately excludes line/price/sportsbook — those are exactly the
    dimensions ``classify_pick_update`` decides materiality on. Two scans
    of the same event/player/market/side are the same bet slot even if
    the number or the book quoting it changed.
    """
    return "|".join([
        str(rec.get("event_id", "")),
        str(rec.get("player_id", "")),
        str(rec.get("market_type", "")),
        str(rec.get("side", "")),
    ])


def classify_pick_update(existing: dict, candidate: dict) -> str:
    """Decide whether *candidate* is the same pick as *existing* or a
    material update that should supersede it.

    Rules (see MATERIAL_PRICE_DELTA_PP in prop_config.py for the exact
    threshold and reasoning):
    - A line change is always material — a different number is a
      structurally different wager, never "the same pick."
    - Otherwise, compare implied probability of the American odds; a
      move of at least MATERIAL_PRICE_DELTA_PP percentage points is
      material. Smaller moves (a book's price wiggling a cent or two)
      are the same pick, just re-observed.
    """
    from .market_analysis import american_to_probability

    existing_line = existing.get("line")
    candidate_line = candidate.get("line")
    if existing_line != candidate_line:
        return UPDATE_MATERIAL

    existing_odds = existing.get("offered_american_odds") or existing.get("latest_american_odds")
    candidate_odds = candidate.get("offered_american_odds")
    if existing_odds is None or candidate_odds is None:
        # No comparable price on one side (e.g. YN markets without a
        # tracked American price) — cannot prove it's the same pick, so
        # treat it as a fresh opportunity rather than silently suppress.
        return UPDATE_MATERIAL

    try:
        existing_prob = american_to_probability(int(existing_odds))
        candidate_prob = american_to_probability(int(candidate_odds))
    except (TypeError, ValueError, ZeroDivisionError):
        return UPDATE_MATERIAL

    delta = abs(candidate_prob - existing_prob)
    return UPDATE_MATERIAL if delta >= cfg.MATERIAL_PRICE_DELTA_PP else UPDATE_DUPLICATE
