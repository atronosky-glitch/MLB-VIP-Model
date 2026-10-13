"""Verified US weather-station registry.

Every entry was checked against the NWS station metadata API
(https://api.weather.gov/stations/<ID>) on 2026-10-12: name, coordinates and IANA time
zone are NWS's own values, not guesses. A market is only MODELED when the station its rules
name resolves to an entry here; anything else is listed as "not modeled: station not in
registry" rather than being forecast for a nearby airport.

Kalshi daily temperature markets name a National Weather Service climate (CLI) location such
as ``CLINYC``. For every US city here that is ``CLI`` + the 3-letter airport code, i.e. the
``K`` + code ASOS station, EXCEPT New York City, whose CLI location (CLINYC) is Central Park
(KNYC), not an airport. The mapping is a FORECAST POINT only: Kalshi settles on The Weather
Company's reported value, which is why the model is calibrated against Kalshi's own recorded
settlement values (see calibration.py) instead of assumed to equal the NWS station reading.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    lat: float
    lon: float
    tz: str


STATIONS: dict[str, Station] = {
    "KNYC": Station("KNYC", 'New York City, Central Park', 40.7833, -73.9667, "America/New_York"),
    "KLGA": Station("KLGA", 'New York, La Guardia Airport', 40.7792, -73.88, "America/New_York"),
    "KJFK": Station("KJFK", 'New York, Kennedy International Airport', 40.6392, -73.7639, "America/New_York"),
    "KEWR": Station("KEWR", 'Newark, Newark International Airport', 40.6825, -74.1694, "America/New_York"),
    "KTTN": Station("KTTN", 'Trenton-Mercer Airport', 40.2764, -74.8164, "America/New_York"),
    "KPHL": Station("KPHL", 'Philadelphia, Philadelphia International Airport', 39.8733, -75.2268, "America/New_York"),
    "KBOS": Station("KBOS", 'Boston, Logan International Airport', 42.3606, -71.0106, "America/New_York"),
    "KDCA": Station("KDCA", 'Washington/Reagan National Airport, DC', 38.8483, -77.0342, "America/New_York"),
    "KATL": Station("KATL", 'Atlanta, Hartsfield - Jackson Atlanta International Airport', 33.6403, -84.4269, "America/New_York"),
    "KMIA": Station("KMIA", 'Miami, Miami International Airport', 25.7906, -80.3164, "America/New_York"),
    "KMSY": Station("KMSY", 'New Orleans, New Orleans International Airport', 29.9928, -90.2508, "America/Chicago"),
    "KHOU": Station("KHOU", 'Houston, Houston Hobby Airport', 29.6375, -95.2825, "America/Chicago"),
    "KDFW": Station("KDFW", 'Dallas/Fort Worth International Airport', 32.8974, -97.022, "America/Chicago"),
    "KAUS": Station("KAUS", 'Austin-Bergstrom International Airport', 30.183, -97.6799, "America/Chicago"),
    "KSAT": Station("KSAT", 'San Antonio, San Antonio International Airport', 29.5328, -98.4636, "America/Chicago"),
    "KOKC": Station("KOKC", 'Oklahoma City, Will Rogers World Airport', 35.3886, -97.6003, "America/Chicago"),
    "KMDW": Station("KMDW", 'Chicago, Chicago Midway Airport', 41.7842, -87.7553, "America/Chicago"),
    "KORD": Station("KORD", "Chicago, Chicago-O'Hare International Airport", 41.9797, -87.9044, "America/Chicago"),
    "KMSP": Station("KMSP", 'Minneapolis, Minneapolis-St. Paul International Airport', 44.8831, -93.2289, "America/Chicago"),
    "KSDF": Station("KSDF", 'Louisville Muhammad Ali International Airport', 38.1741, -85.7365, "America/Kentucky/Louisville"),
    "KDEN": Station("KDEN", 'Denver, Denver International Airport', 39.8466, -104.6562, "America/Denver"),
    "KPHX": Station("KPHX", 'Phoenix, Phoenix Sky Harbor International Airport', 33.4278, -112.0035, "America/Phoenix"),
    "KLAS": Station("KLAS", 'Harry Reid International Airport', 36.0719, -115.1634, "America/Los_Angeles"),
    "KLAX": Station("KLAX", 'Los Angeles, Los Angeles International Airport', 33.9381, -118.3889, "America/Los_Angeles"),
    "KSAN": Station("KSAN", 'San Diego International Airport', 32.7336, -117.1831, "America/Los_Angeles"),
    "KSFO": Station("KSFO", 'San Francisco, San Francisco International Airport', 37.6196, -122.3656, "America/Los_Angeles"),
    "KSEA": Station("KSEA", 'Seattle, Seattle-Tacoma International Airport', 47.4447, -122.3136, "America/Los_Angeles"),
}

# Kalshi CLI location code -> station. CLI + 3 letters maps to K + those letters except where
# listed explicitly.
_CLI_OVERRIDES = {"CLINYC": "KNYC"}


def station_for_cli(code: str | None) -> Station | None:
    if not code:
        return None
    code = code.upper()
    icao = _CLI_OVERRIDES.get(code)
    if icao is None and code.startswith("CLI") and len(code) == 6:
        icao = "K" + code[3:]
    return STATIONS.get(icao) if icao else None


def station_by_icao(icao: str | None) -> Station | None:
    return STATIONS.get((icao or "").upper())
