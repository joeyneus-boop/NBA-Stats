"""Team stats, pace and ratings, plus offensive / defensive rating for every player.

- Downloads each season's team numbers (own and opponent) from ESPN's free feed into data/teams/YYYY.json
- Works out pace, offensive / defensive / net rating and the four factors from them
- Adds ORTG, DRTG and NRTG (estimated with the Dean Oliver box-score formulas) to every player
  in stats.json and data/YYYY.json

Usage:  python build_teams.py           every season (safe to run again, uses saved files for old seasons)
        python build_teams.py current   only the current season (the nightly run)
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import build_career as bc
import fetch_stats as fs

TEAM_DIR = os.path.join("data", "teams")
DATA_DIR = "data"
BYTEAM = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byteam"
STANDINGS = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"

# our column -> ESPN stat name (per-game versions)
BASE = {
    "GP": "gamesPlayed", "PTS": "avgPoints", "FGM": "avgFieldGoalsMade", "FGA": "avgFieldGoalsAttempted",
    "FG3M": "avgThreePointFieldGoalsMade", "FG3A": "avgThreePointFieldGoalsAttempted",
    "FTM": "avgFreeThrowsMade", "FTA": "avgFreeThrowsAttempted", "OREB": "avgOffensiveRebounds",
    "DREB": "avgDefensiveRebounds", "REB": "avgRebounds", "AST": "avgAssists", "STL": "avgSteals",
    "BLK": "avgBlocks", "TOV": "avgTurnovers", "PF": "avgFouls",
}
ORDER = ["GP", "W", "L", "WIN_PCT", "PTS", "OPP_PTS", "NET_PTS", "PACE", "ORTG", "DRTG", "NRTG",
         "FGM", "FGA", "FG_PCT", "FG3M", "FG3A", "FG3_PCT", "FTM", "FTA", "FT_PCT",
         "OREB", "DREB", "REB", "AST", "STL", "BLK", "TOV", "PF",
         "EFG_PCT", "TS_PCT", "TOV_PCT", "OREB_PCT", "DREB_PCT", "FT_RATE", "FG3_RATE", "AST_TO",
         "OPP_FGM", "OPP_FGA", "OPP_FG_PCT", "OPP_FG3M", "OPP_FG3A", "OPP_FG3_PCT", "OPP_FTM", "OPP_FTA",
         "OPP_OREB", "OPP_DREB", "OPP_REB", "OPP_AST", "OPP_TOV", "OPP_EFG_PCT", "OPP_TOV_PCT"]


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def pct(a, b):
    return 100.0 * a / b if a is not None and b else None


def read_json(path):
    return bc.read_json(path)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


# ---------------------------------------------------------------- team numbers
def category_names(defs):
    """ESPN lists stat names once per category (own side only). Opponent categories reuse them."""
    out, first = [], {}
    for c in defs:
        n = c.get("names")
        if n:
            first.setdefault(c.get("name"), n)
        out.append(n or first.get(c.get("name")))
    return out


def parse_team(entry, names_by_cat):
    own, opp = {}, {}
    for i, cat in enumerate(entry.get("categories") or []):
        names = names_by_cat[i] if i < len(names_by_cat) else None
        vals = cat.get("values") or []
        if not names or len(names) != len(vals):
            continue
        if cat.get("name") == "differential":
            continue
        side = opp if str(cat.get("splitId")) == "900" else own
        for k, v in zip(names, vals):
            side.setdefault(k, v)
    return own, opp


def derive(own, opp):
    """Per-game numbers and the stats worked out from them. Returns a dict or None."""
    row = {}
    for col, key in BASE.items():
        row[col] = num(own.get(key))
        if col != "GP":
            row["OPP_" + col] = num(opp.get(key))
    row["FG_PCT"] = num(own.get("fieldGoalPct")) or pct(row["FGM"], row["FGA"])
    row["FG3_PCT"] = num(own.get("threePointFieldGoalPct")) or pct(row["FG3M"], row["FG3A"])
    row["FT_PCT"] = num(own.get("freeThrowPct")) or pct(row["FTM"], row["FTA"])
    row["OPP_FG_PCT"] = num(opp.get("fieldGoalPct")) or pct(row["OPP_FGM"], row["OPP_FGA"])
    row["OPP_FG3_PCT"] = num(opp.get("threePointFieldGoalPct")) or pct(row["OPP_FG3M"], row["OPP_FG3A"])
    need = ["PTS", "FGM", "FGA", "FTA", "OREB", "DREB", "TOV", "OPP_PTS", "OPP_FGM", "OPP_FGA", "OPP_FTA", "OPP_OREB", "OPP_DREB", "OPP_TOV"]
    if not row.get("GP") or any(row.get(k) is None for k in need):
        return None
    t, o = row, row
    poss_t = t["FGA"] + 0.4 * t["FTA"] - 1.07 * (t["OREB"] / (t["OREB"] + o["OPP_DREB"])) * (t["FGA"] - t["FGM"]) + t["TOV"]
    poss_o = o["OPP_FGA"] + 0.4 * o["OPP_FTA"] - 1.07 * (o["OPP_OREB"] / (o["OPP_OREB"] + t["DREB"])) * (o["OPP_FGA"] - o["OPP_FGM"]) + o["OPP_TOV"]
    poss = 0.5 * (poss_t + poss_o)
    if poss <= 0:
        return None
    row["PACE"] = poss
    row["ORTG"] = 100 * t["PTS"] / poss
    row["DRTG"] = 100 * t["OPP_PTS"] / poss
    row["NRTG"] = row["ORTG"] - row["DRTG"]
    row["NET_PTS"] = t["PTS"] - t["OPP_PTS"]
    row["EFG_PCT"] = pct(t["FGM"] + 0.5 * (t["FG3M"] or 0), t["FGA"])
    row["TS_PCT"] = pct(t["PTS"], 2 * (t["FGA"] + 0.44 * t["FTA"]))
    row["TOV_PCT"] = pct(t["TOV"], t["FGA"] + 0.44 * t["FTA"] + t["TOV"])
    row["OREB_PCT"] = pct(t["OREB"], t["OREB"] + o["OPP_DREB"])
    row["DREB_PCT"] = pct(t["DREB"], t["DREB"] + o["OPP_OREB"])
    row["FT_RATE"] = pct(t["FTA"], t["FGA"])
    row["FG3_RATE"] = pct(t["FG3A"], t["FGA"])
    row["AST_TO"] = t["AST"] / t["TOV"] if t.get("AST") is not None and t["TOV"] else None
    row["OPP_EFG_PCT"] = pct(o["OPP_FGM"] + 0.5 * (o["OPP_FG3M"] or 0), o["OPP_FGA"])
    row["OPP_TOV_PCT"] = pct(o["OPP_TOV"], o["OPP_FGA"] + 0.44 * o["OPP_FTA"] + o["OPP_TOV"])
    row["_POSS"] = poss
    return row


def fetch_records(year):
    """{team id: (wins, losses)} from the standings feed. Returns {} if that feed is not usable."""
    data = fs.get_json(f"{STANDINGS}?season={year}&type=0&level=3")
    out = {}
    try:
        def walk(o):
            if isinstance(o, dict):
                ents = (o.get("standings") or {}).get("entries")
                for e in ents or []:
                    st = {s.get("name"): s.get("value") for s in e.get("stats") or []}
                    tid = str((e.get("team") or {}).get("id"))
                    if num(st.get("wins")) is not None and num(st.get("losses")) is not None:
                        out[tid] = (int(st["wins"]), int(st["losses"]))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(data or {})
    except Exception as err:
        print("  standings skipped:", err)
    return out


def fetch_team_season(year):
    """Returns (label, teams) or None if ESPN has no team data for that season."""
    data = fs.get_json(f"{BYTEAM}?season={year}&seasontype=2&limit=50")
    if not data:
        return None
    req = data.get("requestedSeason")
    if isinstance(req, dict) and isinstance(req.get("year"), int) and req["year"] != year:
        return None
    names = category_names(data.get("categories") or [])
    teams = []
    for t in data.get("teams") or []:
        info = t.get("team") or {}
        own, opp = parse_team(t, names)
        row = derive(own, opp)
        if row is None or not info.get("abbreviation"):
            continue
        row["ID"] = str(info.get("id"))
        row["TEAM"] = info.get("abbreviation")
        row["NAME"] = info.get("displayName") or info.get("name") or info["abbreviation"]
        row["SHORT"] = info.get("shortDisplayName") or info.get("name") or info["abbreviation"]
        teams.append(row)
    if len(teams) < 8:
        return None
    rec = fetch_records(year)
    for r in teams:
        if r["ID"] in rec:
            w, l = rec[r["ID"]]
            r["W"], r["L"] = w, l
            r["WIN_PCT"] = 100.0 * w / (w + l) if w + l else None
    label = (req or {}).get("displayName") if isinstance(req, dict) else None
    return (label or f"{year - 1}-{str(year)[2:]}") + " season", teams


def save_team_season(year, label, teams):
    cols = [c for c in ORDER if any(t.get(c) is not None for t in teams)]
    clean = []
    for t in teams:
        r = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items() if v is not None and not k.startswith("_")}
        clean.append(r)
    clean.sort(key=lambda r: -(r.get("NRTG") if r.get("NRTG") is not None else -999))
    write_json(os.path.join(TEAM_DIR, f"{year}.json"), {
        "season": label, "year": year, "updated": datetime.now(timezone.utc).isoformat(),
        "columns": cols, "teams": clean})


# --------------------------------------------------- player offensive / defensive rating
def player_ratings(p, tm, gp_cap=None):
    """Dean Oliver's estimates (the same formulas Basketball-Reference uses). Returns (ORTG, DRTG) or None."""
    g, mpg = num(p.get("GP")), num(p.get("MIN"))
    if not g or not mpg or mpg <= 0:
        return None
    G = tm["GP"]
    # totals: players' per-game numbers times their games, teams' per-game numbers times team games
    def P(k):
        v = num(p.get(k))
        return None if v is None else v * g
    def T(k):
        v = num(tm.get(k))
        return None if v is None else v * G
    MP, TMP = mpg * g, 240.0 * G
    FG, FGA, FT, FTA, TP, PTS, AST, TOV = P("FGM"), P("FGA"), P("FTM"), P("FTA"), P("FG3M"), P("PTS"), P("AST"), P("TOV")
    ORB, STL, BLK, PF = P("OREB"), P("STL"), P("BLK"), P("PF")
    DRB = P("DREB")
    if DRB is None and P("REB") is not None and ORB is not None:
        DRB = P("REB") - ORB
    tFG, tFGA, tFT, tFTA, t3P, tPTS, tAST, tTOV, tORB = T("FGM"), T("FGA"), T("FTM"), T("FTA"), T("FG3M"), T("PTS"), T("AST"), T("TOV"), T("OREB")
    tDRB, tSTL, tBLK, tPF = T("DREB"), T("STL"), T("BLK"), T("PF")
    oFG, oFGA, oFT, oFTA, oPTS, oTOV, oORB = T("OPP_FGM"), T("OPP_FGA"), T("OPP_FTM"), T("OPP_FTA"), T("OPP_PTS"), T("OPP_TOV"), T("OPP_OREB")
    oREB = T("OPP_REB")
    tposs = tm["_POSS"] * G if "_POSS" in tm else tm["PACE"] * G
    out = [None, None]
    need = [FG, FGA, FT, FTA, PTS, AST, TOV, ORB, tFG, tFGA, tFT, tFTA, tPTS, tAST, tTOV, tORB, oREB, tDRB, tposs]
    if all(v is not None for v in need) and FGA > 0 and FTA >= 0 and tFTA > 0 and tFG > FG and tFGA > FGA and MP > 0:
        TP = TP or 0
        t3P = t3P or 0
        share = MP / (TMP / 5)
        qAST = share * (1.14 * ((tAST - AST) / tFG)) + (((tAST / TMP) * MP * 5 - AST) / ((tFG / TMP) * MP * 5 - FG)) * (1 - share)
        FG_part = FG * (1 - 0.5 * ((PTS - FT) / (2 * FGA)) * qAST)
        AST_part = 0.5 * (((tPTS - tFT) - (PTS - FT)) / (2 * (tFGA - FGA))) * AST
        FT_part = (1 - (1 - (FT / FTA if FTA else 0)) ** 2) * 0.4 * FTA
        tScPoss = tFG + (1 - (1 - tFT / tFTA) ** 2) * tFTA * 0.4
        tORBpct = tORB / (tORB + (oREB - oORB)) if oORB is not None else None
        if tORBpct is not None and tScPoss > 0:
            tPlay = tScPoss / (tFGA + tFTA * 0.4 + tTOV)
            wt = ((1 - tORBpct) * tPlay) / ((1 - tORBpct) * tPlay + tORBpct * (1 - tPlay))
            ORB_part = ORB * wt * tPlay
            scale = 1 - (tORB / tScPoss) * wt * tPlay
            ScPoss = (FG_part + AST_part + FT_part) * scale + ORB_part
            FGxPoss = (FGA - FG) * (1 - 1.07 * tORBpct)
            FTxPoss = ((1 - (FT / FTA if FTA else 0)) ** 2) * 0.4 * FTA
            Tot = ScPoss + FGxPoss + FTxPoss + TOV
            PP_FG = 2 * (FG + 0.5 * TP) * (1 - 0.5 * ((PTS - FT) / (2 * FGA)) * qAST)
            PP_AST = 2 * ((tFG - FG + 0.5 * (t3P - TP)) / (tFG - FG)) * 0.5 * (((tPTS - tFT) - (PTS - FT)) / (2 * (tFGA - FGA))) * AST
            PP_ORB = ORB * wt * tPlay * (tPTS / tScPoss)
            PProd = (PP_FG + PP_AST + FT) * scale + PP_ORB
            if Tot > 0:
                out[0] = 100.0 * PProd / Tot
    need_d = [STL, BLK, DRB, PF, oFG, oFGA, oFT, oFTA, oPTS, oTOV, oORB, tDRB, tSTL, tBLK, tPF]
    if all(v is not None for v in need_d) and oFGA > 0 and oFTA > 0 and tposs > 0 and tPF > 0:
        DORpct = oORB / (oORB + tDRB)
        DFGpct = oFG / oFGA
        FMwt = (DFGpct * (1 - DORpct)) / (DFGpct * (1 - DORpct) + (1 - DFGpct) * DORpct)
        Stops1 = STL + BLK * FMwt * (1 - 1.07 * DORpct) + DRB * (1 - FMwt)
        Stops2 = (((oFGA - oFG - tBLK) / TMP) * FMwt * (1 - 1.07 * DORpct) + ((oTOV - tSTL) / TMP)) * MP \
                 + (PF / tPF) * 0.4 * oFTA * (1 - (oFT / oFTA)) ** 2
        Stops = Stops1 + Stops2
        stop_pct = (Stops * TMP) / (tposs * MP)
        tDRtg = 100 * oPTS / tposs
        d_pts = oPTS / (oFG + (1 - (1 - oFT / oFTA) ** 2) * oFTA * 0.4)
        out[1] = tDRtg + 0.2 * (100 * d_pts * (1 - stop_pct) - tDRtg)
    return tuple(out) if out[0] is not None or out[1] is not None else None


