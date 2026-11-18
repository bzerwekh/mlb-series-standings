#!/usr/bin/env python3
"""Build MLB series standings: a series is won or lost on aggregate runs.

Usage:
    python3 build.py              # use cached API data if present
    python3 build.py --refresh    # re-download from statsapi.mlb.com
    python3 build.py --season 2026

Writes standings.html next to this file. Raw API responses are cached in data/.
"""
import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
API = "https://statsapi.mlb.com/api/v1"
PLAYED = {"Final", "Completed Early", "Game Over"}

DIVISION_ORDER = [
    "American League East", "American League Central", "American League West",
    "National League East", "National League Central", "National League West",
]


def fetch(url, cache_name, refresh):
    path = DATA / cache_name
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    DATA.mkdir(exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as r:
        body = r.read()
    path.write_bytes(body)
    return json.loads(body)


def load(season, refresh):
    teams = fetch(f"{API}/teams?sportId=1&season={season}", f"teams_{season}.json", refresh)
    sched = fetch(
        f"{API}/schedule?sportId=1&season={season}&gameType=R&hydrate=linescore",
        f"schedule_{season}.json", refresh)
    return teams["teams"], sched


def home_skipped_last_half(game):
    """True if the home team won without batting in its final half-inning."""
    innings = (game.get("linescore") or {}).get("innings") or []
    if not innings or innings[-1].get("num", 0) < 9:
        return False  # rain-shortened games don't count as "didn't need to bat in the 9th"
    return "runs" not in innings[-1].get("home", {})


def parse_games(sched):
    """Return played games (deduped by gamePk) and a list of unplayed postponed games."""
    games, postponed = {}, {}
    for d in sched["dates"]:
        for g in d["games"]:
            state = g["status"]["detailedState"]
            pk = g["gamePk"]
            if state in PLAYED:
                a, h = g["teams"]["away"], g["teams"]["home"]
                games[pk] = {
                    "pk": pk,
                    "date": g["officialDate"],
                    # Makeup games sort back into the slot they were originally scheduled.
                    "orig": g.get("rescheduledFromDate") or g["officialDate"],
                    "time": g.get("gameDate", ""),
                    "num": g.get("gameNumber", 1),
                    "away": a["team"]["id"], "home": h["team"]["id"],
                    "ar": a.get("score", 0), "hr": h.get("score", 0),
                    "makeup": bool(g.get("rescheduledFromDate")),
                    "state": state,
                    "no_bat": False,
                }
                gm = games[pk]
                gm["no_bat"] = gm["hr"] > gm["ar"] and home_skipped_last_half(g)
            elif state == "Postponed":
                postponed[pk] = g
    # A postponed slot that was later played shares the gamePk; only keep truly unplayed ones.
    pending = {pk: g for pk, g in postponed.items() if pk not in games}
    return games, pending


def build_series(games, team_ids):
    """Group each team's games into series and confirm both teams agree on every grouping."""
    by_team = defaultdict(list)
    for g in games.values():
        by_team[g["away"]].append(g)
        by_team[g["home"]].append(g)

    series = {}  # frozenset(pks) -> list of games
    claims = defaultdict(int)
    for tid in team_ids:
        sched = sorted(by_team[tid], key=lambda g: (g["orig"], g["time"], g["num"]))
        run = []
        for g in sched:
            opp = g["home"] if g["away"] == tid else g["away"]
            if run:
                prev = run[-1]
                prev_opp = prev["home"] if prev["away"] == tid else prev["away"]
                # New series when the opponent changes, or the same pair switches ballparks.
                if opp != prev_opp or g["home"] != prev["home"]:
                    key = frozenset(x["pk"] for x in run)
                    series[key] = run
                    claims[key] += 1
                    run = []
            run.append(g)
        if run:
            key = frozenset(x["pk"] for x in run)
            series[key] = run
            claims[key] += 1

    problems = [sorted(k) for k, n in claims.items() if n != 2]
    return list(series.values()), problems


def score_series(run):
    first = run[0]
    home, away = first["home"], first["away"]
    hr = sum(g["hr"] for g in run)
    ar = sum(g["ar"] for g in run)
    if hr > ar:
        winner, loser = home, away
    elif ar > hr:
        winner, loser = away, home
    else:
        winner = loser = None
    # Flag: the series loser won a home game in this series without batting in the 9th.
    flagged = loser is not None and any(g["no_bat"] and g["home"] == loser for g in run)
    return {
        "home": home, "away": away, "hr": hr, "ar": ar,
        "winner": winner, "loser": loser, "no_bat_loss": flagged,
        # Window of originally scheduled dates (a makeup played in August still dates to May).
        "start": min(g["orig"] for g in run),
        "end": max(g["orig"] for g in run),
        "games": [
            {"date": g["date"], "orig": g["orig"], "ar": g["ar"], "hr": g["hr"],
             "makeup": g["makeup"], "no_bat": g["no_bat"], "state": g["state"]}
            for g in sorted(run, key=lambda g: (g["date"], g["time"], g["num"]))
        ],
    }


def pct(w, l, t):
    n = w + l + t
    return (w + 0.5 * t) / n if n else 0.0


def standings(teams, results):
    rec = {t["id"]: {"W": 0, "L": 0, "T": 0, "RF": 0, "RA": 0, "NB": 0, "HOLD": 0} for t in teams}
    h2h = defaultdict(lambda: {"W": 0, "L": 0, "T": 0})
    for s in results:
        if s["on_hold"]:
            rec[s["home"]]["HOLD"] += 1
            rec[s["away"]]["HOLD"] += 1
            continue
        for me, opp, rf, ra in ((s["home"], s["away"], s["hr"], s["ar"]),
                                (s["away"], s["home"], s["ar"], s["hr"])):
            r = rec[me]
            r["RF"] += rf
            r["RA"] += ra
            key = "W" if s["winner"] == me else "L" if s["loser"] == me else "T"
            r[key] += 1
            h2h[(me, opp)][key] += 1
        if s["no_bat_loss"]:
            rec[s["loser"]]["NB"] += 1

    by_id = {t["id"]: t for t in teams}
    divisions = {}
    for name in DIVISION_ORDER:
        ids = [t["id"] for t in teams if t["division"]["name"] == name]

        def h2h_pct(tid, group):
            w = l = t = 0
            for o in group:
                if o != tid:
                    x = h2h[(tid, o)]
                    w, l, t = w + x["W"], l + x["L"], t + x["T"]
            return pct(w, l, t)

        # Sort by Pct, then head-to-head among teams tied on Pct, then run differential.
        base = sorted(ids, key=lambda i: -pct(rec[i]["W"], rec[i]["L"], rec[i]["T"]))
        ordered, i = [], 0
        while i < len(base):
            p = pct(rec[base[i]]["W"], rec[base[i]]["L"], rec[base[i]]["T"])
            group = [x for x in base[i:] if abs(pct(rec[x]["W"], rec[x]["L"], rec[x]["T"]) - p) < 1e-9]
            group.sort(key=lambda x: (-h2h_pct(x, group), -(rec[x]["RF"] - rec[x]["RA"])))
            ordered += group
            i += len(group)

        lead = rec[ordered[0]]
        lw, ll = lead["W"] + 0.5 * lead["T"], lead["L"] + 0.5 * lead["T"]
        rows = []
        for tid in ordered:
            r = rec[tid]
            w, l = r["W"] + 0.5 * r["T"], r["L"] + 0.5 * r["T"]
            rows.append({
                "id": tid, "abbr": by_id[tid]["abbreviation"], "name": by_id[tid]["name"],
                **r, "Pct": pct(r["W"], r["L"], r["T"]), "SB": ((lw - w) + (l - ll)) / 2,
                "Diff": r["RF"] - r["RA"],
            })
        divisions[name] = rows
    return rec, h2h, divisions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    teams, sched = load(args.season, args.refresh)
    team_ids = [t["id"] for t in teams]
    games, pending = parse_games(sched)
    runs, problems = build_series(games, team_ids)
    if problems:
        sys.exit(f"Series grouping mismatch between teams for {len(problems)} groups: {problems[:5]}")

    results = [score_series(r) for r in runs]
    # A series is on hold if a postponed, still-unplayed game between the pair was
    # originally scheduled within a day of the series window.
    pending_by_pair = defaultdict(list)
    for g in pending.values():
        pair = frozenset((g["teams"]["away"]["team"]["id"], g["teams"]["home"]["team"]["id"]))
        pending_by_pair[pair].append(date.fromisoformat(g["officialDate"]))
    for s in results:
        lo = date.fromisoformat(s["start"]) - timedelta(days=1)
        hi = date.fromisoformat(s["end"]) + timedelta(days=1)
        s["on_hold"] = any(lo <= d <= hi for d in pending_by_pair.get(frozenset((s["home"], s["away"])), []))

    rec, h2h, divisions = standings(teams, results)

    # Validation: every played game in exactly one series; wins match losses league-wide.
    counted = [g for s in runs for g in s]
    assert len(counted) == len({g["pk"] for g in counted}) == len(games), "game assigned twice or missed"
    tw = sum(r["W"] for r in rec.values())
    tl = sum(r["L"] for r in rec.values())
    tt = sum(r["T"] for r in rec.values())
    assert tw == tl and tt % 2 == 0, f"W/L mismatch: {tw} W vs {tl} L, {tt} T"

    by_id = {t["id"]: t for t in teams}
    payload = {
        "season": args.season,
        "teams": {t["id"]: {"abbr": t["abbreviation"], "name": t["name"],
                            "league": t["league"]["name"], "division": t["division"]["name"]}
                  for t in teams},
        "divisions": divisions,
        "division_order": DIVISION_ORDER,
        "series": [{**s, "home_abbr": by_id[s["home"]]["abbreviation"],
                    "away_abbr": by_id[s["away"]]["abbreviation"]} for s in results],
        "h2h": {f"{a}-{b}": v for (a, b), v in h2h.items()},
        "stats": {"games": len(games), "series": len(results), "ties": tt // 2,
                  "on_hold": sum(1 for s in results if s["on_hold"]),
                  "flagged": sum(1 for s in results if s["no_bat_loss"]),
                  "makeups_detached": sum(1 for g in games.values()
                                          if g["makeup"] and g["date"] != g["orig"])},
    }
    html = (ROOT / "template.html").read_text().replace(
        "__DATA__", json.dumps(payload, separators=(",", ":")))
    out = ROOT / "standings.html"
    out.write_text(html)
    st = payload["stats"]
    print(f"{st['games']} games -> {st['series']} series, {st['ties']} tied, "
          f"{st['flagged']} flagged no-bat losses, {st['on_hold']} on hold")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
