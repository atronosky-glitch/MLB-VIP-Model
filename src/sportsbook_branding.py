"""Display names and badge styling for sportsbooks.

Real sportsbook logos are trademarked assets this project has no rights
to embed, so opportunities are shown with a small colored initials badge
instead — a stand-in "logo" that's still visually distinct per book at a
glance without claiming to be the brand's actual mark.
"""

from __future__ import annotations

# slug (as stored in player_prop_odds.sportsbook / the odds API's bookmaker
# key) -> (display name, initials, accent color). Colors are a curated
# palette chosen to read well on a dark background -- not an attempt to
# reproduce any brand's actual color identity.
_BRAND_TABLE: dict[str, tuple[str, str, str]] = {
    "draftkings":  ("DraftKings",   "DK",  "#e8b923"),
    "fanduel":     ("FanDuel",      "FD",  "#48d8ff"),
    "betmgm":      ("BetMGM",       "MGM", "#c084fc"),
    "caesars":     ("Caesars",      "CZ",  "#d9c08a"),
    "williamhill": ("Caesars",      "CZ",  "#d9c08a"),
    "williamhill_us": ("Caesars",   "CZ",  "#d9c08a"),
    "betrivers":   ("BetRivers",    "BR",  "#2dd4bf"),
    "pointsbet":   ("PointsBet",    "PB",  "#ff8a65"),
    "espnbet":     ("ESPN BET",     "ES",  "#7c9eff"),
    "bet365":      ("Bet365",       "365", "#f0a83c"),
    "fanatics":    ("Fanatics",     "FN",  "#e8637a"),
    "bovada":      ("Bovada",       "BV",  "#e07856"),
    "betonlineag": ("BetOnline",    "BOL", "#6ea8fe"),
    "betonline":   ("BetOnline",    "BOL", "#6ea8fe"),
    "mybookieag":  ("MyBookie",     "MB",  "#c084fc"),
    "mybookie":    ("MyBookie",     "MB",  "#c084fc"),
    "lowvig":      ("LowVig",       "LV",  "#8e9aae"),
    "betus":       ("BetUS",        "BU",  "#48d8ff"),
    "wynnbet":     ("WynnBET",      "WY",  "#d9c08a"),
    "unibet":      ("Unibet",       "UB",  "#e8637a"),
    "hardrockbet": ("Hard Rock Bet","HR",  "#e07856"),
    "circasports": ("Circa Sports", "CS",  "#2dd4bf"),
    "superbook":   ("SuperBook",    "SB",  "#7c9eff"),
    "pinnacle":    ("Pinnacle",     "PIN", "#f0a83c"),
    "fliff":       ("Fliff",        "FL",  "#c084fc"),
    "novig":       ("Novig",        "NV",  "#8e9aae"),
    "prizepicks":  ("PrizePicks",   "PP",  "#48d8ff"),
    "underdog":    ("Underdog",     "UD",  "#e8637a"),
    "betparx":     ("BetPARX",      "PX",  "#2dd4bf"),
}

# Fallback palette for any book not in the table above, so an unrecognized
# slug still gets a distinct-looking badge instead of a flat gray one.
_FALLBACK_PALETTE = [
    "#e8b923", "#48d8ff", "#c084fc", "#2dd4bf", "#ff8a65",
    "#7c9eff", "#e8637a", "#d9c08a", "#f0a83c", "#e07856",
]


def _normalize(slug: str | None) -> str:
    return (slug or "").strip().lower()


def sportsbook_display(slug: str | None) -> str:
    """Friendly display name for a raw sportsbook slug/key."""
    key = _normalize(slug)
    if not key:
        return "Unknown"
    entry = _BRAND_TABLE.get(key)
    if entry:
        return entry[0]
    return key.replace("_", " ").replace("-", " ").title()


def _initials(slug: str | None, display_name: str) -> str:
    entry = _BRAND_TABLE.get(_normalize(slug))
    if entry:
        return entry[1]
    words = [w for w in display_name.replace("_", " ").replace("-", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return display_name[:2].upper() if display_name else "??"


def _accent_color(slug: str | None) -> str:
    key = _normalize(slug)
    entry = _BRAND_TABLE.get(key)
    if entry:
        return entry[2]
    # A stable (not Python's randomized hash()) index so the same
    # unrecognized book always gets the same color, in every process.
    index = sum(ord(c) for c in key) % len(_FALLBACK_PALETTE) if key else 0
    return _FALLBACK_PALETTE[index]


def sportsbook_badge_html(slug: str | None, size: int = 26) -> str:
    """Small colored circular 'logo' stand-in: a monogram badge."""
    name = sportsbook_display(slug)
    initials = _initials(slug, name)
    color = _accent_color(slug)
    font_size = max(9, int(size * 0.34))
    return (
        f'<span title="{name}" style="display:inline-flex;align-items:center;'
        f'justify-content:center;width:{size}px;height:{size}px;border-radius:50%;'
        f'background:linear-gradient(145deg,{color},{color}cc);color:#0d0b07;'
        f'font-weight:800;font-size:{font_size}px;letter-spacing:-.02em;'
        f'box-shadow:0 2px 6px rgba(0,0,0,.35);flex:none;line-height:1;">'
        f'{initials}</span>'
    )
