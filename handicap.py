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
import json
import sys
from datetime import timedelta, datetime, timezone
from pathlib import Path

from season import ET as _ET

from teams import _MLB_MAP, to_mlb, ODDS_TEAM
from loaders import (
    load_starters, load_team_stats, load_bullpen, load_ballpark_weather,
    load_odds_meta, load_pitcher_props, load_history_games,
)
from odds import load_odds, get_game_odds, pick_odds_by_time
from mlb_api import (
    HAS_REQUESTS,
    get_mlb_schedule, get_bullpen_stress, get_pitch_hands,
    get_recent_starts, get_team_schedule, get_weather,
    get_lineups, get_bat_sides,
)
from analysis import (
    analyze_game, build_games, resolve_pitchers, pitcher_ids_to_check, ou_trends,
)
from venues import home_venue_ids, coords_are_sane, roof_kind, venue_geo
import render_terminal
from render_html import render_html_page
from suggestions import generate_suggestions

# Reverse of _MLB_MAP: MLB API abbreviations → Handigraphs codes
_MLB_TO_HG = {v: k for k, v in _MLB_MAP.items()}


def _game_log(pid: str) -> list:
    """get_recent_starts() for a player id that arrived as a string, or [] if it
    isn't one. Handigraphs nulls the id out for pitchers it has no data on."""
    try:
        return get_recent_starts(int(pid))
    except (TypeError, ValueError):
        return []


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
    ap.add_argument("--dump-cards", metavar="PATH",
                    help="Write the serialized AI data cards to PATH as JSON and exit. "
                         "Makes no API call — this is the input to scripts/measure_card.py")
    args = ap.parse_args()

    if args.no_color or args.html or args.suggestions_only or args.dump_cards:
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
    # Primary offense window (last 6 vs hand) plus the longer comparison window.
    rhp, lhp, rhp_ctx, lhp_ctx, all6, all12 = load_team_stats(data_dir, target_date)
    bp         = load_bullpen(data_dir, target_date)
    ballpark_wx = {} if args.no_weather else load_ballpark_weather(data_dir, target_date)

    # MLB schedule — fetch first so we can use it as the authoritative game list.
    # Handigraphs starters at early-morning runs may only cover a subset of today's games
    # (starters not yet announced) or include yesterday's starters as stale data.
    mlb_schedule: dict = {}
    bp_stress:   dict = {}
    home_venues: dict = {}
    pitch_hands: dict = {}
    lineups:     dict = {}
    bat_sides:   dict = {}
    # Game logs, keyed by MLB player id, shared across every game on the slate. A
    # doubleheader or a mid-slate trade can put the same pitcher in two lookups.
    sp_logs:     dict = {}
    if not args.no_mlb and HAS_REQUESTS:
        home_venues = home_venue_ids(data_dir, target_date.year)
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
        # One call for the whole slate. A probable with no Handigraphs row has no hand
        # anywhere else, and no hand means the opposing offense card has nothing to
        # split on and renders empty.
        pitch_hands = get_pitch_hands(
            pid
            for info in mlb_schedule.values()
            for pid in (info.get("home_pid"), info.get("away_pid"))
        )
        _log(f"Pitch hands: {len(pitch_hands)} probables")
        # Posted an hour or two before first pitch, so the early runs get nothing and
        # the card says so rather than going silent. Two calls for the whole slate.
        lineups = get_lineups(target_date)
        if lineups:
            bat_sides = get_bat_sides(
                pl["id"] for lu in lineups.values()
                for side in ("away", "home") for pl in lu[side]
            )
            _log(f"Lineups: {len(lineups)} posted, {len(bat_sides)} bat sides")
        else:
            _log("Lineups: none posted yet")

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
    # Graded odds history backs the over/under trend lines; lives alongside the
    # code (git-tracked), not in data/.
    hist_games = load_history_games(Path(__file__).parent / "history", target_date)
    _log(f"History: {len(hist_games)} graded games loaded" if hist_games
         else "History: no graded games found")

    game_data: list[dict] = []
    for p1, p2 in games:
        t1_mlb = to_mlb(p1.get("Team", ""))
        t2_mlb = to_mlb(p2.get("Team", ""))
        gn  = p1.get("game_number") or p2.get("game_number") or 1
        key = (frozenset([t1_mlb, t2_mlb]), gn)

        mlb_info = mlb_schedule.get(key, {})
        lu = lineups.get(key)
        if lu:
            mlb_info["lineups"] = lu
            mlb_info["bat_sides"] = bat_sides

        # Skip games not on today's MLB schedule (catches stale Handigraphs starters)
        if mlb_schedule and not mlb_info:
            _log(f"  Skipping {p1.get('Team','')} @ {p2.get('Team','')}: not on today's MLB schedule")
            continue

        # Reconcile the Handigraphs starters feed against MLB's probables. An opener, a
        # bullpen game and a genuinely stale row all look identical from the outside —
        # "the two sources name different pitchers" — and only recent workload tells
        # them apart, so the game logs are fetched BEFORE the resolver, not after.
        if mlb_info:
            if not args.no_mlb and HAS_REQUESTS:
                for pid in pitcher_ids_to_check(p1, p2, mlb_info):
                    if pid not in sp_logs:
                        sp_logs[pid] = _game_log(pid)
            p1, p2 = resolve_pitchers(p1, p2, mlb_info, logs=sp_logs,
                                      hands=pitch_hands, today=target_date)

        if not args.no_mlb and HAS_REQUESTS:
            for p in (p1, p2):
                pid  = str(p.get("mlbam_id") or "")
                team = p.get("Team", "")
                if pid and team:
                    if pid not in sp_logs:
                        sp_logs[pid] = _game_log(pid)
                    mlb_info[f"history_{team}"] = sp_logs[pid]
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

        # Is this game at the home club's own park? A mismatch between the game's
        # venue and the club's registered home venue IS a neutral site — Field of
        # Dreams, Williamsport, Mexico City, Tokyo, or a season-long relocation.
        # Anything keyed on the team code (park factor, the old weather lookup) is
        # describing a different ballpark on those days.
        venue_id  = mlb_info.get("venue_id")
        home_mlb  = mlb_info.get("home_mlb_id")
        expected  = home_venues.get(home_mlb) if home_venues else None
        neutral   = bool(venue_id and expected and venue_id != expected)
        mlb_info["neutral_site"] = neutral

        # Fallback to Open-Meteo when the Handigraphs file is absent — which is the
        # normal case for the tomorrow page, since ballpark_weather is not date-aware
        # upstream and is skipped for that slot.
        if not wx and not args.no_weather and HAS_REQUESTS:
            lat = mlb_info.get("venue_lat")
            lon = mlb_info.get("venue_lon")
            azimuth = mlb_info.get("venue_azimuth")
            elev    = mlb_info.get("venue_elevation")
            # The schedule's inline venue hydrate is the cheap path, but it does not
            # always carry location. Fall back to the dedicated venue endpoint (cached
            # per venue, so at most one call per park per season) before giving up.
            if not coords_are_sane(lat, lon, venue_id) and venue_id:
                geo = venue_geo(venue_id, data_dir)
                if geo:
                    lat, lon = geo["lat"], geo["lon"]
                    azimuth = azimuth if azimuth is not None else geo.get("azimuth")
                    elev    = elev    if elev    is not None else geo.get("elevation_ft")
            if coords_are_sane(lat, lon, venue_id):
                wx = get_weather(
                    lat, lon, target_date,
                    first_pitch_utc=mlb_info.get("game_date", ""),
                    azimuth=azimuth,
                    venue_name=mlb_info.get("venue", ""),
                    roof=roof_kind(venue_id),
                    elevation_ft=elev,
                )
            else:
                # No trustworthy coordinates. Show nothing rather than the home team's
                # usual park — MLB's records are null for Mexico City and ~350 miles
                # off for Bristol, and a confidently wrong forecast is worse than none.
                _log(f"  No usable coordinates for venue {venue_id} "
                     f"({mlb_info.get('venue','?')}) — skipping weather")

        if args.html or args.suggestions_only or args.dump_cards:
            g = analyze_game(p1, p2, rhp, lhp, bp, mlb_info, wx, target_date,
                             rhp_ctx=rhp_ctx, lhp_ctx=lhp_ctx,
                             all_pool=all6, all_ctx=all12)
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
            today_s = target_date.strftime("%Y-%m-%d")
            g["away_ou"] = ou_trends(hist_games, g["away"], g["away_sp_id"], False, today_s)
            g["home_ou"] = ou_trends(hist_games, g["home"], g["home_sp_id"], True,  today_s)
            game_data.append(g)
        else:
            render_terminal.print_game(p1, p2, rhp, lhp, bp, mlb_info, wx,
                                       rhp_ctx=rhp_ctx, lhp_ctx=lhp_ctx,
                                       all_pool=all6, all_ctx=all12)

    if args.dump_cards:
        # Serialize exactly what the model is sent, and stop. No API call, so this is
        # free to run on a real slate — which is the only place the cards are real.
        from suggestions import _serialize_game_for_ai
        cards = [{"game": f"{g['away']} @ {g['home']}",
                  "card": _serialize_game_for_ai(g)} for g in game_data]
        Path(args.dump_cards).write_text(json.dumps(cards, indent=2))
        print(f"[dump-cards] wrote {len(cards)} cards to {args.dump_cards}", file=sys.stderr)
    elif args.suggestions_only:
        generate_suggestions(game_data, data_dir, target_date)
    elif args.html:
        generated_at = datetime.now(timezone.utc).isoformat()
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
