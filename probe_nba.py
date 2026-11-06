"""One-off helper: checks whether GitHub's servers can reach NBA.com's tracking stats
(defense: opponent FG% when guarded, shots faced; hustle: deflections, contested shots, loose balls).
Saves what it finds in data/probe_nba/. Nothing on the website uses this."""
import json
import os
import subprocess
import time

OUT = os.path.join("data", "probe_nba")
SEASON = "2025-26"

HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}


def save(name, obj):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name + ".json"), "w") as f:
        json.dump(obj, f, indent=1, default=str)


def summarize(frame):
    return {"columns": list(frame.columns), "rows": len(frame), "first_rows": json.loads(frame.head(5).to_json(orient="records"))}


def main():
    report = {}
    try:
        from nba_api.stats.endpoints import leaguedashptdefend, leaguehustlestatsplayer, leaguedashplayerstats
    except Exception as err:
        report["import_error"] = str(err)
        save("report", report)
        return
    tests = {
        "defense_overall": lambda: leaguedashptdefend.LeagueDashPtDefend(
            defense_category="Overall", season=SEASON, season_type_all_star="Regular Season", per_mode_simple="PerGame",
            headers=HEADERS, timeout=45).get_data_frames()[0],
        "hustle_player": lambda: leaguehustlestatsplayer.LeagueHustleStatsPlayer(
            season=SEASON, season_type_all_star="Regular Season", per_mode_time="PerGame",
            headers=HEADERS, timeout=45).get_data_frames()[0],
        "advanced_player": lambda: leaguedashplayerstats.LeagueDashPlayerStats(
            season=SEASON, season_type_all_star="Regular Season", measure_type_detailed_defense="Defense",
            per_mode_detailed="PerGame", headers=HEADERS, timeout=45).get_data_frames()[0],
    }
    for name, fn in tests.items():
        started = time.time()
        for attempt in range(2):
            try:
                frame = fn()
                report[name] = {"ok": True, "seconds": round(time.time() - started, 1), **summarize(frame)}
                break
            except Exception as err:
                report[name] = {"ok": False, "seconds": round(time.time() - started, 1), "error": f"{type(err).__name__}: {str(err)[:300]}"}
                time.sleep(3)
    save("report", report)
    print(json.dumps({k: v.get("ok") if isinstance(v, dict) else v for k, v in report.items()}))
    if os.environ.get("GITHUB_ACTIONS"):
        sh = lambda *a: subprocess.run(a, capture_output=True, text=True)
        sh("git", "config", "user.name", "stats-bot")
        sh("git", "config", "user.email", "stats-bot@users.noreply.github.com")
        sh("git", "add", OUT)
        if sh("git", "diff", "--staged", "--quiet").returncode != 0:
            sh("git", "commit", "-m", "NBA.com data probe results")
            for _ in range(4):
                sh("git", "pull", "--rebase", "--autostash", "origin", "main")
                if sh("git", "push").returncode == 0:
                    break


if __name__ == "__main__":
    main()
