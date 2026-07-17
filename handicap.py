#!/usr/bin/env python3
"""
MLB game handicapper — uses Handigraphs CSV exports + MLB Stats API.

Usage:
  python handicap.py                    # today's games
  python handicap.py --date tomorrow    # tomorrow's games
  python handicap.py --date 2026-06-24  # specific date
  python handicap.py --game NYY         # single team only
  python handicap.py --refresh          # re-download data first
  python handicap.py --no-mlb           # skip MLB API (faster, no pitcher history)
  python handicap.py --no-weather       # skip weather lookup
  python handicap.py --no-color         # plain text output
"""

import argparse
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

_ET = timezone(timedelta(hours=-4))

from teams import _MLB_MAP, to_mlb, ODDS_TEAM
from loaders import (
    load_starters, load_team_stats, load_bullpen, load_ballpark_weather,
    load_odds_meta, load_pitcher_props,
)
from odds import load_odds, get_game_odds, pick_odds_by_time
from mlb_api import (
    HAS_REQUESTS,
    get_mlb_schedule, get_bullpen_stress,
    get_recent_starts, get_team_schedule, get_weather,
)
from analysis import analyze_game, build_games, validate_pitchers
import render_terminal
from render_html import render_html_page
from suggestions import generate_suggestions

# Reverse of _MLB_MAP: MLB API abbreviations → Handigraphs codes
_MLB_TO_HG = {v: k for k, v in _MLB_MAP.items()}


