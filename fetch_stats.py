"""Downloads NBA per-game player stats from ESPN's free public feed and saves stats.json."""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete"
PAGE_SIZE = 50
MAX_PAGES = 30

# Our column name -> (possible ESPN stat names, possible ESPN short labels)
COLUMNS = {
    "GP":       (["gamesPlayed"], ["GP"]),
    "MIN":      (["avgMinutes", "minutes"], ["MIN"]),
    "PTS":      (["avgPoints"], ["PTS"]),
    "REB":      (["avgRebounds"], ["REB"]),
    "OREB":     (["avgOffensiveRebounds"], ["OREB", "OR"]),
    "DREB":     (["avgDefensiveRebounds"], ["DREB", "DR"]),
    "AST":      (["avgAssists"], ["AST"]),
    "STL":      (["avgSteals"], ["STL"]),
    "BLK":      (["avgBlocks"], ["BLK"]),
    "TOV":      (["avgTurnovers"], ["TO", "TOV"]),
    "PF":       (["avgFouls", "avgPersonalFouls"], ["PF"]),
    "FGM":      (["avgFieldGoalsMade"], ["FGM"]),
    "FGA":      (["avgFieldGoalsAttempted"], ["FGA"]),
    "FG_PCT":   (["fieldGoalPct"], ["FG%"]),
    "FG3M":     (["avgThreePointFieldGoalsMade"], ["3PM"]),
    "FG3A":     (["avgThreePointFieldGoalsAttempted"], ["3PA"]),
    "FG3_PCT":  (["threePointFieldGoalPct", "threePointPct"], ["3P%"]),
    "FTM":      (["avgFreeThrowsMade"], ["FTM"]),
    "FTA":      (["avgFreeThrowsAttempted"], ["FTA"]),
    "FT_PCT":   (["freeThrowPct"], ["FT%"]),
}


def get_page(page):
    params = urllib.parse.urlencode(
        {"limit": PAGE_SIZE, "page": page, "isqualified": "false"}
    )
    req = urllib.request.Request(
        f"{URL}?{params}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; nba-stat-lab)",
                 "Accept": "application/json"},
    )
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as err:
            last_err = err
            print(f"Page {page}, attempt {attempt + 1} failed: {err}")
            time.sleep(5)
    raise SystemExit(f"Could not download page {page}: {last_err}")


def to_number(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def build_index(header_categories):
    """Map each of our columns to (category position, value position)."""
    by_name, by_label = {}, {}
    for ci, cat in enumerate(header_categories):
        names = cat.get("names") or []
        labels = cat.get("labels") or []
        for vi, n in enumerate(names):
            by_name.setdefault(n.lower(), (ci, vi))
        for vi, l in enumerate(labels):
            by_label.setdefault(l.upper(), (ci, vi))
    index = {}
    for col, (names, labels) in COLUMNS.items():
        for n in names:
            if n.lower() in by_name:
                index[col] = by_name[n.lower()]
                break
        else:
            for l in labels:
                if l.upper() in by_label:
                    index[col] = by_label[l.upper()]
                    break
    return index, sorted(by_name)


def main():
    players, index, season_label = [], None, "Current season"
    seen_names = []

    for page in range(1, MAX_PAGES + 1):
        data = get_page(page)
        athletes = data.get("athletes") or []
        if page == 1:
            index, seen_names = build_index(data.get("categories") or [])
            req_season = data.get("requestedSeason")
            if isinstance(req_season, dict) and isinstance(req_season.get("year"), int):
                y = req_season["year"]
                season_label = f"{y - 1}-{str(y)[2:]} season"
            print("ESPN stat names seen:", ", ".join(seen_names))
            print("Matched columns:", ", ".join(index))
            print("Missing columns:", ", ".join(c for c in COLUMNS if c not in index) or "none")
            if "PTS" not in index or "GP" not in index:
                raise SystemExit("Could not find points / games played in ESPN's data. "
                                 "Send me the log above and I'll adjust the script.")
        if not athletes:
            break

        for a in athletes:
            info = a.get("athlete") or {}
            row = {
                "PLAYER": info.get("displayName") or info.get("shortName") or "?",
                "TEAM": info.get("teamShortName") or info.get("teamName") or "",
            }
            cats = a.get("categories") or []
            for col, (ci, vi) in index.items():
                try:
                    row[col] = to_number(cats[ci]["values"][vi])
                except (IndexError, KeyError, TypeError):
                    row[col] = None
            players.append(row)

        pages = (data.get("pagination") or {}).get("pages")
        if pages and page >= pages:
            break
        time.sleep(0.5)

    if not players:
        raise SystemExit("ESPN returned no players. Leaving stats.json alone.")

    out = {
        "season": season_label,
        "updated": datetime.now(timezone.utc).isoformat(),
        "columns": [c for c in COLUMNS if c in index],
        "players": players,
    }
    with open("stats.json", "w") as f:
        json.dump(out, f)
    print(f"Saved {len(players)} players.")


if __name__ == "__main__":
    main()