def add_player_ratings(path, teams, is_stats_json=False):
    d = read_json(path)
    if not d or not d.get("players"):
        return 0
    by = {t["TEAM"]: t for t in teams}
    hit = miss = 0
    for p in d["players"]:
        for k in ("ORTG", "DRTG", "NRTG"):
            p.pop(k, None)
        tm = by.get(p.get("TEAM"))
        r = None
        if tm:
            try:
                r = player_ratings(p, tm)
            except (ZeroDivisionError, ValueError, OverflowError):
                r = None
        if not r:
            miss += 1
            continue
        hit += 1
        o, df = r
        # keep silly outputs (tiny samples) out of the data
        if o is not None and 20 < o < 250:
            p["ORTG"] = round(o, 2)
        if df is not None and 60 < df < 160:
            p["DRTG"] = round(df, 2)
        if "ORTG" in p and "DRTG" in p:
            p["NRTG"] = round(p["ORTG"] - p["DRTG"], 2)
    cols = list(d.get("columns") or [])
    for c in ("ORTG", "DRTG", "NRTG"):
        if any(c in p for p in d["players"]):
            if c not in cols:
                cols.append(c)
        elif c in cols:
            cols.remove(c)
    d["columns"] = cols
    write_json(path, d)
    return hit, miss


# -------------------------------------------------------------------------- run
def season_years():
    years = set()
    for path in glob.glob(os.path.join(DATA_DIR, "[0-9][0-9][0-9][0-9].json")):
        years.add(int(os.path.basename(path)[:4]))
    cur = read_json("stats.json") or {}
    cy = bc.season_end_year(cur.get("season"))
    if cy:
        years.add(cy)
    return sorted(years), cy


