"""Synthetic API events for deterministic tests.

These replace the mutable cache-dependent fixtures so tests never skip.
"""

TB_TOR_EVENT_ID = "cDV9yci5IGzMCCGu193A"
SF_KC_EVENT_ID = "BlzeFyQlUMHlth1H7vUh"

# ── TB @ TOR ──────────────────────────────────────────────────────

tb_tor_event = {
    "eventID": TB_TOR_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "Tampa Bay Rays"}, "teamID": "TBR"},
        "home": {"names": {"long": "Toronto Blue Jays"}, "teamID": "TOR"},
    },
    "odds": {
        # ── Away moneyline ──
        "points-away-game-ml-away": {
            "statEntityID": "away",
            "marketName": "Tampa Bay Rays Moneyline",
            "sideID": "away",
            "opposingOddID": "points-home-game-ml-home",
            "byBookmaker": {
                "betmgm":    {"odds": "-169"},
                "fanduel":   {"odds": "136"},
                "draftkings":{"odds": "140"},
                "williamhill":{"odds": "143"},
                "betrivers": {"odds": "130"},
                "pointsbet": {"odds": "135"},
            },
        },
        # ── Home moneyline ──
        "points-home-game-ml-home": {
            "statEntityID": "home",
            "marketName": "Toronto Blue Jays Moneyline",
            "sideID": "home",
            "opposingOddID": "points-away-game-ml-away",
            "byBookmaker": {
                "betmgm":    {"odds": "135"},
                "fanduel":   {"odds": "-156"},
                "draftkings":{"odds": "-160"},
                "williamhill":{"odds": "-163"},
                "betrivers": {"odds": "-150"},
                "pointsbet": {"odds": "-155"},
            },
        },
        # ── Team-total for participant-map verification ──
        "points-away-game-ou-over": {
            "statEntityID": "away",
            "marketName": "Tampa Bay Rays Over/Under",
            "byBookmaker": {
                "fanduel": {"odds": "-110", "overUnder": 3.5},
            },
        },
        # ── Team-total home for verification ──
        "points-home-game-ou-over": {
            "statEntityID": "home",
            "marketName": "Toronto Blue Jays Over/Under",
            "byBookmaker": {
                "fanduel": {"odds": "-115", "overUnder": 4.5},
            },
        },
    },
}

# ── SF @ KC ───────────────────────────────────────────────────────

sf_kc_event = {
    "eventID": SF_KC_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "San Francisco Giants"}, "teamID": "SFG"},
        "home": {"names": {"long": "Kansas City Royals"}, "teamID": "KCR"},
    },
    "odds": {
        # ── Away moneyline ──
        "points-away-game-ml-away": {
            "statEntityID": "away",
            "marketName": "San Francisco Giants Moneyline",
            "sideID": "away",
            "opposingOddID": "points-home-game-ml-home",
            "byBookmaker": {
                "betmgm":    {"odds": "120"},
                "fanduel":   {"odds": "118"},
                "draftkings":{"odds": "125"},
                "williamhill":{"odds": "122"},
                "betrivers": {"odds": "115"},
                "pointsbet": {"odds": "128"},
            },
        },
        # ── Home moneyline ──
        "points-home-game-ml-home": {
            "statEntityID": "home",
            "marketName": "Kansas City Royals Moneyline",
            "sideID": "home",
            "opposingOddID": "points-away-game-ml-away",
            "byBookmaker": {
                "betmgm":    {"odds": "-140"},
                "fanduel":   {"odds": "-138"},
                "draftkings":{"odds": "-145"},
                "williamhill":{"odds": "-142"},
                "betrivers": {"odds": "-135"},
                "pointsbet": {"odds": "-148"},
            },
        },
        # ── Team-total for verification ──
        "points-away-game-ou-over": {
            "statEntityID": "away",
            "marketName": "San Francisco Giants Over/Under",
            "byBookmaker": {
                "fanduel": {"odds": "-110", "overUnder": 3.5},
            },
        },
    },
}

