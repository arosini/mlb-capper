#!/usr/bin/env python3
"""
Read-only inspection CLI for ad hoc checks — no writes, no paid API calls.

Usage:
  python3 scripts/inspect.py schedule [DATE]
  python3 scripts/inspect.py starters TEAM [DATE]
  python3 scripts/inspect.py team-stats TEAM [DATE]
  python3 scripts/inspect.py bullpen TEAM [DATE]
  python3 scripts/inspect.py weather TEAM1 TEAM2 [DATE]
  python3 scripts/inspect.py odds AWAY HOME [DATE]
  python3 scripts/inspect.py props AWAY HOME [DATE]
  python3 scripts/inspect.py picks [DATE]
  python3 scripts/inspect.py history [DATE]
  python3 scripts/inspect.py suggestions [DATE]

DATE is "today" (default), "tomorrow", or YYYY-MM-DD.
"""

import argparse
import json
import sys
from datetime import date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams import ODDS_TEAM, to_stats
from loaders import (
    load_starters, load_team_stats, load_bullpen, load_ballpark_weather,
    load_pitcher_props,
)
from odds import load_odds
from mlb_api import get_mlb_schedule

_ET = timezone(timedelta(hours=-4))
_ROOT = Path(__file__).resolve().parent.parent


def _parse_date(s: str) -> date:
    if s == "today":
        from datetime import datetime
        return datetime.now(_ET).date()
    if s == "tomorrow":
        from datetime import datetime
        return datetime.now(_ET).date() + timedelta(days=1)
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"ERROR: Invalid date '{s}'. Use today, tomorrow, or YYYY-MM-DD.")


def cmd_schedule(args):
    sched = get_mlb_schedule(_parse_date(args.date))
    print(f"{len(sched)} games on {args.date}")
    for (teams_fs, gn), g in sorted(sched.items(), key=lambda kv: kv[1]["game_date"]):
        print(f"  {g['away']} @ {g['home']} (g{gn})  {g['venue']}  "
              f"SP: {g['away_pname'] or '?'} / {g['home_pname'] or '?'}")


def cmd_starters(args):
    rows = load_starters(_ROOT / "data", _parse_date(args.date))
    matches = [r for r in rows if r.get("Team") == args.team]
    if not matches:
        print(f"No starters found for {args.team} on {args.date}")
        return
    for r in matches:
        print(json.dumps(r, indent=2))


def cmd_team_stats(args):
    rhp, lhp = load_team_stats(_ROOT / "data", _parse_date(args.date))
    team = to_stats(args.team)
    print("vs RHP:", json.dumps(rhp.get(team), indent=2))
    print("vs LHP:", json.dumps(lhp.get(team), indent=2))


def cmd_bullpen(args):
    bp = load_bullpen(_ROOT / "data", _parse_date(args.date))
    team = to_stats(args.team)
    print(json.dumps(bp.get(team), indent=2))


def cmd_weather(args):
    wx = load_ballpark_weather(_ROOT / "data", _parse_date(args.date))
    match = None
    for (fs, gn), g in wx.items():
        if fs == frozenset([args.team1, args.team2]):
            match = g
            break
    print(json.dumps(match, indent=2) if match else "No weather entry found")


def cmd_odds(args):
    odds = load_odds(_ROOT / "data", _parse_date(args.date))
    away_name = ODDS_TEAM.get(args.away, args.away)
    home_name = ODDS_TEAM.get(args.home, args.home)
    games = odds.get((away_name, home_name), [])
    print(f"{len(games)} event(s) for {args.away} @ {args.home}")
    for g in games:
        print(json.dumps(g, indent=2))


def cmd_props(args):
    props = load_pitcher_props(_ROOT / "data", _parse_date(args.date))
    away_name = ODDS_TEAM.get(args.away, args.away)
    home_name = ODDS_TEAM.get(args.home, args.home)
    events = props.get((away_name, home_name), [])
    print(f"{len(events)} event(s) for {args.away} @ {args.home}")
    for e in events:
        print(json.dumps(e, indent=2))


def _print_log(path: Path):
    if not path.exists():
        print(f"No file at {path}")
        return
    data = json.loads(path.read_text())
    print(f"{len(data)} record(s) in {path.name}")
    for r in data:
        print(json.dumps(r, indent=2))


def cmd_picks(args):
    _print_log(_ROOT / "picks" / f"{args.date}.json")


def cmd_history(args):
    _print_log(_ROOT / "history" / f"{args.date}.json")


def cmd_suggestions(args):
    matches = sorted((_ROOT / "data").glob(f"suggestions_{args.date}.json"))
    if not matches:
        print(f"No suggestions file for {args.date}")
        return
    data = json.loads(matches[0].read_text())
    for p in data.get("picks", []):
        print(json.dumps(p, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("schedule"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_schedule)
    p = sub.add_parser("starters"); p.add_argument("team"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_starters)
    p = sub.add_parser("team-stats"); p.add_argument("team"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_team_stats)
    p = sub.add_parser("bullpen"); p.add_argument("team"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_bullpen)
    p = sub.add_parser("weather"); p.add_argument("team1"); p.add_argument("team2"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_weather)
    p = sub.add_parser("odds"); p.add_argument("away"); p.add_argument("home"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_odds)
    p = sub.add_parser("props"); p.add_argument("away"); p.add_argument("home"); p.add_argument("date", nargs="?", default="today"); p.set_defaults(fn=cmd_props)
    p = sub.add_parser("picks"); p.add_argument("date", nargs="?", default=str(_parse_date("today"))); p.set_defaults(fn=cmd_picks)
    p = sub.add_parser("history"); p.add_argument("date", nargs="?", default=str(_parse_date("today"))); p.set_defaults(fn=cmd_history)
    p = sub.add_parser("suggestions"); p.add_argument("date", nargs="?", default=str(_parse_date("today"))); p.set_defaults(fn=cmd_suggestions)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
