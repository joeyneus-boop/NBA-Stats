"""Downloads NBA per-game player stats and saves them to stats.json."""
import json
import time
from datetime import datetime, timezone

from nba_api.stats.endpoints import leaguedashplayerstats

SEASON = "2026-27"  # change this each new season

KEEP = [
    "PLAYER_NAME", "TEAM_ABBREVIATION", "GP", "MIN",
    "PTS", "REB", "OREB", "DREB", "AST", "STL", "BLK", "TOV", "PF",
    "FGM", "FGA", "FG_PCT", "FG3M", "FG3A", "FG3_PCT",
    "FTM", "FTA", "FT_PCT", "PLUS_MINUS",
]


def fetch():
    for attempt in range(3):
        try:
            data = leaguedashplayerstats.LeagueDashPlayerStats(
                season=SEASON,
                per_mode_detailed="PerGame",
                timeout=60,
            ).get_normalized_dict()
            return data["LeagueDashPlayerStats"]
        except Exception as err:  # try again a couple of times
            print(f"Attempt {attempt + 1} failed: {err}")
            time.sleep(10)
    raise SystemExit("Could not download stats after 3 tries.")


def main():
    rows = fetch()
    if not rows:
        # Don't overwrite good data with an empty file
        raise SystemExit("NBA.com returned no players. Leaving stats.json alone.")

    players = []
    for r in rows:
        p = {k: r.get(k) for k in KEEP}
        p["PLAYER"] = p.pop("PLAYER_NAME")
        p["TEAM"] = p.pop("TEAM_ABBREVIATION")
        players.append(p)

    out = {
        "season": SEASON,
        "updated": datetime.now(timezone.utc).isoformat(),
        "players": players,
    }
    with open("stats.json", "w") as f:
        json.dump(out, f)
    print(f"Saved {len(players)} players.")


if __name__ == "__main__":
    main()