# ── Flaherty event (player props) ─────────────────────────────────

flaherty_event = {
    "eventID": "EVENT_FLAHERTY_001",
    "teams": {
        "away": {"names": {"long": "Detroit Tigers"}, "teamID": "DET"},
        "home": {"names": {"long": "Chicago Cubs"}, "teamID": "CHC"},
    },
    "odds": {
        # ── Yes/No (pitcher records any strikeout) ──
        "pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-yn-yes": {
            "playerID": "JACK_FLAHERTY_1_MLB",
            "playerNames": {"full": "Jack Flaherty", "short": "J. Flaherty"},
            "marketName": "Jack Flaherty Any Strikeouts Yes/No",
            "byBookmaker": {
                "draftkings": {"odds": -575, "available": True},
                "espnbet":    {"odds": -600, "available": True},
                "fanduel":    {"odds": -550, "available": True},
                "betmgm":     {"odds": -525, "available": True},
                "pointsbet":  {"odds": -560, "available": True},
            },
        },
        "pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-yn-no": {
            "playerID": "JACK_FLAHERTY_1_MLB",
            "playerNames": {"full": "Jack Flaherty", "short": "J. Flaherty"},
            "marketName": "Jack Flaherty Any Strikeouts Yes/No",
            "byBookmaker": {},
        },
        # ── Over/Under ──
        "pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-ou-over": {
            "playerID": "JACK_FLAHERTY_1_MLB",
            "playerNames": {"full": "Jack Flaherty", "short": "J. Flaherty"},
            "marketName": "Jack Flaherty Strikeouts Over/Under",
            "byBookmaker": {
                "fanduel": {
                    "overUnder": 5.5,
                    "odds": -110,
                    "available": True,
                    "altLines": [
                        {"overUnder": 6.5, "odds": 130, "available": True},
                        {"overUnder": 7.5, "odds": 200, "available": True},
                    ],
                },
                "draftkings": {
                    "overUnder": 5.5,
                    "odds": -115,
                    "available": True,
                },
                "betmgm": {
                    "overUnder": 5.5,
                    "odds": -110,
                    "available": True,
                },
                "williamhill": {
                    "overUnder": 5.5,
                    "odds": -105,
                    "available": True,
                },
                "pointsbet": {
                    "overUnder": 5.5,
                    "odds": -112,
                    "available": True,
                },
            },
        },
        "pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-ou-under": {
            "playerID": "JACK_FLAHERTY_1_MLB",
            "playerNames": {"full": "Jack Flaherty", "short": "J. Flaherty"},
            "marketName": "Jack Flaherty Strikeouts Over/Under",
            "byBookmaker": {
                "fanduel": {
                    "overUnder": 5.5,
                    "odds": -110,
                    "available": True,
                    "altLines": [
                        {"overUnder": 6.5, "odds": -170, "available": True},
                        {"overUnder": 7.5, "odds": -280, "available": True},
                    ],
                },
                "draftkings": {
                    "overUnder": 5.5,
                    "odds": -105,
                    "available": True,
                },
                "betmgm": {
                    "overUnder": 5.5,
                    "odds": -110,
                    "available": True,
                },
                "williamhill": {
                    "overUnder": 5.5,
                    "odds": -115,
                    "available": True,
                },
                "pointsbet": {
                    "overUnder": 5.5,
                    "odds": -108,
                    "available": True,
                },
            },
        },
        "pitching_strikeouts-JAMESON_TAILLON_1_MLB-game-ou-over": {
            "playerID": "JAMESON_TAILLON_1_MLB",
            "playerNames": {"full": "Jameson Taillon", "short": "J. Taillon"},
            "marketName": "Jameson Taillon Strikeouts Over/Under",
            "byBookmaker": {
                "fanduel": {
                    "overUnder": 4.5,
                    "odds": -115,
                    "available": True,
                },
                "draftkings": {
                    "overUnder": 4.5,
                    "odds": -110,
                    "available": True,
                },
                "betmgm": {
                    "overUnder": 3.5,
                    "odds": -120,
                    "available": True,
                },
                "williamhill": {
                    "overUnder": 3.5,
                    "odds": -115,
                    "available": True,
                },
            },
        },
        "pitching_strikeouts-JAMESON_TAILLON_1_MLB-game-ou-under": {
            "playerID": "JAMESON_TAILLON_1_MLB",
            "playerNames": {"full": "Jameson Taillon", "short": "J. Taillon"},
            "marketName": "Jameson Taillon Strikeouts Over/Under",
            "byBookmaker": {
                "fanduel": {
                    "overUnder": 4.5,
                    "odds": -105,
                    "available": True,
                },
                "draftkings": {
                    "overUnder": 4.5,
                    "odds": -110,
                    "available": True,
                },
                "betmgm": {
                    "overUnder": 3.5,
                    "odds": 100,
                    "available": True,
                },
                "williamhill": {
                    "overUnder": 3.5,
                    "odds": -105,
                    "available": True,
                },
            },
        },
    },
}

