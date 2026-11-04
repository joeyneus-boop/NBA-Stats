"""Downloads game-by-game "highlight games" from ESPN's free public feed into data/games/.

A highlight game is one with 30+ points, 15+ rebounds, 12+ assists, 5+ steals, 6+ blocks,
7+ threes, or a triple-double. Keeping only those keeps the files small enough for a web page.

  python fetch_games.py            -> the current season (refreshed) + every past season not saved yet
  python fetch_games.py current    -> just the current season (used by the nightly update)
  python fetch_games.py 2019       -> force a re-download of the season ending in 2019

Safe to run again and again. Saves (and, on GitHub, commits) after every season.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import backfill
import build_career
import fetch_stats as fs

GAMELOG = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog?season={year}"
GAMELOG_ALT = GAMELOG + "&region=us&lang=en&contentorigin=espn"
GAMES_DIR = os.path.join("data", "games")
WORKERS = 8
TIME_BUDGET = 5 * 3600            # stop starting new seasons after 5 hours (GitHub stops jobs at 6)
EMPTY_SEASONS_TO_STOP = 2
THRESHOLDS = {"PTS": 30, "REB": 15, "AST": 12, "STL": 5, "BLK": 6, "FG3M": 7}
COLS = ["ID", "PLAYER", "TEAM", "OPP", "HA", "DATE", "RES", "SCORE", "TYPE",
        "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV", "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "PF"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nba-stat-lab)", "Accept": "application/json"}


class FormatError(Exception):
    pass


class TooManyErrors(Exception):
    pass


def http_json(url, tries=3, timeout=20):
    """Returns (ok, data). ok=False means ESPN could not be reached; data=None with ok=True means 'no such log'."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (400, 404):
                return True, None
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return False, None


def fetch_log(pid, year):
    ok, data = http_json(GAMELOG.format(id=pid, year=year))
    if ok and data is None:                       # try the longer form of the address once
        ok2, data2 = http_json(GAMELOG_ALT.format(id=pid, year=year), tries=1)
        if ok2 and data2 is not None:
            return True, data2
    return ok, data


def pair(text):
    """'8-17' -> (8.0, 17.0)"""
    try:
        a, b = str(text).split("-")
        return fs.to_number(a), fs.to_number(b)
    except Exception:
        return None, None


def parse_gamelog(data):
    """Turns one ESPN game log into a list of game dicts. Returns None when the layout isn't recognised."""
    labels = [str(l).upper() for l in (data.get("labels") or [])]
    if "PTS" not in labels:
        return None
    ix = {l: i for i, l in enumerate(labels)}
    events = data.get("events") if isinstance(data.get("events"), dict) else {}
    out, seen = [], set()
    for st in data.get("seasonTypes") or []:
        name = (st.get("displayName") or "").lower()
        typ = "P" if ("post" in name or "playoff" in name) else ("R" if "regular" in name else None)
        if typ is None:
            continue
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                eid = str(ev.get("eventId"))
                stats = ev.get("stats") or []
                if eid in seen or not stats:
                    continue
                seen.add(eid)

                def get(label):
                    i = ix.get(label)
                    return stats[i] if i is not None and i < len(stats) else None

                pts = fs.to_number(get("PTS"))
                if pts is None:           # did not play
                    continue
                fgm, fga = pair(get("FG"))
                f3m, f3a = pair(get("3PT"))
                ftm, fta = pair(get("FT"))
                meta = events.get(eid) or {}
                opp = meta.get("opponent") if isinstance(meta.get("opponent"), dict) else {}
                home_id, away_id = str(meta.get("homeTeamId") or ""), str(meta.get("awayTeamId") or "")
                at_vs = meta.get("atVs") or ""
                out.append({
                    "eid": eid,
                    "DATE": (meta.get("gameDate") or "")[:10],
                    "OPP": opp.get("abbreviation") or opp.get("displayName") or "",
                    "opp_id": str(opp.get("id") or ""),
                    "own_id": away_id if at_vs == "@" else home_id,
                    "HA": "@" if at_vs == "@" else "vs",
                    "RES": meta.get("gameResult") or "",
                    "SCORE": str(meta.get("score") or ""),
                    "TYPE": typ,
                    "MIN": fs.to_number(get("MIN")),
                    "PTS": pts,
                    "REB": fs.to_number(get("REB")),
                    "AST": fs.to_number(get("AST")),
                    "STL": fs.to_number(get("STL")),
                    "BLK": fs.to_number(get("BLK")),
                    "TOV": fs.to_number(get("TO")),
                    "FGM": fgm, "FGA": fga, "FG3M": f3m, "FG3A": f3a, "FTM": ftm, "FTA": fta,
                    "PF": fs.to_number(get("PF")),
                })
    return out


