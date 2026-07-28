"""Clean up recommendations for the 13 dropped markets from PostgreSQL."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.connection import get_connection, get_database_url

url = get_database_url()
if not url:
    print("ERROR: DATABASE_URL not set. Run this from your dashboard terminal.")
    sys.exit(1)

conn = get_connection()

dropped = [
    'pitching_outs_ou', 'pitching_earnedRuns_ou', 'pitching_pitchesThrown_ou',
    'batting_hits+runs+rbi_ou', 'batting_RBI_ou', 'batting_runs_ou',
    'batting_runs+rbi_ou', 'batting_singles_ou', 'batting_doubles_ou',
    'batting_basesOnBalls_ou', 'batting_triples_ou', 'batting_strikeouts_ou',
    'batting_firstHomeRun_ou',
    'pitching_outs_yn', 'pitching_earnedRuns_yn', 'pitching_pitchesThrown_yn',
    'batting_hits+runs+rbi_yn', 'batting_RBI_yn', 'batting_runs_yn',
    'batting_runs+rbi_yn', 'batting_singles_yn', 'batting_doubles_yn',
    'batting_basesOnBalls_yn', 'batting_triples_yn', 'batting_strikeouts_yn',
    'batting_firstHomeRun_yn',
]

placeholders = ','.join('%s' for _ in dropped)

count_hr = conn.execute(
    f'SELECT COUNT(*) FROM historical_recommendations WHERE market_type IN ({placeholders})',
    dropped
).fetchone()[0]
print(f'historical_recommendations to delete: {count_hr}')

count_op = conn.execute(
    f'''SELECT COUNT(*) FROM official_picks op
        JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
        WHERE hr.market_type IN ({placeholders})''',
    dropped
).fetchone()[0]
print(f'official_picks to delete: {count_op}')

if count_op:
    conn.execute(
        f'''DELETE FROM official_picks WHERE recommendation_id IN (
            SELECT recommendation_id FROM historical_recommendations
            WHERE market_type IN ({placeholders})
        )''',
        dropped
    )
    print(f'Deleted {count_op} official_picks')

if count_hr:
    conn.execute(
        f'DELETE FROM historical_recommendations WHERE market_type IN ({placeholders})',
        dropped
    )
    print(f'Deleted {count_hr} historical_recommendations')

conn.commit()
conn.close()
print('PostgreSQL cleanup complete')