# ── Outs event (pitcher outs recorded O/U) ─────────────────────────
# Used for Phase 2 outs integration tests.
# Pattern: pitching_outs-{PLAYER_ID}-game-ou-{side}
# No YN variant for outs.

OUTS_EVENT_ID = "EVENT_OUTS_001"

outs_event = {
    "eventID": OUTS_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "New York Yankees"}, "teamID": "NYY"},
        "home": {"names": {"long": "Boston Red Sox"}, "teamID": "BOS"},
    },
    "odds": {
        # ── Gerrit Cole main line (17.5 outs) — 6 books ──
        "pitching_outs-GERRIT_COLE_1_MLB-game-ou-over": {
            "playerID": "GERRIT_COLE_1_MLB",
            "playerNames": {"full": "Gerrit Cole", "short": "G. Cole"},
            "marketName": "Gerrit Cole Outs Recorded Over/Under",
            "byBookmaker": {
                "fanduel": {
                    "overUnder": 17.5, "odds": -115, "available": True,
                    "altLines": [
                        {"overUnder": 15.5, "odds": -200, "available": True},
                        {"overUnder": 19.5, "odds": 150, "available": True},
                    ],
                },
                "draftkings": {
                    "overUnder": 17.5, "odds": -110, "available": True,
                    "altLines": [
                        {"overUnder": 15.5, "odds": -195, "available": True},
                    ],
                },
                "betmgm": {
                    "overUnder": 17.5, "odds": -112, "available": True,
                    "altLines": [
                        {"overUnder": 15.5, "odds": -210, "available": True},
                    ],
                },
                "williamhill": {"overUnder": 17.5, "odds": -108, "available": True},
                "pointsbet":   {"overUnder": 17.5, "odds": -120, "available": True},
                "caesars":     {"overUnder": 17.5, "odds": -105, "available": True},
            },
        },
        "pitching_outs-GERRIT_COLE_1_MLB-game-ou-under": {
            "playerID": "GERRIT_COLE_1_MLB",
            "playerNames": {"full": "Gerrit Cole", "short": "G. Cole"},
            "marketName": "Gerrit Cole Outs Recorded Over/Under",
            "byBookmaker": {
                "fanduel": {
                    "overUnder": 17.5, "odds": -105, "available": True,
                    "altLines": [
                        {"overUnder": 15.5, "odds": 165, "available": True},
                        {"overUnder": 19.5, "odds": -180, "available": True},
                    ],
                },
                "draftkings": {
                    "overUnder": 17.5, "odds": -110, "available": True,
                    "altLines": [
                        {"overUnder": 15.5, "odds": 160, "available": True},
                    ],
                },
                "betmgm": {
                    "overUnder": 17.5, "odds": -108, "available": True,
                    "altLines": [
                        {"overUnder": 15.5, "odds": 170, "available": True},
                    ],
                },
                "williamhill": {"overUnder": 17.5, "odds": -112, "available": True},
                "pointsbet":   {"overUnder": 17.5, "odds": -100, "available": True},
                "caesars":     {"overUnder": 17.5, "odds": -115, "available": True},
            },
        },
        # ── Justin Verlander main line (16.5 outs) — 5 books ──
        "pitching_outs-JUSTIN_VERLANDER_1_MLB-game-ou-over": {
            "playerID": "JUSTIN_VERLANDER_1_MLB",
            "playerNames": {"full": "Justin Verlander", "short": "J. Verlander"},
            "marketName": "Justin Verlander Outs Recorded Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 16.5, "odds": -130, "available": True},
                "draftkings": {"overUnder": 16.5, "odds": -125, "available": True},
                "betmgm":     {"overUnder": 16.5, "odds": -128, "available": True},
                "williamhill":{"overUnder": 16.5, "odds": -135, "available": True},
                "caesars":    {"overUnder": 16.5, "odds": -120, "available": True},
            },
        },
        "pitching_outs-JUSTIN_VERLANDER_1_MLB-game-ou-under": {
            "playerID": "JUSTIN_VERLANDER_1_MLB",
            "playerNames": {"full": "Justin Verlander", "short": "J. Verlander"},
            "marketName": "Justin Verlander Outs Recorded Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 16.5, "odds": 110, "available": True},
                "draftkings": {"overUnder": 16.5, "odds": 105, "available": True},
                "betmgm":     {"overUnder": 16.5, "odds": 108, "available": True},
                "williamhill":{"overUnder": 16.5, "odds": 115, "available": True},
                "caesars":    {"overUnder": 16.5, "odds": 100, "available": True},
            },
        },
    },
}

