"""Downloads past NBA seasons from ESPN's free public feed, one file per season in data/.

- Safe to run again and again: seasons that are already saved are skipped.
- Works from the newest finished season backwards until ESPN has no more data.
- Saves (and, on GitHub, commits) after every season, so a long run never loses progress.
- Reuses the same code as fetch_stats.py, so the nightly update is not touched.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import build_career
import fetch_stats as fs

DATA_DIR = "data"
TIME_BUDGET = 5 * 3600 + 15 * 60   # stop starting new seasons after ~5h15m (GitHub stops jobs at 6h)
EARLIEST_POSSIBLE = 1947
EMPTY_SEASONS_TO_STOP = 2          # this many empty seasons in a row = ESPN has nothing older


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True)


def commit_progress(message):
    """On GitHub, save progress right away. On a normal computer this does nothing."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    sh("git", "config", "user.name", "stats-bot")
    sh("git", "config", "user.email", "stats-bot@users.noreply.github.com")
    sh("git", "add", DATA_DIR)
    if sh("git", "diff", "--staged", "--quiet").returncode == 0:
        return
    sh("git", "commit", "-m", message)
    for _ in range(5):
        sh("git", "pull", "--rebase", "--autostash", "origin", "main")
        r = sh("git", "push")
        if r.returncode == 0:
            return
        time.sleep(6)
    print("  (could not push this save yet; will try again after the next season)", r.stderr[-200:])


def current_end_year():
    """The season stats.json holds is the 'current' one; everything before it is 'past'."""
    cur = build_career.read_json("stats.json") or {}
    y = build_career.season_end_year(cur.get("season"))
    if y:
        return y
    now = datetime.now(timezone.utc)
    return now.year + 1 if now.month >= 8 else now.year


def get_season_page(year, page):
    params = urllib.parse.urlencode({"limit": fs.PAGE_SIZE, "page": page, "isqualified": "false",
                                     "season": year, "seasontype": 2})
    return fs.get_json(f"{fs.URL}?{params}")


class ESPNError(Exception):
    pass


def fetch_basic_season(year, debug):
    """Returns (players, label) or None when ESPN has no data for that season. Raises ESPNError on network trouble."""
    players, index, label = [], None, f"{year - 1}-{str(year)[2:]} season"
    for page in range(1, fs.MAX_PAGES + 1):
        data = get_season_page(year, page)
        if data is None:
            raise ESPNError(f"ESPN did not answer for {label}, page {page}.")
        athletes = data.get("athletes") or []
        if page == 1:
            req = data.get("requestedSeason")
            if isinstance(req, dict) and isinstance(req.get("year"), int) and req["year"] != year:
                print(f"  ESPN answered with season {req['year']} instead of {year}. Treating {label} as unavailable.")
                return None
            if not isinstance(req, dict):
                print("  note: ESPN did not say which season this is; trusting the request.")
            index, names = fs.build_index(data.get("categories") or [])
            debug["stat_names_seen"] = names
            if "PTS" not in index or "GP" not in index:
                print(f"  {label}: no points / games columns, treating as unavailable.")
                return None
        if not athletes:
            break
        for a in athletes:
            info = a.get("athlete") or {}
            row = {
                "ID": str(info.get("id")) if info.get("id") is not None else None,
                "PLAYER": info.get("displayName") or info.get("shortName") or "?",
                "TEAM": info.get("teamShortName") or info.get("teamName") or "",
                "POS": fs.position_of(info),
            }
            cats = a.get("categories") or []
            for col, (ci, vi) in index.items():
                try:
                    row[col] = fs.to_number(cats[ci]["values"][vi])
                except (IndexError, KeyError, TypeError):
                    row[col] = None
            if row["ID"] and (row.get("GP") or 0) > 0:
                players.append(row)
        pages = (data.get("pagination") or {}).get("pages")
        if pages and page >= pages:
            break
        time.sleep(0.4)
    best = {}
    for p in players:  # in case ESPN lists a traded player more than once, keep the row with the most games
        if p["ID"] not in best or (p.get("GP") or 0) > (best[p["ID"]].get("GP") or 0):
            best[p["ID"]] = p
    players = list(best.values())
    if len(players) < 20:
        print(f"  {label}: only {len(players)} players found, treating as unavailable.")
        return None
    cols = [c for c in fs.COLUMNS if c in index]
    return players, label, cols


