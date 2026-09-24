"""Exact team identity tables for prediction-market contract mapping.

Kalshi identifies a team in every game-level ticker by a short code (the
last ticker segment of a moneyline market, e.g. ``KXNFLGAME-26OCT05ATLNO-NO``)
and by a human label (``yes_sub_title``: "New Orleans", "NO Saints",
"Chicago WS"). Labels are NOT unique across a league's teams in general
("Chicago", "Los Angeles", "New York") and are inconsistent between
series ("ATL Falcons" vs "Atlanta"), so fuzzy name matching is unsafe.
This module is the exact bridge: every code -> one canonical full team
name, plus the set of labels Kalshi was observed to use for that code.

Built from real, live, unauthenticated Kalshi reads (2026-09-23) of
KX{MLB,NFL,WNBA}{GAME,SPREAD,TOTAL}; the observed (code, label) pairs are
pinned by tests/test_kalshi_mapping.py against a saved real payload
fixture. A team name that is not in this table resolves to None and is
treated as UNSUPPORTED -- never guessed. NCAAF is deliberately absent
(130+ programs, no verified table).

Pure data + tiny helpers: no I/O, no network.
"""

from __future__ import annotations

import unicodedata


def normalize_team_key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().replace(".", "").split())


# code -> (canonical full name, Kalshi labels observed for that code,
#          extra recommendation-side aliases)
_MLB = {
    "ATH": ("Athletics", ("A's",), ("Oakland Athletics", "Sacramento Athletics", "Kansas City Athletics", "Las Vegas Athletics")),
    "ATL": ("Atlanta Braves", ("Atlanta",), ()),
    "AZ": ("Arizona Diamondbacks", ("Arizona",), ("Arizona D-backs", "Arizona Dbacks")),
    "BAL": ("Baltimore Orioles", ("Baltimore",), ()),
    "BOS": ("Boston Red Sox", ("Boston",), ()),
    "CHC": ("Chicago Cubs", ("Chicago C",), ()),
    "CIN": ("Cincinnati Reds", ("Cincinnati",), ()),
    "CLE": ("Cleveland Guardians", ("Cleveland",), ()),
    "COL": ("Colorado Rockies", ("Colorado",), ()),
    "CWS": ("Chicago White Sox", ("Chicago WS",), ()),
    "DET": ("Detroit Tigers", ("Detroit",), ()),
    "HOU": ("Houston Astros", ("Houston",), ()),
    "KC": ("Kansas City Royals", ("Kansas City",), ()),
    "LAA": ("Los Angeles Angels", ("Los Angeles A",), ("LA Angels", "Anaheim Angels")),
    "LAD": ("Los Angeles Dodgers", ("Los Angeles D",), ("LA Dodgers",)),
    "MIA": ("Miami Marlins", ("Miami",), ()),
    "MIL": ("Milwaukee Brewers", ("Milwaukee",), ()),
    "MIN": ("Minnesota Twins", ("Minnesota",), ()),
    "NYM": ("New York Mets", ("New York M",), ()),
    "NYY": ("New York Yankees", ("New York Y",), ()),
    "PHI": ("Philadelphia Phillies", ("Philadelphia",), ()),
    "PIT": ("Pittsburgh Pirates", ("Pittsburgh",), ()),
    "SD": ("San Diego Padres", ("San Diego",), ()),
    "SEA": ("Seattle Mariners", ("Seattle",), ()),
    "SF": ("San Francisco Giants", ("San Francisco",), ()),
    "STL": ("St. Louis Cardinals", ("St. Louis",), ("Saint Louis Cardinals",)),
    "TB": ("Tampa Bay Rays", ("Tampa Bay",), ()),
    "TEX": ("Texas Rangers", ("Texas",), ()),
    "TOR": ("Toronto Blue Jays", ("Toronto",), ()),
    "WSH": ("Washington Nationals", ("Washington",), ()),
}