# ── Hits allowed event ──────────────────────────────────────────────
# Pattern: pitching_hits-{PLAYER_ID}-game-ou-{side}

HITS_EVENT_ID = "EVENT_HITS_001"

hits_event = {
    "eventID": HITS_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "Atlanta Braves"}, "teamID": "ATL"},
        "home": {"names": {"long": "Philadelphia Phillies"}, "teamID": "PHI"},
    },
    "odds": {
        "pitching_hits-COLE_RAGANS_1_MLB-game-ou-over": {
            "playerID": "COLE_RAGANS_1_MLB",
            "playerNames": {"full": "Cole Ragans", "short": "C. Ragans"},
            "marketName": "Cole Ragans Hits Allowed Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 5.5, "odds": -115, "available": True},
                "draftkings": {"overUnder": 5.5, "odds": -110, "available": True},
                "betmgm":     {"overUnder": 5.5, "odds": -112, "available": True},
                "williamhill":{"overUnder": 5.5, "odds": -108, "available": True},
                "caesars":    {"overUnder": 5.5, "odds": -120, "available": True},
            },
        },
        "pitching_hits-COLE_RAGANS_1_MLB-game-ou-under": {
            "playerID": "COLE_RAGANS_1_MLB",
            "playerNames": {"full": "Cole Ragans", "short": "C. Ragans"},
            "marketName": "Cole Ragans Hits Allowed Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 5.5, "odds": -105, "available": True},
                "draftkings": {"overUnder": 5.5, "odds": -110, "available": True},
                "betmgm":     {"overUnder": 5.5, "odds": -108, "available": True},
                "williamhill":{"overUnder": 5.5, "odds": -112, "available": True},
                "caesars":    {"overUnder": 5.5, "odds": -100, "available": True},
            },
        },
        "pitching_hits-ZACK_WHEELER_1_MLB-game-ou-over": {
            "playerID": "ZACK_WHEELER_1_MLB",
            "playerNames": {"full": "Zack Wheeler", "short": "Z. Wheeler"},
            "marketName": "Zack Wheeler Hits Allowed Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 4.5, "odds": -130, "available": True},
                "draftkings": {"overUnder": 4.5, "odds": -125, "available": True},
                "betmgm":     {"overUnder": 4.5, "odds": -128, "available": True},
                "williamhill":{"overUnder": 4.5, "odds": -135, "available": True},
                "caesars":    {"overUnder": 4.5, "odds": -120, "available": True},
            },
        },
        "pitching_hits-ZACK_WHEELER_1_MLB-game-ou-under": {
            "playerID": "ZACK_WHEELER_1_MLB",
            "playerNames": {"full": "Zack Wheeler", "short": "Z. Wheeler"},
            "marketName": "Zack Wheeler Hits Allowed Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 4.5, "odds": 110, "available": True},
                "draftkings": {"overUnder": 4.5, "odds": 105, "available": True},
                "betmgm":     {"overUnder": 4.5, "odds": 108, "available": True},
                "williamhill":{"overUnder": 4.5, "odds": 115, "available": True},
                "caesars":    {"overUnder": 4.5, "odds": 100, "available": True},
            },
        },
    },
}

