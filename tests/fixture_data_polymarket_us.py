"""Sanitized REAL responses captured 2026-09-14 from Polymarket US's
public, unauthenticated market-data API (https://gateway.polymarket.us)
-- no credentials were used or needed to fetch these; get_markets/
get_market/get_orderbook/get_best_bid_ask are all public GET endpoints
per Stage 1's own design. Trimmed to the fields this codebase actually
reads (irrelevant metadata like team logos/colors removed), but every
value kept is exactly what the live API returned -- these are the
fixtures that finally confirmed Polymarket US's NO-side book structure
(see src/execution/polymarket_us.py::normalize_orderbook's docstring).

Two live, currently-open, real two-outcome markets:
- paccc-usho-midterms-2026-11-03-dem ("U.S House Midterm Winner")
- paccc-usse-midterms-2026-11-03-rep ("U.S Senate Midterm Winner")

Both confirmed the identical mechanism: the two marketSides (Yes/No)
share ONE identifier (the market slug), and
stats.lastPriceSample.{longPx,shortPx} sum to exactly 1.000.
"""

REAL_MARKET_HOUSE_DEM = {
    "id": "22648",
    "question": "U.S House Midterm Winner",
    "slug": "paccc-usho-midterms-2026-11-03-dem",
    "endDate": "2026-11-04T00:00:00Z",
    "category": "politics",
    "startDate": "2025-01-01T00:00:00Z",
    "active": True,
    "marketType": "election",
    "closed": False,
    "status": "MARKET_STATUS_OPEN",
    "marketSides": [
        {
            "id": "22649",
            "marketSideType": "MARKET_SIDE_TYPE_INSTRUMENT",
            "identifier": "paccc-usho-midterms-2026-11-03-dem",
            "description": "Yes",
            "price": "0.8540",
            "marketId": 22648,
            "long": True,
            "tradable": True,
        },
        {
            "id": "22650",
            "marketSideType": "MARKET_SIDE_TYPE_INSTRUMENT",
            "identifier": "paccc-usho-midterms-2026-11-03-dem",
            "description": "No",
            "price": "0.147",
            "marketId": 22648,
            "long": False,
            "tradable": True,
        },
    ],
    "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.8540", "0.147"],
}

REAL_ORDERBOOK_HOUSE_DEM = {
    "marketData": {
        "marketSlug": "paccc-usho-midterms-2026-11-03-dem",
        "bids": [
            {"px": {"value": "0.8530", "currency": "USD"}, "qty": "3625.0000"},
            {"px": {"value": "0.8520", "currency": "USD"}, "qty": "2747.0000"},
            {"px": {"value": "0.8500", "currency": "USD"}, "qty": "5000.0000"},
            {"px": {"value": "0.8490", "currency": "USD"}, "qty": "5276.0000"},
            {"px": {"value": "0.8480", "currency": "USD"}, "qty": "14.0000"},
        ],
        "offers": [
            {"px": {"value": "0.8540", "currency": "USD"}, "qty": "7203.0000"},
            {"px": {"value": "0.8570", "currency": "USD"}, "qty": "28.0000"},
            {"px": {"value": "0.8580", "currency": "USD"}, "qty": "45.0000"},
            {"px": {"value": "0.8590", "currency": "USD"}, "qty": "66.0000"},
            {"px": {"value": "0.8600", "currency": "USD"}, "qty": "3169.0000"},
        ],
        "state": "MARKET_STATE_OPEN",
        "stats": {
            "lastTradePx": {"value": "0.8530", "currency": "USD"},
            "currentPx": {"value": "0.8530", "currency": "USD"},
            "lastPriceSample": {
                "longPx": {"value": "0.8530", "currency": "USD"},
                "shortPx": {"value": "0.147", "currency": "USD"},
                "ts": "2026-09-15T00:39:36.337717848Z",
            },
        },
    },
}

REAL_MARKET_SENATE_REP = {
    "id": "22651",
    "question": "U.S Senate Midterm Winner",
    "slug": "paccc-usse-midterms-2026-11-03-rep",
    "endDate": "2026-11-04T00:00:00Z",
    "category": "politics",
    "active": True,
    "marketType": "election",
    "closed": False,
    "status": "MARKET_STATUS_OPEN",
    "marketSides": [
        {
            "id": "22652",
            "identifier": "paccc-usse-midterms-2026-11-03-rep",
            "description": "No",
            "price": "0.517",
            "long": False,
            "tradable": True,
        },
        {
            "id": "22653",
            "identifier": "paccc-usse-midterms-2026-11-03-rep",
            "description": "Yes",
            "price": "0.4830",
            "long": True,
            "tradable": True,
        },
    ],
    "outcomes": ["No", "Yes"],
    "outcomePrices": ["0.517", "0.4830"],
}

REAL_ORDERBOOK_SENATE_REP = {
    "marketData": {
        "marketSlug": "paccc-usse-midterms-2026-11-03-rep",
        "bids": [
            {"px": {"value": "0.4820", "currency": "USD"}, "qty": "1000.0000"},
        ],
        "offers": [
            {"px": {"value": "0.4830", "currency": "USD"}, "qty": "500.0000"},
        ],
        "state": "MARKET_STATE_OPEN",
        "stats": {
            "lastPriceSample": {
                "longPx": {"value": "0.4830", "currency": "USD"},
                "shortPx": {"value": "0.517", "currency": "USD"},
                "ts": "2026-09-15T00:00:00.000000000Z",
            },
        },
    },
}