_NFL = {
    "ARI": ("Arizona Cardinals", ("ARI Cardinals", "Arizona"), ()),
    "ATL": ("Atlanta Falcons", ("ATL Falcons", "Atlanta"), ()),
    "BAL": ("Baltimore Ravens", ("BAL Ravens", "Baltimore"), ()),
    "BUF": ("Buffalo Bills", ("BUF Bills", "Buffalo"), ()),
    "CAR": ("Carolina Panthers", ("CAR Panthers", "Carolina"), ()),
    "CHI": ("Chicago Bears", ("CHI Bears", "Chicago"), ()),
    "CIN": ("Cincinnati Bengals", ("CIN Bengals", "Cincinnati"), ()),
    "CLE": ("Cleveland Browns", ("CLE Browns", "Cleveland"), ()),
    "DAL": ("Dallas Cowboys", ("DAL Cowboys", "Dallas"), ()),
    "DEN": ("Denver Broncos", ("DEN Broncos", "Denver"), ()),
    "DET": ("Detroit Lions", ("DET Lions", "Detroit"), ()),
    "GB": ("Green Bay Packers", ("GB Packers", "Green Bay"), ()),
    "HOU": ("Houston Texans", ("HOU Texans", "Houston"), ()),
    "IND": ("Indianapolis Colts", ("IND Colts", "Indianapolis"), ()),
    "JAC": ("Jacksonville Jaguars", ("JAC Jaguars", "Jacksonville"), ()),
    "KC": ("Kansas City Chiefs", ("KC Chiefs", "Kansas City"), ()),
    "LAC": ("Los Angeles Chargers", ("LA Chargers", "Los Angeles C"), ("LA Chargers",)),
    "LAR": ("Los Angeles Rams", ("LA Rams", "Los Angeles R"), ("LA Rams",)),
    "LV": ("Las Vegas Raiders", ("LV Raiders", "Las Vegas"), ()),
    "MIA": ("Miami Dolphins", ("MIA Dolphins", "Miami"), ()),
    "MIN": ("Minnesota Vikings", ("MIN Vikings", "Minnesota"), ()),
    "NE": ("New England Patriots", ("NE Patriots", "New England"), ()),
    "NO": ("New Orleans Saints", ("NO Saints", "New Orleans"), ()),
    "NYG": ("New York Giants", ("NY Giants", "New York G"), ()),
    "NYJ": ("New York Jets", ("NY Jets", "New York J"), ()),
    "PHI": ("Philadelphia Eagles", ("PHI Eagles", "Philadelphia"), ()),
    "PIT": ("Pittsburgh Steelers", ("PIT Steelers", "Pittsburgh"), ()),
    "SEA": ("Seattle Seahawks", ("SEA Seahawks", "Seattle"), ()),
    "SF": ("San Francisco 49ers", ("SF 49ers", "San Francisco"), ("San Francisco Forty Niners",)),
    "TB": ("Tampa Bay Buccaneers", ("TB Buccaneers", "Tampa Bay"), ()),
    "TEN": ("Tennessee Titans", ("TEN Titans", "Tennessee"), ()),
    "WAS": ("Washington Commanders", ("WAS Commanders", "Washington"), ()),
}

# Kalshi's WNBA feed also lists all-star exhibition sides ("Team Coop",
# "Team Spoon") -- deliberately absent, so they resolve to None.
_WNBA = {
    "ATL": ("Atlanta Dream", ("Atlanta",), ()),
    "CHI": ("Chicago Sky", ("Chicago",), ()),
    "CONN": ("Connecticut Sun", ("Connecticut",), ()),
    "DAL": ("Dallas Wings", ("Dallas",), ()),
    "GS": ("Golden State Valkyries", ("Golden State",), ()),
    "IND": ("Indiana Fever", ("Indiana",), ()),
    "LA": ("Los Angeles Sparks", ("Los Angeles",), ("LA Sparks",)),
    "LV": ("Las Vegas Aces", ("Las Vegas",), ()),
    "MIN": ("Minnesota Lynx", ("Minnesota",), ()),
    "NY": ("New York Liberty", ("New York",), ()),
    "PDX": ("Portland Fire", ("Portland",), ()),
    "PHX": ("Phoenix Mercury", ("Phoenix",), ()),
    "SEA": ("Seattle Storm", ("Seattle",), ()),
    "TOR": ("Toronto Tempo", ("Toronto",), ()),
    "WSH": ("Washington Mystics", ("Washington",), ()),
}

TEAM_TABLES: dict[str, dict[str, tuple]] = {"MLB": _MLB, "NFL": _NFL, "WNBA": _WNBA}

_NAME_TO_CODE: dict[str, dict[str, str]] = {}
for _league, _table in TEAM_TABLES.items():
    _index: dict[str, str] = {}
    for _code, (_canonical, _labels, _aliases) in _table.items():
        for _name in (_canonical, *_aliases):
            _key = normalize_team_key(_name)
            # A name mapping to two different codes is ambiguous: drop it
            # entirely (fail closed) rather than pick one.
            if _index.get(_key, _code) != _code:
                _index[_key] = ""
            else:
                _index[_key] = _code
    _NAME_TO_CODE[_league] = {k: v for k, v in _index.items() if v}


def supported_leagues() -> frozenset[str]:
    return frozenset(TEAM_TABLES)


def team_code_for_name(league: str, name: str | None) -> str | None:
    """Kalshi team code for a recommendation-side team name, or None."""
    if not name:
        return None
    return _NAME_TO_CODE.get((league or "").upper(), {}).get(normalize_team_key(name))


def canonical_team_name(league: str, name: str | None) -> str | None:
    code = team_code_for_name(league, name)
    return code_to_canonical_name(league, code) if code else None


def code_to_canonical_name(league: str, code: str | None) -> str | None:
    entry = TEAM_TABLES.get((league or "").upper(), {}).get(code or "")
    return entry[0] if entry else None


def label_matches_code(league: str, code: str, label: str | None) -> bool:
    """True if *label* is one of the labels Kalshi was observed to use
    for *code* (exact, case-sensitive)."""
    entry = TEAM_TABLES.get((league or "").upper(), {}).get(code)
    return bool(entry and label in entry[1])