# ── Walks allowed event ─────────────────────────────────────────────
# Pattern: pitching_basesOnBalls-{PLAYER_ID}-game-ou-{side}
# Also has YN variant: pitching_basesOnBalls-{PLAYER_ID}-game-yn-{side}

WALKS_EVENT_ID = "EVENT_WALKS_001"

walks_event = {
    "eventID": WALKS_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "New York Mets"}, "teamID": "NYM"},
        "home": {"names": {"long": "Los Angeles Dodgers"}, "teamID": "LAD"},
    },
    "odds": {
        # ── O/U ──
        "pitching_basesOnBalls-SHOTA_IMANAGA_1_MLB-game-ou-over": {
            "playerID": "SHOTA_IMANAGA_1_MLB",
            "playerNames": {"full": "Shota Imanaga", "short": "S. Imanaga"},
            "marketName": "Shota Imanaga Walks Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 1.5, "odds": -160, "available": True},
                "draftkings": {"overUnder": 1.5, "odds": -155, "available": True},
                "betmgm":     {"overUnder": 1.5, "odds": -158, "available": True},
                "williamhill":{"overUnder": 1.5, "odds": -165, "available": True},
                "caesars":    {"overUnder": 1.5, "odds": -150, "available": True},
            },
        },
        "pitching_basesOnBalls-SHOTA_IMANAGA_1_MLB-game-ou-under": {
            "playerID": "SHOTA_IMANAGA_1_MLB",
            "playerNames": {"full": "Shota Imanaga", "short": "S. Imanaga"},
            "marketName": "Shota Imanaga Walks Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 1.5, "odds": 130, "available": True},
                "draftkings": {"overUnder": 1.5, "odds": 125, "available": True},
                "betmgm":     {"overUnder": 1.5, "odds": 128, "available": True},
                "williamhill":{"overUnder": 1.5, "odds": 135, "available": True},
                "caesars":    {"overUnder": 1.5, "odds": 120, "available": True},
            },
        },
        # ── YN (sparse — same pattern as strikeouts YN) ──
        "pitching_basesOnBalls-SHOTA_IMANAGA_1_MLB-game-yn-yes": {
            "playerID": "SHOTA_IMANAGA_1_MLB",
            "playerNames": {"full": "Shota Imanaga", "short": "S. Imanaga"},
            "marketName": "Shota Imanaga Any Walks Yes/No",
            "byBookmaker": {
                "draftkings": {"odds": -350, "available": True},
                "fanduel":    {"odds": -375, "available": True},
            },
        },
        "pitching_basesOnBalls-SHOTA_IMANAGA_1_MLB-game-yn-no": {
            "playerID": "SHOTA_IMANAGA_1_MLB",
            "playerNames": {"full": "Shota Imanaga", "short": "S. Imanaga"},
            "marketName": "Shota Imanaga Any Walks Yes/No",
            "byBookmaker": {},
        },
    },
}

# ── Earned runs event ───────────────────────────────────────────────
# Pattern: pitching_earnedRuns-{PLAYER_ID}-game-ou-{side}
# Also has YN variant: pitching_earnedRuns-{PLAYER_ID}-game-yn-{side}

ER_EVENT_ID = "EVENT_ER_001"