def main():
    ap = argparse.ArgumentParser(description="MLB game handicapper")
    ap.add_argument("--date", default="today",
                    help="today (default), tomorrow, or YYYY-MM-DD")
    ap.add_argument("--data-dir", default="./data",
                    help="Directory containing Handigraphs CSV files")
    ap.add_argument("--refresh", action="store_true",
                    help="Download fresh data before analysis")
    ap.add_argument("--game", metavar="TEAM",
                    help="Show only games involving this team (e.g. NYY)")
    ap.add_argument("--no-mlb", action="store_true",
                    help="Skip MLB API calls (no pitcher history / home-away context)")
    ap.add_argument("--no-weather", action="store_true",
                    help="Skip weather lookup")
    ap.add_argument("--no-color", action="store_true",
                    help="Plain text output (no ANSI colors)")
    ap.add_argument("--html", action="store_true",
                    help="Output a self-contained HTML page to stdout")
    ap.add_argument("--suggestions-only", action="store_true",
                    help="Generate and cache AI suggestions (no HTML output); run before --html")
    args = ap.parse_args()

    if args.no_color or args.html or args.suggestions_only:
        render_terminal.use_color = False

    # In HTML mode route status messages to stderr so they don't corrupt the HTML
    _log = (lambda msg: print(msg, file=sys.stderr)) if args.html else print

    # Resolve date
    today_d = datetime.now(_ET).date()
    if args.date == "today":
        target_date, slot = today_d, "today"
    elif args.date == "tomorrow":
        target_date, slot = today_d + timedelta(days=1), "tomorrow"
    else:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
            slot = "today"
        except ValueError:
            sys.exit(f"ERROR: Invalid date '{args.date}'. Use today, tomorrow, or YYYY-MM-DD.")

    data_dir = Path(args.data_dir)

    # Optionally download fresh data
    if args.refresh:
        from download import download_all
        data_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Downloading data for {target_date}...")
        if not download_all(target_date, data_dir, slot):
            _log("Download failed or not configured. Falling back to existing files.")

    if not data_dir.exists():
        sys.exit(
            f"ERROR: Data directory '{data_dir}' does not exist.\n"
            f"Create it and place your CSV files there, or run with --refresh."
        )

    # Load data
    starters   = load_starters(data_dir, target_date)
    rhp, lhp   = load_team_stats(data_dir, target_date)
    bp         = load_bullpen(data_dir, target_date)
    ballpark_wx = {} if args.no_weather else load_ballpark_weather(data_dir, target_date)

    # MLB schedule — fetch first so we can use it as the authoritative game list.
    # Handigraphs starters at early-morning runs may only cover a subset of today's games
    # (starters not yet announced) or include yesterday's starters as stale data.
    mlb_schedule: dict = {}
    bp_stress:   dict = {}
    if not args.no_mlb and HAS_REQUESTS:
        _log("Fetching MLB schedule...")
        mlb_schedule = get_mlb_schedule(target_date)
        _log(f"  {len(mlb_schedule)} games found")
        all_team_ids = {
            tid
            for info in mlb_schedule.values()
            for tid in (info.get("home_mlb_id"), info.get("away_mlb_id"))
            if tid is not None
        }
        _log("Fetching bullpen stress (past 2 days)...")
        bp_stress = get_bullpen_stress(all_team_ids, target_date, data_dir)
        _log(f"  {len(bp_stress)} teams")

    games = build_games(starters)

    # Supplement with any MLB schedule games not yet present in Handigraphs starters.
    if mlb_schedule:
        covered = {
            (frozenset([to_mlb(p1["Team"]), to_mlb(p2["Team"])]), p1.get("game_number") or 1)
            for p1, p2 in games
        }
        starters_by_team_game = {
            (r.get("Team", ""), r.get("game_number") or 1): r for r in starters
        }
        for sched_key, sched_info in mlb_schedule.items():
            if sched_key in covered:
                continue
            gn = sched_info.get("game_number") or 1
            away_hg = _MLB_TO_HG.get(sched_info["away"], sched_info["away"])
            home_hg = _MLB_TO_HG.get(sched_info["home"], sched_info["home"])
            away_row = dict(starters_by_team_game.get((away_hg, gn)) or {
                "Name": sched_info.get("away_pname") or "TBD",
                "Team": away_hg, "Opponent": home_hg,
                "mlbam_id": sched_info.get("away_pid"),
            })
            home_row = dict(starters_by_team_game.get((home_hg, gn)) or {
                "Name": sched_info.get("home_pname") or "TBD",
                "Team": home_hg, "Opponent": away_hg,
                "mlbam_id": sched_info.get("home_pid"),
            })
            away_row.setdefault("Opponent", home_hg)
            home_row.setdefault("Opponent", away_hg)
            away_row.setdefault("game_number", gn)
            home_row.setdefault("game_number", gn)
            games.append((away_row, home_row))

    if not games:
        if args.html:
            generated_at = datetime.now(timezone.utc).isoformat()
            print(render_html_page([], target_date, generated_at, slot=slot))
            return
        sys.exit("No games found. Check your data directory and date.")

    # Filter by team
    if args.game:
        team_filter = args.game.upper()
        games = [(p1, p2) for p1, p2 in games
                 if team_filter in (p1.get("Team", ""), p2.get("Team", ""))]
        if not games:
            sys.exit(f"No games found for '{team_filter}'.")

    if not args.html:
        print(render_terminal.bold(f"\n{'━'*64}"))
        print(render_terminal.bold(f"  MLB Handicap — {target_date.strftime('%A, %B %d %Y')}"))
        print(render_terminal.bold(f"{'━'*64}"))

    odds_data  = load_odds(data_dir, target_date)
    _log(f"Odds: {len(odds_data)} games loaded" if odds_data else "Odds: no file found")
    odds_at    = load_odds_meta(data_dir, target_date)
    props_data = load_pitcher_props(data_dir, target_date)
    _log(f"Props: {len(props_data)} games loaded" if props_data else "Props: no file found")

    game_data: list[dict] = []
    for p1, p2 in games:
        t1_mlb = to_mlb(p1.get("Team", ""))
        t2_mlb = to_mlb(p2.get("Team", ""))
        gn  = p1.get("game_number") or p2.get("game_number") or 1
        key = (frozenset([t1_mlb, t2_mlb]), gn)

        mlb_info = mlb_schedule.get(key, {})

        # Skip games not on today's MLB schedule (catches stale Handigraphs starters)
        if mlb_schedule and not mlb_info:
            _log(f"  Skipping {p1.get('Team','')} @ {p2.get('Team','')}: not on today's MLB schedule")
            continue

        # Validate pitcher IDs match MLB probable starters (prevents yesterday's pitcher showing)
        if mlb_info:
            p1, p2 = validate_pitchers(p1, p2, mlb_info)

        if not args.no_mlb and HAS_REQUESTS:
            for p in (p1, p2):
                pid  = p.get("mlbam_id")
                team = p.get("Team", "")
                if pid and team:
                    mlb_info[f"history_{team}"] = get_recent_starts(int(pid))
            away_id = mlb_info.get("away_mlb_id")
            home_id = mlb_info.get("home_mlb_id")
            if away_id:
                mlb_info["away_record"] = get_team_schedule(int(away_id), target_date.year)
            if home_id:
                mlb_info["home_record"] = get_team_schedule(int(home_id), target_date.year)
            if bp_stress:
                if away_id and int(away_id) in bp_stress:
                    mlb_info["away_bp_stress"] = bp_stress[int(away_id)]
                if home_id and int(home_id) in bp_stress:
                    mlb_info["home_bp_stress"] = bp_stress[int(home_id)]

        # Ballpark weather keyed by raw team codes (Handigraphs starters JSON)
        t1_raw = p1.get("Team", "")
        t2_raw = p2.get("Team", "")
        wx = ballpark_wx.get((frozenset([t1_raw, t2_raw]), gn), {})
        # Fallback to Open-Meteo if Handigraphs weather file wasn't downloaded
        if not wx and not args.no_weather and HAS_REQUESTS:
            home_t = mlb_info.get("home", t2_raw)
            wx = get_weather(home_t, target_date)

        if args.html or args.suggestions_only:
            g = analyze_game(p1, p2, rhp, lhp, bp, mlb_info, wx, target_date)
            # MLB's scheduled start time disambiguates doubleheader legs when matching
            # against the Odds API, which has no game-number field of its own.
            time_hint = mlb_info.get("game_date", "")
            g["odds"] = get_game_odds(odds_data, g["away"], g["home"],
                                      g["away_sp"]["name"], g["home_sp"]["name"],
                                      props_data, game_time_utc=time_hint)
            # Add commence_time from odds for AI filtering and picks display
            away_full = ODDS_TEAM.get(g["away"], "")
            home_full = ODDS_TEAM.get(g["home"], "")
            raw_games = (odds_data.get((away_full, home_full))
                        or odds_data.get((home_full, away_full)) or [])
            raw_game = pick_odds_by_time(raw_games, time_hint) or {}
            g["game_time_utc"] = raw_game.get("commence_time", "")
            game_data.append(g)
        else:
            render_terminal.print_game(p1, p2, rhp, lhp, bp, mlb_info, wx)

    if args.suggestions_only:
        generate_suggestions(game_data, data_dir, target_date)
    elif args.html:
        from datetime import timezone as _tz
        generated_at = datetime.now(_tz.utc).isoformat()
        suggestions = generate_suggestions(game_data, data_dir, target_date)
        try:
            from picks import load_all_picks as _lap
            picks_dir = Path("./picks")
            all_picks = _lap(picks_dir, target_date)
        except Exception:
            all_picks = []
        print(render_html_page(game_data, target_date, generated_at, odds_at,
                               suggestions, all_picks, slot=slot))
    else:
        print()


if __name__ == "__main__":
    main()
