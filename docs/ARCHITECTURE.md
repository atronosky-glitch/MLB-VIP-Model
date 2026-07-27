# docs/ARCHITECTURE.md — System architecture

## Components

| Module | Responsibility |
|---|---|
| `src/api_client.py` | HTTP client for SportsGameOdds v2. Caches every response as JSON under `data/_api_cache/`. Key: `SportsGameOddsClient` class with `get_events()`. |
| `src/odds_parser.py` | Parses full-game MLB odds (moneyline, spread, total) from an event dict. Builds participant map (`_build_participant_map`), collects main + alt lines, runs consensus-based sign analysis (`_validate_mappings`) to flag possible swap errors. Returns `ParsedOddsResult` with separate `odds_rows` and `audit_rows`. |
| `src/player_prop_parser.py` | Parses pitcher strikeout Over/Under markets. Matches by odd ID pattern `pitching_strikeouts-{PLAYER_ID}-game-ou-{side}`. Extracts `player_id` from odd_data `playerID`, `player_name` from `marketName`. Validates all fields. Uses `_build_group_key` for pairing Over/Under at same line. Separate entries for alt lines (`is_alt_line=1`). Returns `ParsedPlayerPropResult`. |
| `src/validation_constants.py` | Single source of truth for validation status strings, approved/excluded sets, confidence levels, and reason strings. NEVER duplicate elsewhere. |
| `src/prop_config.py` | Centralised thresholds for player prop analysis and scanner: market quality names, `MIN_COMPARISON_BOOKS=4`, `OUTLIER_EV_THRESHOLD=0.10`, `STRONG_EDGE_THRESHOLD=0.05`, `POSITIVE_EDGE_THRESHOLD=0.02`, `ACTIONABLE_EDGE_THRESHOLD=0.02`, `FRESHNESS_THRESHOLD_SECONDS=3600`. All analysis modules import the config as a module (`cfg`) and access values at runtime. |
| `src/market_analysis.py` | Conversion utilities (`american_to_decimal`, `american_to_probability`), price comparison (`better_price`, `best_price`), two-outcome vig removal (`remove_vig`), consensus (`consensus_price`), EV (`expected_value`), side analysis (`analyze_side`), two-way analysis (`analyze_two_way_market`), CLV (`compute_clv`), slow-book detection (`find_slow_books`). |
| `src/player_prop_analysis.py` | LOO-consensus EV for player prop groups. Dual-status: `_classify_market` (EXCLUDED / INSUFFICIENT / VALID / NEEDS_REVIEW), `_classify_bet` (STRONG / POSITIVE / MARGINAL / NO_EDGE). Entry point: `analyze_prop_group()`. Imports config as `cfg` module, not by name. |
| `src/player_prop_scanner.py` | Generic player-prop edge scanner. Registry-driven: `resolve_markets()` validates market/form combos, `run_scan()` fetches, parses, filters (market type, sportsbook, player, game), analyzes, ranks, deduplicates. `display_results()` / `display_verbose()` use `MarketConfig.scanner_title`. CLI: `python -m src.player_prop_scanner --market <name> [--market-form ou\|yn\|all] [--all\|--positive-only\|--actionable-only] [--min-ev PCT] [--limit N] [--sportsbook X] [--player X] [--game X]`. |
| `src/strikeout_scanner.py` | Backward-compatible wrapper around `player_prop_scanner`. Forces `market="strikeouts"`, maps old `--market ou\|yn\|all` to `market_form`. No analysis logic — pure delegation. CLI: `python -m src.strikeout_scanner [--all] [--positive-only] [--actionable-only] [--min-ev PCT] [--limit N] [--market ou\|yn\|all]`. |
| `database/db_manager.py` | All SQLite operations. `init_db()` creates all tables. `save_odds_batch()` / `save_player_prop_batch()` for bulk inserts with audit. Safe migrations via `_safe_migrate_odds()` / `_safe_migrate_player_prop()`. |
| `main.py` | CLI entry point. Fetches events, parses all odds (standard + player props), stores in DB, prints market analysis to terminal. |
| `tests/` | 10 test files, 411 tests. Uses isolated in-memory DB (`conftest.py` fixture). Never touches `mlb_model.db`. |

## Data flow

