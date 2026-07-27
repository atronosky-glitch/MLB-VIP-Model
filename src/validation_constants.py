"""Centralised validation statuses for participant-mapping gate.

Single source of truth consumed by the parser, database, analysis,
and display layers.  NEVER duplicate these constants elsewhere.
"""

# ── Individual-row validation statuses ─────────────────────────────
# Assigned to each (odd_id, sportsbook) pair after participant-mapping
# and consensus-based sign analysis.

STATUS_VALID = "VALID"                          # mapping correct, no issues
STATUS_CONFIRMED = "CONFIRMED"                   # mapping cross-verified by stable API identifiers
STATUS_VERIFIED = "VERIFIED"                     # mapping verified (synonym for display clarity)
STATUS_POSSIBLE_MAPPING_ERROR = "POSSIBLE_MAPPING_ERROR"  # sign contradicts consensus, possible swap
STATUS_INVALID_MAPPING = "INVALID_MAPPING"      # API identifiers prove wrong entity
STATUS_UNVERIFIED = "UNVERIFIED"                 # no verification possible
STATUS_NONE = "NONE"                             # no status assigned
STATUS_UNKNOWN = "UNKNOWN"                       # unrecognised entity ID


# ── Approved for model calculations ───────────────────────────────
APPROVED_STATUSES = frozenset({
    STATUS_VALID,
    STATUS_CONFIRMED,
    STATUS_VERIFIED,
})


# ── Explicitly excluded from model calculations ───────────────────
EXCLUDED_STATUSES = frozenset({
    STATUS_POSSIBLE_MAPPING_ERROR,
    STATUS_INVALID_MAPPING,
    STATUS_UNVERIFIED,
    STATUS_NONE,
    STATUS_UNKNOWN,
})


# ── Mapping confidence levels (from audit) ─────────────────────────
CONFIDENCE_CONFIRMED = "CONFIRMED"    # verified by statEntityID + marketName
CONFIDENCE_HIGH = "HIGH"              # mapped by statEntityID only
CONFIDENCE_NONE = "NONE"              # unrecognized entity


# ── Validation reasons (populated during validation) ───────────────
REASON_OK = "Participant mapping verified, sign consistent with consensus"
REASON_SWAP_SUSPECTED = "Sign pattern is exact inverse of consensus — possible swap"
REASON_BOTH_SAME_SIGN = "Both sides have same dominant sign — market broken"
REASON_NO_COMMON_BOOKS = "Fewer than 5 common books between sides — skipping analysis"
REASON_ID_UNKNOWN = "entityID not in participant map"
REASON_NO_OPPOSING_ODD = "No paired odd ID found for sign analysis"
REASON_NOT_ML = "Not a moneyline market — skipping sign analysis"