def is_highlight(g):
    if any(g.get(k) is not None and g[k] >= t for k, t in THRESHOLDS.items()):
        return True
    return sum(1 for k in ("PTS", "REB", "AST", "STL", "BLK") if (g.get(k) or 0) >= 10) >= 3


def collect_season(year, label, plist, deadline):
    """plist = [(id, name, team, gp)]. Returns (rows, parsed_total) or None if ESPN has no logs for that season."""
    # a quick look at the busiest players first, so an empty season is spotted in seconds
    plist = sorted(plist, key=lambda x: -(x[3] or 0))
    team_abbr = {}
    games, parsed_total, got_data, errors_in_row = [], 0, 0, 0
    first_debug = {}

    def one(p):
        return p, fetch_log(p[0], year)

    def absorb(p, ok, data):
        nonlocal parsed_total, got_data, errors_in_row
        if not ok:
            errors_in_row += 1
            if errors_in_row >= 40:
                raise TooManyErrors("ESPN stopped answering.")
            return
        errors_in_row = 0
        if data is None:
            return
        got_data += 1
        parsed = parse_gamelog(data) if isinstance(data, dict) else None
        if "keys" not in first_debug:
            first_debug.update(keys=sorted(data.keys()) if isinstance(data, dict) else str(type(data)),
                               labels=data.get("labels") if isinstance(data, dict) else None)
        if parsed is None:
            return
        for g in parsed:
            parsed_total += 1
            if g["opp_id"] and g["OPP"]:
                team_abbr[g["opp_id"]] = g["OPP"]
            if is_highlight(g):
                g["ID"], g["PLAYER"], g["fallback_team"] = p[0], p[1], p[2]
                games.append(g)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        # first batch decides whether this season exists at all
        head = plist[:30]
        for p, (ok, data) in pool.map(one, head):
            absorb(p, ok, data)
        if parsed_total == 0:
            if got_data > 0:
                raise FormatError(f"ESPN answered for {got_data} players, but the game-log layout was not recognised. "
                                  f"Keys seen: {first_debug.get('keys')}. Labels: {first_debug.get('labels')}")
            return None
        print(f"  first 30 players OK ({parsed_total} games parsed). Reading the rest...")
        rest = plist[30:]
        for i in range(0, len(rest), 80):
            if time.time() > deadline:
                raise TooManyErrors("Time budget used up in the middle of a season.")
            for p, (ok, data) in pool.map(one, rest[i:i + 80]):
                absorb(p, ok, data)
            if (i // 80) % 3 == 2:
                print(f"  ...{min(30 + i + 80, len(plist))} of {len(plist)} players, {len(games)} highlight games so far")

    rows = []
    for g in games:
        team = team_abbr.get(g["own_id"]) or g["fallback_team"] or ""
        rows.append([g["ID"], g["PLAYER"], team] + [g.get(c) for c in COLS[3:]])
    rows.sort(key=lambda r: (r[COLS.index("DATE")] or "", r[0]))
    return rows, parsed_total


def clean_row(r):
    return [round(v, 2) if isinstance(v, float) else v for v in r]


def write_games(year, label, rows, parsed_total):
    os.makedirs(GAMES_DIR, exist_ok=True)
    with open(os.path.join(GAMES_DIR, f"{year}.json"), "w") as f:
        json.dump({"season": label, "year": year, "updated": datetime.now(timezone.utc).isoformat(),
                   "games_seen": parsed_total, "cols": COLS, "rows": [clean_row(r) for r in rows]},
                  f, separators=(",", ":"))


def write_index(floor=None):
    old = build_career.read_json(os.path.join(GAMES_DIR, "index.json")) or {}
    seasons = []
    for path in sorted(os.listdir(GAMES_DIR) if os.path.isdir(GAMES_DIR) else []):
        if path.endswith(".json") and path[:4].isdigit():
            d = build_career.read_json(os.path.join(GAMES_DIR, path))
            if d:
                seasons.append({"year": d["year"], "label": d["season"].replace(" season", ""),
                                "file": f"{GAMES_DIR.replace(os.sep, '/')}/{path}", "games": len(d.get("rows", []))})
    seasons.sort(key=lambda s: -s["year"])
    idx = {"updated": datetime.now(timezone.utc).isoformat(), "thresholds": THRESHOLDS, "seasons": seasons,
           "floor": floor if floor is not None else old.get("floor")}
    with open(os.path.join(GAMES_DIR, "index.json"), "w") as f:
        json.dump(idx, f, separators=(",", ":"))


def season_players(year, current=False):
    d = build_career.read_json("stats.json" if current else os.path.join("data", f"{year}.json")) or {}
    return [(p["ID"], p.get("PLAYER", "?"), p.get("TEAM", ""), p.get("GP") or 0)
            for p in d.get("players", []) if p.get("ID") and (p.get("GP") or 0) > 0], d.get("season", "")


def main():
    start = time.time()
    deadline = start + TIME_BUDGET
    args = sys.argv[1:]
    current_only = "current" in args
    force = [int(a) for a in args if a.isdigit()]
    os.makedirs(GAMES_DIR, exist_ok=True)
    cur_year = backfill.current_end_year()
    idx = build_career.read_json(os.path.join(GAMES_DIR, "index.json")) or {}
    floor = idx.get("floor") or 0

    # 1) the current season is always refreshed
    plist, label = season_players(cur_year, current=True)
    if plist:
        print(f"Current season ({label}): {len(plist)} players")
        try:
            got = collect_season(cur_year, label, plist, deadline)
            if got:
                write_games(cur_year, label, got[0], got[1])
                print(f"  saved {len(got[0])} highlight games out of {got[1]} games.")
            else:
                print("  no game logs found for the current season.")
        except (FormatError, TooManyErrors) as err:
            print("  current season skipped:", err)
            if isinstance(err, FormatError) and current_only:
                sys.exit(str(err))
        write_index()

    if current_only:
        return

    # 2) past seasons, newest first
    saved, empty_run, new_floor = 0, 0, None
    for year in range(cur_year - 1, 1983, -1):
        if year <= floor:
            break
        path = os.path.join(GAMES_DIR, f"{year}.json")
        if os.path.exists(path) and year not in force:
            continue
        plist, label = season_players(year)
        if not plist:
            continue                      # that season has not been downloaded by backfill.py
        if time.time() > deadline:
            print("Time budget used up. Run this again to continue where it stopped.")
            break
        print(f"Season ending {year} ({label}): {len(plist)} players")
        try:
            got = collect_season(year, label, plist, deadline)
        except FormatError as err:
            write_index(new_floor)
            backfill.commit_progress("Update game logs")
            sys.exit(str(err))
        except TooManyErrors as err:
            print("  stopping:", err, "Run again later to continue.")
            break
        if got is None:
            empty_run += 1
            print("  no game logs for this season.")
            if empty_run >= EMPTY_SEASONS_TO_STOP:
                new_floor = year + EMPTY_SEASONS_TO_STOP - 1
                print(f"ESPN has no game logs older than the {new_floor}-{str(new_floor + 1)[2:]} season. Done going back.")
                break
            continue
        empty_run = 0
        write_games(year, label, got[0], got[1])
        write_index(new_floor)
        saved += 1
        print(f"  saved {len(got[0])} highlight games out of {got[1]} games.")
        backfill.commit_progress(f"Add game logs {year}")

    write_index(new_floor)
    backfill.commit_progress("Update game logs")
    print(f"Done. Saved {saved} past season(s) this run.")


if __name__ == "__main__":
    main()