def write_team_index(cy):
    seasons = []
    for path in sorted(glob.glob(os.path.join(TEAM_DIR, "[0-9][0-9][0-9][0-9].json")), reverse=True):
        d = read_json(path)
        if d and d.get("teams"):
            y = d.get("year") or int(os.path.basename(path)[:4])
            seasons.append({"year": y, "label": (d.get("season") or "").replace(" season", ""), "file": f"{TEAM_DIR}/{y}.json", "teams": len(d["teams"])})
    write_json(os.path.join(TEAM_DIR, "index.json"), {
        "updated": datetime.now(timezone.utc).isoformat(), "current": cy, "seasons": seasons})


def commit(msg):
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    sh = lambda *a: subprocess.run(a, capture_output=True, text=True)
    sh("git", "config", "user.name", "stats-bot")
    sh("git", "config", "user.email", "stats-bot@users.noreply.github.com")
    sh("git", "add", "data", "stats.json")
    if sh("git", "diff", "--staged", "--quiet").returncode == 0:
        return
    sh("git", "commit", "-m", msg)
    for _ in range(5):
        sh("git", "pull", "--rebase", "--autostash", "origin", "main")
        if sh("git", "push").returncode == 0:
            return
        time.sleep(6)


def main():
    only_current = "current" in sys.argv[1:]
    years, cy = season_years()
    if only_current and cy:
        years = [cy]
    start = time.time()
    done = 0
    for y in sorted(years, reverse=True):
        if time.time() - start > 40 * 60:
            print("Time budget used up; run again to finish.")
            break
        tpath = os.path.join(TEAM_DIR, f"{y}.json")
        fresh = y == cy or not os.path.exists(tpath)
        if fresh:
            print(f"Team data for season ending {y}...")
            got = fetch_team_season(y)
            if got is None:
                print("  ESPN has no team data for that season.")
                continue
            label, teams = got
            save_team_season(y, label, teams)
            print(f"  saved {len(teams)} teams")
            time.sleep(0.5)
        tdata = read_json(tpath) or {}
        # rebuild raw rows (with possessions) for the rating formulas
        teams = tdata.get("teams") or []
        for t in teams:
            t["_POSS"] = t.get("PACE")
        ppath = "stats.json" if y == cy else os.path.join(DATA_DIR, f"{y}.json")
        if os.path.exists(ppath):
            try:
                res = add_player_ratings(ppath, teams)
                print(f"  ratings added for {res[0]} players ({res[1]} without a matching team)" if res else "  no player file")
            except Exception as err:
                print("  player ratings skipped because of an error:", err)
        done += 1
    write_team_index(cy)
    try:
        bc.main()
    except Exception as err:
        print("Career update skipped because of an error:", err)
    if not only_current:
        commit("Update team stats and ratings")
    print(f"Done. Processed {done} season(s).")


if __name__ == "__main__":
    main()
