"""Clean up dropped-market picks on Render PostgreSQL."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.connection import get_connection

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

# Delete official_picks first (FK)
op_count = conn.execute(
    'SELECT COUNT(*) FROM official_picks WHERE recommendation_id IN ('
    'SELECT recommendation_id FROM historical_recommendations WHERE market_type IN (' + placeholders + '))',
    dropped
).fetchone()[0]
print(f'  official_picks to delete: {op_count}')

if op_count:
    conn.execute(
        'DELETE FROM official_picks WHERE recommendation_id IN ('
        'SELECT recommendation_id FROM historical_recommendations WHERE market_type IN (' + placeholders + '))',
        dropped
    )

# Delete historical_recommendations
hr_count = conn.execute(
    'SELECT COUNT(*) FROM historical_recommendations WHERE market_type IN (' + placeholders + ')',
    dropped
).fetchone()[0]
print(f'  historical_recommendations to delete: {hr_count}')

if hr_count:
    conn.execute(
        'DELETE FROM historical_recommendations WHERE market_type IN (' + placeholders + ')',
        dropped
    )

conn.commit()
conn.close()
print('Done — PostgreSQL cleanup complete')