def clean(p):
    out = {}
    for k, v in p.items():
        if v is None:
            continue
        out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def load_draft_cache():
    cache = dict(fs.load_previous_cache())
    saved = build_career.read_json(os.path.join(DATA_DIR, "draft_cache.json")) or {}
    cache.update(saved)
    return cache


def do_season(year, cache):
    debug = {}
    got = fetch_basic_season(year, debug)
    if got is None:
        return None
    players, label, cols = got
    print(f"  {label}: {len(players)} players. Looking up extra stats...")

    extra = []
    try:
        extra = fs.fetch_player_extras(players, year, debug)
    except Exception as err:
        print("  extra stats skipped because of an error:", err)
    try:
        extra += fs.add_usage_rate(players)
    except Exception as err:
        print("  usage rate skipped because of an error:", err)
    try:
        fs.fetch_drafts(players, cache, debug)
    except Exception as err:
        print("  draft years skipped because of an error:", err)
    for p in players:
        p["DRAFT"] = cache.get(str(p["ID"]))

    columns = [c for c in fs.COLUMNS if c in cols or c in extra] + [c for c in fs.EXTRA_ORDER if c in extra and c not in fs.COLUMNS]
    out = {
        "season": label,
        "year": year,
        "updated": datetime.now(timezone.utc).isoformat(),
        "columns": columns,
        "players": [clean(p) for p in players],
    }
    return out


def main():
    start = time.time()
    force = [int(a) for a in sys.argv[1:] if a.isdigit()]
    os.makedirs(DATA_DIR, exist_ok=True)
    top = current_end_year() - 1
    index = build_career.read_json(os.path.join(DATA_DIR, "index.json")) or {}
    floor = index.get("floor") or EARLIEST_POSSIBLE - 1
    cache = load_draft_cache()
    print(f"Past seasons: working from {top - 1}-{str(top)[2:]} backwards. Oldest known limit: {floor if floor > EARLIEST_POSSIBLE - 1 else 'none yet'}.")

    saved, empty_run, new_floor, stopped_early = 0, 0, None, False
    for year in range(top, floor, -1):
        path = os.path.join(DATA_DIR, f"{year}.json")
        if os.path.exists(path) and year not in force:
            continue
        if time.time() - start > TIME_BUDGET:
            print("Time budget used up. Run the backfill again to continue where it stopped.")
            stopped_early = True
            break
        print(f"Season ending {year}:")
        try:
            season = do_season(year, cache)
        except ESPNError as err:
            print("  " + str(err) + " Stopping here; run again later to continue.")
            stopped_early = True
            break
        if season is None:
            empty_run += 1
            if empty_run >= EMPTY_SEASONS_TO_STOP:
                new_floor = year + EMPTY_SEASONS_TO_STOP - 1
                print(f"ESPN has nothing older than {new_floor}-{str(new_floor + 1)[2:]}. Done going back.")
                break
            continue
        empty_run = 0
        with open(path, "w") as f:
            json.dump(season, f, separators=(",", ":"))
        with open(os.path.join(DATA_DIR, "draft_cache.json"), "w") as f:
            json.dump(cache, f, separators=(",", ":"))
        saved += 1
        print(f"  saved {path} ({len(season['players'])} players, columns: {', '.join(season['columns'])})")
        try:
            build_career.main(floor=new_floor)
        except Exception as err:
            print("  career update skipped because of an error:", err)
        commit_progress(f"Add season data {year}")

    try:
        build_career.main(floor=new_floor)
    except Exception as err:
        print("Career update skipped because of an error:", err)
    commit_progress("Update career averages")
    print(f"Done. Saved {saved} new season(s) this run.{' (stopped early)' if stopped_early else ''}")


if __name__ == "__main__":
    main()
