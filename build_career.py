"""Builds career averages (data/career.json) and the season list (data/index.json).

Uses only files already in the repo (stats.json for the current season and data/YYYY.json
for past seasons), so it needs no internet and runs in a second or two.
"""
import glob
import json
import os
import re
from datetime import datetime, timezone

DATA_DIR = "data"

# per-game numbers: career value = average over every season, weighted by games played
PER_GAME = ["MIN", "PTS", "REB", "OREB", "DREB", "AST", "STL", "BLK", "TOV", "PF",
            "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "PLUS_MINUS"]
# weighted by total minutes played instead
MINUTE_WEIGHTED = ["PER", "USG_PCT"]
# season totals: career value = sum over every season
TOTALS = ["GS", "DD2", "TD3"]

ORDER = ["GP", "MIN", "PTS", "REB", "OREB", "DREB", "AST", "STL", "BLK", "TOV", "PF",
         "FGM", "FGA", "FG_PCT", "FG3M", "FG3A", "FG3_PCT", "FTM", "FTA", "FT_PCT",
         "PER", "USG_PCT", "TS_PCT", "EFG_PCT", "PLUS_MINUS", "AST_TO",
         "PTS40", "REB40", "AST40", "GS", "DD2", "TD3", "SEASONS"]


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def season_end_year(label):
    """'2025-26 season' -> 2026"""
    m = re.search(r"(\d{4})-(\d{2})", label or "")
    if m:
        return int(m.group(1)) + 1
    m = re.search(r"(\d{4})", label or "")
    return int(m.group(1)) if m else None


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def load_seasons():
    """[(end_year, label, players)] for every past season plus the current one, oldest first."""
    seasons = {}
    for path in glob.glob(os.path.join(DATA_DIR, "[0-9][0-9][0-9][0-9].json")):
        d = read_json(path)
        if d and d.get("players"):
            y = d.get("year") or season_end_year(d.get("season"))
            if y:
                seasons[y] = (d.get("season") or str(y), d["players"])
    cur = read_json("stats.json")
    if cur and cur.get("players"):
        y = season_end_year(cur.get("season"))
        if y:
            seasons[y] = (cur.get("season") or str(y), cur["players"])  # newest data always wins
    return [(y, *seasons[y]) for y in sorted(seasons)]


def build_career(seasons):
    people = {}
    for y, label, players in seasons:
        for p in players:
            pid = p.get("ID")
            gp = num(p.get("GP"))
            if not pid or not gp or gp <= 0:
                continue
            a = people.setdefault(pid, {"num": {}, "den": {}, "gp": 0, "tot": {}, "n": 0, "last_y": 0})
            a["gp"] += gp
            a["n"] += 1
            for k in PER_GAME:
                v = num(p.get(k))
                if v is not None:
                    a["num"][k] = a["num"].get(k, 0) + v * gp
                    a["den"][k] = a["den"].get(k, 0) + gp
            mins = (num(p.get("MIN")) or 0) * gp
            for k in MINUTE_WEIGHTED:
                v = num(p.get(k))
                if v is not None and mins > 0:
                    a["num"][k] = a["num"].get(k, 0) + v * mins
                    a["den"][k] = a["den"].get(k, 0) + mins
            for k in TOTALS:
                v = num(p.get(k))
                if v is not None:
                    a["tot"][k] = a["tot"].get(k, 0) + v
            if y >= a["last_y"]:  # latest season decides name, team, position, draft year
                a["last_y"] = y
                a["info"] = {k: p.get(k) for k in ("ID", "PLAYER", "TEAM", "POS", "DRAFT")}

    out = []
    for pid, a in people.items():
        row = dict(a["info"])
        row["GP"] = int(a["gp"]) if float(a["gp"]).is_integer() else round(a["gp"], 2)
        row["SEASONS"] = a["n"]
        avg = {k: a["num"][k] / a["den"][k] for k in a["num"] if a["den"].get(k)}
        for k, v in avg.items():
            row[k] = round(v, 4)
        for k, v in a["tot"].items():
            row[k] = round(v, 4)
        # shooting percentages are recalculated from career makes and attempts
        if avg.get("FGA") and "FGM" in avg:
            row["FG_PCT"] = round(100 * avg["FGM"] / avg["FGA"], 3)
            if "FG3M" in avg:
                row["EFG_PCT"] = round(100 * (avg["FGM"] + 0.5 * avg["FG3M"]) / avg["FGA"], 3)
        if avg.get("FG3A") and "FG3M" in avg:
            row["FG3_PCT"] = round(100 * avg["FG3M"] / avg["FG3A"], 3)
        if avg.get("FTA") and "FTM" in avg:
            row["FT_PCT"] = round(100 * avg["FTM"] / avg["FTA"], 3)
        if "PTS" in avg and avg.get("FGA") is not None and "FTA" in avg:
            denom = 2 * (avg["FGA"] + 0.44 * avg["FTA"])
            if denom > 0:
                row["TS_PCT"] = round(100 * avg["PTS"] / denom, 3)
        if avg.get("TOV") and "AST" in avg:
            row["AST_TO"] = round(avg["AST"] / avg["TOV"], 3)
        if avg.get("MIN"):
            for src, dst in (("PTS", "PTS40"), ("REB", "REB40"), ("AST", "AST40")):
                if src in avg:
                    row[dst] = round(avg[src] / avg["MIN"] * 40, 3)
        out.append({k: v for k, v in row.items() if v is not None})
    out.sort(key=lambda r: -(r.get("PTS") or 0))
    cols = [c for c in ORDER if any(c in r for r in out)]
    return out, cols


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def write_index(seasons, career_label, floor=None):
    old = read_json(os.path.join(DATA_DIR, "index.json")) or {}
    cur = read_json("stats.json") or {}
    cur_year = season_end_year(cur.get("season"))
    past = [{"year": y, "label": label.replace(" season", ""), "file": f"{DATA_DIR}/{y}.json", "players": len(players)}
            for y, label, players in reversed(seasons) if y != cur_year]
    idx = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "current": {"year": cur_year, "label": (cur.get("season") or "").replace(" season", "")},
        "seasons": past,
        "career": {"file": f"{DATA_DIR}/career.json", "through": career_label} if career_label else None,
        "floor": floor if floor is not None else old.get("floor"),
    }
    write_json(os.path.join(DATA_DIR, "index.json"), idx)


def main(floor=None):
    seasons = load_seasons()
    past = [s for s in seasons if os.path.exists(os.path.join(DATA_DIR, f"{s[0]}.json"))]
    if not past:
        print("Career: no past seasons saved yet. Run the backfill first. Nothing to do.")
        return False
    players, cols = build_career(seasons)
    if not players:
        print("Career: no players built. Leaving files alone.")
        return False
    through = seasons[-1][1].replace(" season", "")
    write_json(os.path.join(DATA_DIR, "career.json"), {
        "season": f"Career averages (through {through})",
        "mode": "career",
        "updated": datetime.now(timezone.utc).isoformat(),
        "seasons_used": len(seasons),
        "columns": cols,
        "players": players,
    })
    write_index(seasons, through, floor)
    print(f"Career: {len(players)} players across {len(seasons)} seasons.")
    return True


if __name__ == "__main__":
    main()
