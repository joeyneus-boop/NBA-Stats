"""Downloads NBA per-game player stats from ESPN's free public feed and saves stats.json.

Also looks up each player's draft year (once per player, remembered between runs
inside stats.json so later nights are fast).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete"
CORE_ATHLETE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{}"
CORE_STATS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/seasons/{year}/types/2/athletes/{id}/statistics/0"
PAGE_SIZE = 50
MAX_PAGES = 30
DRAFT_TIME_BUDGET = 420  # seconds to spend on draft lookups per run

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


def get_json(url, tries=3, timeout=30):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; nba-stat-lab)",
                 "Accept": "application/json"},
    )
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as err:
            print(f"  request failed ({attempt + 1}/{tries}): {err}")
            if attempt < tries - 1:
                time.sleep(4)
    return None


def get_page(page):
    params = urllib.parse.urlencode(
        {"limit": PAGE_SIZE, "page": page, "isqualified": "false"}
    )
    data = get_json(f"{URL}?{params}")
    if data is None:
        raise SystemExit(f"Could not download page {page}.")
    return data


def to_number(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def position_of(info):
    pos = info.get("position")
    if isinstance(pos, dict):
        return pos.get("abbreviation") or pos.get("displayName")
    if isinstance(pos, str):
        return pos
    return info.get("positionAbbreviation") or None


def build_index(header_categories):
    """Map each of our columns to (category position, value position)."""
    by_name, by_label = {}, {}
    for ci, cat in enumerate(header_categories):
        for vi, n in enumerate(cat.get("names") or []):
            by_name.setdefault(n.lower(), (ci, vi))
        for vi, l in enumerate(cat.get("labels") or []):
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


def load_previous_cache():
    try:
        with open("stats.json") as f:
            return dict(json.load(f).get("draft_cache") or {})
    except Exception:
        return {}


def fetch_drafts(players, cache, debug):
    """Fill cache[player_id] = draft year (0 = undrafted) for players not seen before."""
    todo = [p for p in players if p.get("ID") and str(p["ID"]) not in cache]
    if not todo:
        print("Draft years: nothing new to look up.")
        return
    print(f"Draft years: looking up {len(todo)} players...")
    start = time.time()
    found, ok, consecutive_fails = {}, 0, 0
    for p in todo:
        if time.time() - start > DRAFT_TIME_BUDGET:
            print("Draft lookups: time budget reached, will continue next run.")
            break
        data = get_json(CORE_ATHLETE.format(p["ID"]), tries=1, timeout=15)
        if not isinstance(data, dict):
            consecutive_fails += 1
            if ok == 0 and consecutive_fails >= 8:
                print("Draft lookups: ESPN's player service isn't answering. Skipping.")
                break
            continue
        consecutive_fails = 0
        ok += 1
        draft = data.get("draft")
        year = 0
        if isinstance(draft, dict):
            y = to_number(draft.get("year"))
            if y:
                year = int(y)
        found[str(p["ID"])] = year
        if "core_keys" not in debug:
            debug["core_keys"] = sorted(data.keys())
            debug["core_draft"] = draft
        time.sleep(0.1)

    drafted = sum(1 for y in found.values() if y)
    if ok >= 20 and drafted == 0:
        # Looks like the data format isn't what we expected; don't remember "undrafted" for everyone.
        print("Draft lookups: no draft years found at all; ignoring this batch.")
        return
    cache.update(found)
    print(f"Draft years: stored {len(found)} players ({drafted} drafted).")


def flatten_stats(data):
    """Turn ESPN's nested stats response into {stat name: number}."""
    out = {}
    splits = data.get("splits") if isinstance(data, dict) else None
    for cat in (splits or {}).get("categories") or []:
        for st in cat.get("stats") or []:
            name, val = st.get("name"), to_number(st.get("value"))
            if name and val is not None:
                out.setdefault(name, val)
    return out