```
API response (JSON)
  └─ src/api_client.py (fetch, cache)
     └─ src/odds_parser.py OR src/player_prop_parser.py
        ├─ Identify raw market (odd ID pattern)
        ├─ Map to stable entity (entityID/playerID)
        ├─ Validate (check fields, sign consensus if applicable)
        ├─ Separate approved vs excluded rows
        └─ Return ParsedOddsResult / ParsedPlayerPropResult
           └─ database/db_manager.py
              ├─ odds/player_prop_odds table (approved rows)
              └─ odds_mapping_audit/player_prop_mapping_audit table (all rows)
                 └─ src/market_analysis.py OR src/player_prop_analysis.py
                    ├─ Query approved-status rows (SQL WHERE validation_status IN ...)
                    ├─ Defense-in-depth filter (_filter_approved)
                    ├─ Consensus (implied-probability-space average)
                    ├─ No-vig fair probability (paired side removal)
    ├─ LOO fair probability (player props only)
                     ├─ EV per book: fair_prob * decimal_odds - 1
                     ├─ Market quality classification
                     ├─ Bet status classification
                      └─ Display to terminal (main.py OR player_prop_scanner.py)
                                └─ Scanner filtering by mode (all/positive/actionable)
                                └─ Registry-driven presentation (scanner_title from MarketConfig)
                                └─ Case-insensitive sportsbook/player/game filters
                                └─ Ranking (EV desc, MQ, n_books, time, pitcher, book)
                                └─ Deduplication (event+player+line+side+book)
                                └─ Staleness check (age > FRESHNESS_THRESHOLD_SECONDS)
```

## Validation model

- **Approved statuses** (`APPROVED_STATUSES = {VALID, CONFIRMED, VERIFIED}`) — rows that may enter analysis
- **Excluded statuses**: POSSIBLE_MAPPING_ERROR, INVALID_MAPPING, UNVERIFIED, NONE, UNKNOWN — stored for audit, blocked from analysis
- **Audit retention**: every (odd_id, sportsbook) pair has a record in audit tables, including provenance data
- **Defense-in-depth**: SQL filtering (`WHERE validation_status IN ...`) is the primary gate; `_filter_approved()` in `market_analysis.py` is a second layer in analysis functions
- **No automatic participant swapping**: flagged records are never corrected — they are simply excluded

## Market grouping (pitcher strikeouts)

Over and Under at the same exact line are paired for analysis. The `market_group_key` is:

```
{event_id}|{player_id}|pitching_strikeouts_ou|game|{line}[_alt]
```

Key strips the side so that Over and Under at the same line share one key. Alt lines get `_alt` suffix, keeping them separate from main lines.

## EV model

1. American odds → decimal odds (`american_to_decimal`)
2. Decimal odds → implied probability (`american_to_probability` = 1 / decimal)
3. No-vig probability = imp_prob / (imp_prob_over + imp_prob_under)
4. LOO consensus (player props): fair probability from all books except the one being evaluated
5. EV = fair_probability * decimal_odds - 1
6. A market can be `VALID_MARKET` (enough paired books) while every bet is `NO_EDGE` (negative EV)

## Database tables

Inspected from `database/db_manager.py`:

- **games** — event_id (PK), league, away_team, home_team, start_time, status, sport_id, league_id, created_at, updated_at
- **odds** — id (PK), event_id (FK→games), sportsbook, market, selection, price, points, is_alt_line, available, pulled_at, odd_id, validation_status, mapping_confidence, mapping_method, validation_reason
- **raw_responses** — id (PK), endpoint, params, pulled_at, response_json
- **data_pulls** — id (PK), event_id, pull_type, pulled_at
- **bet_results** — id (PK), event_id, sportsbook, market, selection, price, outcome, units, profit, graded_at (currently unused)
- **odds_mapping_audit** — audit_id (PK), event_id, odd_id, sportsbook, raw_participant_id, raw_participant_name, matched_team_id, matched_team_name, mapping_method, mapping_confidence, validation_status, validation_reason, price, created_at
- **player_prop_odds** — id (PK), event_id, odd_id, sportsbook, player_id, player_name, team_id, team_name, market_type, market_group_key, side, line, price, decimal_odds, is_alt_line, available, validation_status, mapping_confidence, mapping_method, validation_reason, captured_at, created_at
- **player_prop_mapping_audit** — audit_id (PK), event_id, odd_id, sportsbook, player_id, player_name, team_id, team_name, market_type, market_group_key, side, line, price, decimal_odds, is_alt_line, available, validation_status, mapping_confidence, mapping_method, validation_reason, excluded, exclusion_reasons, captured_at, created_at
