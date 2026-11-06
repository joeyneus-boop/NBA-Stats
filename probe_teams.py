"""One-off helper: asks ESPN which team stats exist and saves what it finds in data/probe/.
Nothing on the website uses this. It only helps us build the Teams tab correctly."""
import json
import os
import subprocess

import fetch_stats as fs

OUT = os.path.join("data", "probe")
BASE_SITE = "https://site.web.api.espn.com/apis"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

TESTS = {
    "byteam_2024": f"{BASE_SITE}/common/v3/sports/basketball/nba/statistics/byteam?season=2024&seasontype=2",
    "byteam_2024_limit": f"{BASE_SITE}/common/v3/sports/basketball/nba/statistics/byteam?season=2024&seasontype=2&limit=50&page=1",
    "byteam_1995": f"{BASE_SITE}/common/v3/sports/basketball/nba/statistics/byteam?season=1995&seasontype=2",
    "byteam_current": f"{BASE_SITE}/common/v3/sports/basketball/nba/statistics/byteam?seasontype=2",
    "standings_2024": f"{BASE_SITE}/v2/sports/basketball/nba/standings?season=2024&type=0&level=3",
    "standings_1995": f"{BASE_SITE}/v2/sports/basketball/nba/standings?season=1995&type=0&level=3",
    "core_team_2024": f"{CORE}/seasons/2024/types/2/teams/13/statistics",
    "core_team_1995": f"{CORE}/seasons/1995/types/2/teams/13/statistics",
    "site_team_stats_2024": f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/13/statistics?season=2024",
    "site_teams": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams",
}


def walk(o, found, path=""):
    """Collect every {name, value} pair so the stat names are easy to read."""
    if isinstance(o, dict):
        if "name" in o and ("value" in o or "displayValue" in o):
            found.append(f"{path}/{o.get('name')} = {o.get('value', o.get('displayValue'))}")
        for k, v in o.items():
            walk(v, found, f"{path}/{k}" if not isinstance(v, (dict, list)) or k in ("splits", "categories", "stats") else path)
    elif isinstance(o, list):
        for v in o[:60]:
            walk(v, found, path)


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}
    for name, url in TESTS.items():
        print("Trying", name)
        data = fs.get_json(url, tries=2)
        if data is None:
            report[name] = {"ok": False, "url": url}
            continue
        raw = json.dumps(data, separators=(",", ":"))
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            f.write(raw[:120000])
        found = []
        walk(data, found)
        report[name] = {"ok": True, "url": url, "bytes": len(raw),
                        "top_keys": list(data.keys())[:25] if isinstance(data, dict) else "list",
                        "stat_pairs_seen": len(found), "first_stats": found[:90]}
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(json.dumps({k: (v["ok"], v.get("stat_pairs_seen")) for k, v in report.items()}))
    if os.environ.get("GITHUB_ACTIONS"):
        sh = lambda *a: subprocess.run(a, capture_output=True, text=True)
        sh("git", "config", "user.name", "stats-bot")
        sh("git", "config", "user.email", "stats-bot@users.noreply.github.com")
        sh("git", "add", OUT)
        if sh("git", "diff", "--staged", "--quiet").returncode != 0:
            sh("git", "commit", "-m", "Team data probe results")
            for _ in range(4):
                sh("git", "pull", "--rebase", "--autostash", "origin", "main")
                if sh("git", "push").returncode == 0:
                    break


if __name__ == "__main__":
    main()