def pick_per_game(stats, per_game_name, total_name, gp):
    if per_game_name in stats:
        return stats[per_game_name]
    if total_name in stats and gp:
        return stats[total_name] / gp
    return None


def fetch_rebound_split(players, season_year, debug):
    """Adds OREB and DREB (per game) to each player. Returns True if it worked."""
    todo = [p for p in players if p.get("ID") and (p.get("GP") or 0) > 0]
    print(f"Rebound split: looking up {len(todo)} players...")

    def one(p):
        data = get_json(CORE_STATS.format(year=season_year, id=p["ID"]), tries=2, timeout=15)
        return p, (flatten_stats(data) if isinstance(data, dict) else None)

    ok = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i in range(0, len(todo), 24):
            if time.time() - start > 400:
                print("Rebound split: time budget reached.")
                break
            batch = todo[i:i + 24]
            for p, stats in pool.map(one, batch):
                if stats is None:
                    continue
                if "rebound_stat_names" not in debug and stats:
                    debug["rebound_stat_names"] = sorted(stats)
                o = pick_per_game(stats, "avgOffensiveRebounds", "offensiveRebounds", p.get("GP"))
                d = pick_per_game(stats, "avgDefensiveRebounds", "defensiveRebounds", p.get("GP"))
                if o is not None or d is not None:
                    p["OREB"], p["DREB"] = o, d
                    ok += 1
            if i == 0 and ok == 0:
                print("Rebound split: first batch returned nothing. Skipping.")
                return False
    print(f"Rebound split: got numbers for {ok} of {len(todo)} players.")
    return ok >= max(10, len(todo) // 2)


def main():
    players, index, season_label = [], None, "Current season"
    now = datetime.now(timezone.utc)
    season_year = now.year + 1 if now.month >= 8 else now.year
    debug = {}

    for page in range(1, MAX_PAGES + 1):
        data = get_page(page)
        athletes = data.get("athletes") or []
        if page == 1:
            index, seen_names = build_index(data.get("categories") or [])
            debug["stat_names_seen"] = seen_names
            req_season = data.get("requestedSeason")
            if isinstance(req_season, dict) and isinstance(req_season.get("year"), int):
                y = req_season["year"]
                season_year = y
                season_label = f"{y - 1}-{str(y)[2:]} season"
            print("Matched columns:", ", ".join(index))
            print("Missing columns:", ", ".join(c for c in COLUMNS if c not in index) or "none")
            if "PTS" not in index or "GP" not in index:
                raise SystemExit("Could not find points / games played in ESPN's data.")
        if not athletes:
            break

        for a in athletes:
            info = a.get("athlete") or {}
            if "athlete_sample" not in debug:
                debug["athlete_sample"] = info
            age = to_number(info.get("age"))
            row = {
                "ID": str(info.get("id")) if info.get("id") is not None else None,
                "PLAYER": info.get("displayName") or info.get("shortName") or "?",
                "TEAM": info.get("teamShortName") or info.get("teamName") or "",
                "POS": position_of(info),
                "AGE": int(age) if age else None,
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

    cache = load_previous_cache()
    try:
        fetch_drafts(players, cache, debug)
    except Exception as err:  # never let draft trouble block the main stats
        print("Draft lookups skipped because of an error:", err)

    have_split = "OREB" in index and "DREB" in index
    if not have_split:
        try:
            have_split = fetch_rebound_split(players, season_year, debug)
        except Exception as err:
            print("Rebound split skipped because of an error:", err)
            have_split = False

    for p in players:
        p["DRAFT"] = cache.get(str(p["ID"])) if p.get("ID") else None

    out = {
        "season": season_label,
        "updated": datetime.now(timezone.utc).isoformat(),
        "columns": [c for c in COLUMNS if c in index or (have_split and c in ("OREB", "DREB"))],
        "draft_cache": cache,
        "debug": debug,
        "players": players,
    }
    with open("stats.json", "w") as f:
        json.dump(out, f)
    print(f"Saved {len(players)} players.")


if __name__ == "__main__":
    main()