earned_runs_event = {
    "eventID": ER_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "Houston Astros"}, "teamID": "HOU"},
        "home": {"names": {"long": "Seattle Mariners"}, "teamID": "SEA"},
    },
    "odds": {
        # ── O/U ──
        "pitching_earnedRuns-FRAMBER_VALDEZ_1_MLB-game-ou-over": {
            "playerID": "FRAMBER_VALDEZ_1_MLB",
            "playerNames": {"full": "Framber Valdez", "short": "F. Valdez"},
            "marketName": "Framber Valdez Earned Runs Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 2.5, "odds": -120, "available": True},
                "draftkings": {"overUnder": 2.5, "odds": -115, "available": True},
                "betmgm":     {"overUnder": 2.5, "odds": -118, "available": True},
                "williamhill":{"overUnder": 2.5, "odds": -125, "available": True},
                "caesars":    {"overUnder": 2.5, "odds": -110, "available": True},
            },
        },
        "pitching_earnedRuns-FRAMBER_VALDEZ_1_MLB-game-ou-under": {
            "playerID": "FRAMBER_VALDEZ_1_MLB",
            "playerNames": {"full": "Framber Valdez", "short": "F. Valdez"},
            "marketName": "Framber Valdez Earned Runs Over/Under",
            "byBookmaker": {
                "fanduel":    {"overUnder": 2.5, "odds": 100, "available": True},
                "draftkings": {"overUnder": 2.5, "odds": -105, "available": True},
                "betmgm":     {"overUnder": 2.5, "odds": -102, "available": True},
                "williamhill":{"overUnder": 2.5, "odds": 105, "available": True},
                "caesars":    {"overUnder": 2.5, "odds": -100, "available": True},
            },
        },
        # ── YN ──
        "pitching_earnedRuns-FRAMBER_VALDEZ_1_MLB-game-yn-yes": {
            "playerID": "FRAMBER_VALDEZ_1_MLB",
            "playerNames": {"full": "Framber Valdez", "short": "F. Valdez"},
            "marketName": "Framber Valdez Any Earned Runs Yes/No",
            "byBookmaker": {
                "draftkings": {"odds": -250, "available": True},
                "fanduel":    {"odds": -275, "available": True},
            },
        },
        "pitching_earnedRuns-FRAMBER_VALDEZ_1_MLB-game-yn-no": {
            "playerID": "FRAMBER_VALDEZ_1_MLB",
            "playerNames": {"full": "Framber Valdez", "short": "F. Valdez"},
            "marketName": "Framber Valdez Any Earned Runs Yes/No",
            "byBookmaker": {},
        },
    },
}


# ── Batter markets event ────────────────────────────────────────────
# Covers Tier 1 (hits, total bases, H+R+RBI, home runs, RBI),
# Tier 2 (singles, doubles, walks, stolen bases, triples),
# and Tier 3 (batter strikeouts, first HR).

BATTER_EVENT_ID = "BATTER_TEST_001"

