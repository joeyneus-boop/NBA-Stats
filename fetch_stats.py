"""Downloads NBA per-game player stats from ESPN's free public feed and saves stats.json.

Also looks up each player's draft year (once per player, remembered between runs
inside stats.json so later nights are fast).
"""
import json
import math
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

# Extra per-player stats from ESPN's detailed stats feed.
# Our column name -> ESPN stat names to try (first one found wins)
EXTRA_STATS = {
    "OREB":       ["avgOffensiveRebounds"],
    "DREB":       ["avgDefensiveRebounds"],
    "PER":        ["PER"],
    "USG_PCT":    ["usageRate"],
    "TS_PCT":     ["trueShootingPct"],
    "EFG_PCT":    ["effectiveFGPct"],
    "PLUS_MINUS": ["plusMinus"],
    "AST_TO":     ["assistTurnoverRatio"],
    "PTS40":      ["p40"],
    "REB40":      ["r40"],
    "AST40":      ["a40"],
    "POSS":       ["avgEstimatedPossessions"],
    "GS":         ["gamesStarted"],
    "DD2":        ["doubleDouble"],
    "TD3":        ["tripleDouble"],
    "VORP":       ["VORP"],
}
# Older totals to fall back on (divided by games played) if no per-game number exists
TOTAL_FALLBACKS = {"OREB": "offensiveRebounds", "DREB": "defensiveRebounds"}


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
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


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


def fetch_player_extras(players, season_year, debug):
    """Adds the EXTRA_STATS columns to each player. Returns the list of columns that worked."""
    todo = [p for p in players if p.get("ID") and (p.get("GP") or 0) > 0]
    print(f"Extra stats: looking up {len(todo)} players...")

    def one(p):
        data = get_json(CORE_STATS.format(year=season_year, id=p["ID"]), tries=2, timeout=15)
        return p, (flatten_stats(data) if isinstance(data, dict) else None)

    counts = {c: 0 for c in EXTRA_STATS}
    start = time.time()
    samples = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i in range(0, len(todo), 24):
            if time.time() - start > 400:
                print("Extra stats: time budget reached.")
                break
            for p, stats in pool.map(one, todo[i:i + 24]):
                if not stats:
                    continue
                if "extra_stat_names" not in debug:
                    debug["extra_stat_names"] = sorted(stats)
                for col, names in EXTRA_STATS.items():
                    val = next((stats[n] for n in names if n in stats), None)
                    if val is None and col in TOTAL_FALLBACKS and p.get("GP"):
                        tot = stats.get(TOTAL_FALLBACKS[col])
                        val = tot / p["GP"] if tot is not None else None
                    if val is not None:
                        p[col] = round(val, 4)
                        counts[col] += 1
                if len(samples) < 4 and p.get("MIN", 0) and p["MIN"] > 20:
                    samples.append({"player": p["PLAYER"], **{c: p.get(c) for c in EXTRA_STATS}})
            if i == 0 and sum(counts.values()) == 0:
                print("Extra stats: first batch returned nothing. Skipping.")
                return []
    debug["extra_samples"] = samples
    good = [c for c, n in counts.items() if n >= max(10, len(todo) // 2)]
    print("Extra stats added:", ", ".join(good) or "none")
    print("Extra stats missing:", ", ".join(c for c in EXTRA_STATS if c not in good) or "none")
    debug["extra_counts"] = counts
    return good


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

    extra_cols = []
    try:
        extra_cols = fetch_player_extras(players, season_year, debug)
    except Exception as err:  # never let this block the main stats
        print("Extra stats skipped because of an error:", err)

    for p in players:
        p["DRAFT"] = cache.get(str(p["ID"])) if p.get("ID") else None

    out = {
        "season": season_label,
        "updated": datetime.now(timezone.utc).isoformat(),
        "columns": [c for c in COLUMNS if c in index or c in extra_cols]
                   + [c for c in EXTRA_STATS if c in extra_cols and c not in COLUMNS],
        "draft_cache": cache,
        "debug": debug,
        "players": players,
    }
    with open("stats.json", "w") as f:
        json.dump(out, f)
    print(f"Saved {len(players)} players.")


if __name__ == "__main__":
    main()