batter_event = {
    "eventID": BATTER_EVENT_ID,
    "teams": {
        "away": {"names": {"long": "New York Yankees"}, "teamID": "NYY"},
        "home": {"names": {"long": "Boston Red Sox"}, "teamID": "BOS"},
    },
    "odds": {
        # ── Tier 1: Batter Hits O/U ──
        "batting_hits-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Hits Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_hits-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "+120", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+130", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "+115", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "+125", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betrivers":  {"odds": "+110", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_hits-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Hits Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_hits-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-150", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-160", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-145", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-155", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betrivers":  {"odds": "-140", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 1: Batter Hits YN ──
        "batting_hits-AARON_JUDGE_1_MLB-game-yn-yes": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Any Hits Yes/No",
            "betTypeID": "yn",
            "sideID": "yes",
            "opposingOddID": "batting_hits-AARON_JUDGE_1_MLB-game-yn-no",
            "byBookmaker": {
                "draftkings": {"odds": "-220", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-200", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-210", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-230", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_hits-AARON_JUDGE_1_MLB-game-yn-no": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Any Hits Yes/No",
            "betTypeID": "yn",
            "sideID": "no",
            "opposingOddID": "batting_hits-AARON_JUDGE_1_MLB-game-yn-yes",
            "byBookmaker": {},
        },
        # ── Tier 1: Home Runs O/U ──
        "batting_homeRuns-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Home Runs Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_homeRuns-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "+350", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+380", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "+340", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "+360", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_homeRuns-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Home Runs Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_homeRuns-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-500", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-550", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-480", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-520", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 1: Total Bases O/U ──
        "batting_totalBases-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Total Bases Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_totalBases-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "-110", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-105", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-115", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-108", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betrivers":  {"odds": "-112", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_totalBases-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Total Bases Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_totalBases-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-110", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-115", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-105", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-112", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betrivers":  {"odds": "-108", "overUnder": "1.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 1: Hits + Runs + RBI O/U ──
        "batting_hits+runs+rbi-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Hits + Runs + RBIs Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_hits+runs+rbi-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "-115", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-110", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-120", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-108", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_hits+runs+rbi-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Hits + Runs + RBIs Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_hits+runs+rbi-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-105", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-110", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-100", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-112", "overUnder": "2.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 1: RBI O/U ──
        "batting_RBI-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Runs Batted In Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_RBI-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "+140", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+150", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "+135", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "+145", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_RBI-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Runs Batted In Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_RBI-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-170", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-180", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-165", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-175", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 2: Singles O/U ──
        "batting_singles-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Singles Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_singles-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "+105", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+110", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "+100", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "+108", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_singles-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Singles Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_singles-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-135", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-140", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-130", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-138", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 2: Doubles O/U ──
        "batting_doubles-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Doubles Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_doubles-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "+300", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+320", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "+290", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "+310", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_doubles-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Doubles Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_doubles-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-450", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-480", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-430", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "caesars":    {"odds": "-460", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 2: Walks O/U ──
        "batting_basesOnBalls-AARON_JUDGE_1_MLB-game-ou-over": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Walks Over/Under",
            "betTypeID": "ou",
            "sideID": "over",
            "opposingOddID": "batting_basesOnBalls-AARON_JUDGE_1_MLB-game-ou-under",
            "byBookmaker": {
                "draftkings": {"odds": "+130", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+140", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "+125", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_basesOnBalls-AARON_JUDGE_1_MLB-game-ou-under": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge Walks Over/Under",
            "betTypeID": "ou",
            "sideID": "under",
            "opposingOddID": "batting_basesOnBalls-AARON_JUDGE_1_MLB-game-ou-over",
            "byBookmaker": {
                "draftkings": {"odds": "-160", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "-170", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "betmgm":     {"odds": "-155", "overUnder": "0.5", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        # ── Tier 3: First Home Run YN ──
        "batting_firstHomeRun-AARON_JUDGE_1_MLB-game-yn-yes": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge To Record First Home Run Yes/No",
            "betTypeID": "yn",
            "sideID": "yes",
            "opposingOddID": "batting_firstHomeRun-AARON_JUDGE_1_MLB-game-yn-no",
            "byBookmaker": {
                "draftkings": {"odds": "+800", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
                "fanduel":    {"odds": "+850", "available": True, "lastUpdatedAt": "2026-07-23T20:00:00Z"},
            },
        },
        "batting_firstHomeRun-AARON_JUDGE_1_MLB-game-yn-no": {
            "playerID": "AARON_JUDGE_1_MLB",
            "playerNames": {"full": "Aaron Judge", "short": "A. Judge"},
            "marketName": "Aaron Judge To Record First Home Run Yes/No",
            "betTypeID": "yn",
            "sideID": "no",
            "opposingOddID": "batting_firstHomeRun-AARON_JUDGE_1_MLB-game-yn-yes",
            "byBookmaker": {},
        },
    },
}

# ── Convenience: list of all synthetic events ─────────────────────

all_synthetic_events = [tb_tor_event, sf_kc_event, flaherty_event, batter_event]
