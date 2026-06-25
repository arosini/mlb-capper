#!/usr/bin/env python3
"""
Permanent odds history — save pre-game odds and annotate final results.

Records live in history/YYYY-MM-DD.json (git-tracked).

Usage:
  python history.py --save [--date YYYY-MM-DD]      # write/update today's odds
  python history.py --annotate [--date YYYY-MM-DD]  # fill in results (run at 3 AM ET)
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ET = timezone(timedelta(hours=-4))

MLB_API = "https://statsapi.mlb.com/api/v1"

# Full team name (Odds API) → Handigraphs code
_NAME_TO_CODE = {
    "Arizona Diamondbacks":   "ARI",  "Athletics":              "ATH",
    "Atlanta Braves":         "ATL",  "Baltimore Orioles":      "BAL",
    "Boston Red Sox":         "BOS",  "Chicago Cubs":           "CHC",
    "Chicago White Sox":      "CHW",  "Cincinnati Reds":        "CIN",
    "Cleveland Guardians":    "CLE",  "Colorado Rockies":       "COL",
    "Detroit Tigers":         "DET",  "Houston Astros":         "HOU",
    "Kansas City Royals":     "KCR",  "Los Angeles Angels":     "LAA",
    "Los Angeles Dodgers":    "LAD",  "Miami Marlins":          "MIA",
    "Milwaukee Brewers":      "MIL",  "Minnesota Twins":        "MIN",
    "New York Mets":          "NYM",  "New York Yankees":       "NYY",
    "Philadelphia Phillies":  "PHI",  "Pittsburgh Pirates":     "PIT",
    "San Diego Padres":       "SDP",  "Seattle Mariners":       "SEA",
    "San Francisco Giants":   "SFG",  "St. Louis Cardinals":    "STL",
    "Tampa Bay Rays":         "TBR",  "Texas Rangers":          "TEX",
    "Toronto Blue Jays":      "TOR",  "Washington Nationals":   "WSN",
    "Oakland Athletics":      "ATH",  # legacy name still in some API responses
}

# Handigraphs code → MLB Stats API abbreviation (where they differ)
_TO_MLB_ABBR = {
    "CHW": "CWS", "KCR": "KC", "SDP": "SD", "SFG": "SF",
    "TBR": "TB",  "WSN": "WSH", "ARI": "AZ",
}


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _pick_best_result(candidates: list, game_time_utc: str) -> dict:
    """For doubleheaders: pick the result whose game_time is closest to game_time_utc."""
    if len(candidates) == 1:
        return candidates[0]
    if not game_time_utc:
        return candidates[0]
    try:
        target = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
    except Exception:
        return candidates[0]

    def _dist(c):
        try:
            return abs((datetime.fromisoformat(
                c.get("game_time", "").replace("Z", "+00:00")) - target
            ).total_seconds())
        except Exception:
            return float("inf")

    return min(candidates, key=_dist)


def _best_price(bookmakers: list, market_key: str, outcome_name: str):
    best = None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name") == outcome_name:
                    p = oc.get("price")
                    if p is not None and (best is None or p > best):
                        best = p
    return best


def _best_spread(bookmakers: list, outcome_name: str, market_key: str = "spreads"):
    best_price, best_point = None, None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name") == outcome_name:
                    p, pt = oc.get("price"), oc.get("point")
                    if p is not None and (best_price is None or p > best_price):
                        best_price, best_point = p, pt
    return best_point, best_price


def _best_total(bookmakers: list, side: str, market_key: str = "totals"):
    best_price, best_point = None, None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name") == side and oc.get("point") is not None:
                    p, pt = oc.get("price"), oc.get("point")
                    if p is not None and (best_price is None or p > best_price):
                        best_price, best_point = p, pt
    return best_point, best_price


def _load_starters_by_team(data_dir: Path, date_str: str) -> dict:
    """Return {team_code: {name, hand, mlbam_id}} from the starters JSON."""
    for slot in ("today", "tomorrow"):
        p = data_dir / f"starters_last3g_{slot}_{date_str}.json"
        if p.exists():
            break
    else:
        return {}
    try:
        raw = json.loads(p.read_text())
        rows = raw.get("starters", raw) if isinstance(raw, dict) else raw
    except Exception:
        return {}
    result = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        team = r.get("team", "")
        if not team:
            continue
        result[team] = {
            "name":     r.get("name", "TBD"),
            "hand":     (r.get("throws") or "?")[0].upper(),
            "mlbam_id": r.get("mlbam_id") or r.get("id"),
        }
    return result


def save_odds_history(data_dir: Path, history_dir: Path, target_date: date) -> int:
    """
    Read current odds + starters; write/update history/{date}.json.
    Skips records where odds_final=True (game already started + odds locked in).
    Returns count of records written/updated.
    """
    date_str = target_date.strftime("%Y-%m-%d")

    odds_raw = _read_json(data_dir / f"odds_{date_str}.json")
    if not odds_raw:
        print(f"[history] No odds file for {date_str} — skipping")
        return 0

    odds_meta = _read_json(data_dir / f"odds_meta_{date_str}.json") or {}
    odds_recorded_at = odds_meta.get("fetched_at", datetime.now(timezone.utc).isoformat())

    starters = _load_starters_by_team(data_dir, date_str)

    # Load existing records for the day; keyed by (away, home, game_time_utc) to
    # handle doubleheaders where the same two teams play twice in one day.
    hist_path = history_dir / f"{date_str}.json"
    existing: list[dict] = _read_json(hist_path) or []
    by_game: dict[tuple, dict] = {
        (r["away"], r["home"], r.get("game_time_utc", "")): r for r in existing
    }

    now = datetime.now(timezone.utc)
    written = 0

    for game in odds_raw:
        if not isinstance(game, dict):
            continue
        away_name = game.get("away_team", "")
        home_name = game.get("home_team", "")
        if not away_name or not home_name:
            continue

        commence = game.get("commence_time", "")
        key = (away_name, home_name, commence)
        existing_rec = by_game.get(key, {})

        # Don't update records where odds are already locked in
        if existing_rec.get("odds_final"):
            continue

        # Determine if game has started
        game_started = False
        if commence:
            try:
                ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                game_started = ct <= now
            except Exception:
                pass

        away_code = _NAME_TO_CODE.get(away_name, "")
        home_code = _NAME_TO_CODE.get(home_name, "")
        away_sp = starters.get(away_code, {})
        home_sp = starters.get(home_code, {})

        bks = game.get("bookmakers", [])
        sp_away_pt, sp_away_pr = _best_spread(bks, away_name)
        sp_home_pt, sp_home_pr = _best_spread(bks, home_name)
        over_pt, over_pr = _best_total(bks, "Over")
        _, under_pr    = _best_total(bks, "Under")

        rec = {
            # Game identity
            "date":           date_str,
            "away":           away_name,
            "away_code":      away_code,
            "home":           home_name,
            "home_code":      home_code,
            "game_time_utc":  commence,
            # Pitchers
            "away_pitcher":      away_sp.get("name", "TBD"),
            "away_pitcher_hand": away_sp.get("hand", ""),
            "away_pitcher_id":   away_sp.get("mlbam_id"),
            "home_pitcher":      home_sp.get("name", "TBD"),
            "home_pitcher_hand": home_sp.get("hand", ""),
            "home_pitcher_id":   home_sp.get("mlbam_id"),
            # Odds (best available across DK/FanDuel/Fanatics)
            "ml_away":           _best_price(bks, "h2h", away_name),
            "ml_home":           _best_price(bks, "h2h", home_name),
            "spread_away_line":  sp_away_pt,
            "spread_away_price": sp_away_pr,
            "spread_home_line":  sp_home_pt,
            "spread_home_price": sp_home_pr,
            "total_line":        over_pt,
            "over_price":        over_pr,
            "under_price":       under_pr,
            "odds_recorded_at":  odds_recorded_at,
            "odds_final":        game_started,
            # Result (filled by --annotate)
            "away_score":   existing_rec.get("away_score"),
            "home_score":   existing_rec.get("home_score"),
            "total_runs":   existing_rec.get("total_runs"),
            "over_hit":     existing_rec.get("over_hit"),
            "home_win":     existing_rec.get("home_win"),
            "annotated_at": existing_rec.get("annotated_at"),
        }

        by_game[key] = rec
        written += 1

    records = list(by_game.values())
    if records:
        # Sort by game time for readability
        records.sort(key=lambda r: r.get("game_time_utc") or "")
        _write_json(hist_path, records)
        final_count = sum(1 for r in records if r.get("odds_final"))
        print(f"[history] {date_str}: {written} updated, {final_count} final, {len(records)} total — history/{date_str}.json")

    return written


def annotate_results(history_dir: Path, target_date: date) -> int:
    """
    Fill in final scores for completed games in history/{date}.json.
    Returns count of newly annotated games.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    hist_path = history_dir / f"{date_str}.json"
    records: list[dict] = _read_json(hist_path) or []

    unannotated = [r for r in records if not r.get("annotated_at")]
    if not unannotated:
        print(f"[annotate] {date_str}: all games already annotated")
        return 0

    scores = _fetch_final_scores(target_date)
    if not scores:
        print(f"[annotate] {date_str}: no final scores available yet")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    annotated = 0

    for rec in records:
        if rec.get("annotated_at"):
            continue
        away_code = rec.get("away_code") or _NAME_TO_CODE.get(rec.get("away", ""), "")
        home_code = rec.get("home_code") or _NAME_TO_CODE.get(rec.get("home", ""), "")
        away_mlb  = _TO_MLB_ABBR.get(away_code, away_code)
        home_mlb  = _TO_MLB_ABBR.get(home_code, home_code)

        candidates = scores.get(frozenset([away_mlb, home_mlb]))
        if not candidates:
            continue

        # For doubleheaders pick the game whose scheduled time is closest to this record
        result = _pick_best_result(candidates, rec.get("game_time_utc", ""))

        # Postponed / cancelled / suspended — mark void so we stop retrying
        if result.get("status"):
            rec["status"] = result["status"]
            rec["annotated_at"] = now
            annotated += 1
            print(f"  {rec['away']} @ {rec['home']} → {result['status'].upper()}")
            continue

        away_score = result["away_score"]
        home_score = result["home_score"]
        total_runs = away_score + home_score
        total_line = rec.get("total_line")

        rec["away_score"] = away_score
        rec["home_score"] = home_score
        rec["total_runs"] = total_runs
        rec["home_win"] = home_score > away_score
        if total_line is not None:
            if total_runs > total_line:
                rec["over_hit"] = True
            elif total_runs < total_line:
                rec["over_hit"] = False
            else:
                rec["over_hit"] = None  # push
        rec["annotated_at"] = now
        annotated += 1
        print(f"  {rec['away']} {away_score} @ {rec['home']} {home_score}  "
              f"(total {total_runs}, line {total_line} → "
              f"{'OVER' if rec.get('over_hit') else 'UNDER' if rec.get('over_hit') is False else 'PUSH'})")

    if annotated:
        _write_json(hist_path, records)
        print(f"[annotate] {date_str}: annotated {annotated} game(s)")
    else:
        unannotated = [f"{r['away']} @ {r['home']}" for r in records if not r.get("annotated_at")]
        if unannotated:
            print(f"[annotate] {date_str}: no new results found (still pending: {', '.join(unannotated)})")
        else:
            print(f"[annotate] {date_str}: all games already annotated")

    return annotated


def _fetch_final_scores(target_date: date) -> dict:
    """
    Query MLB Stats API for final scores and non-playing game statuses.

    Returns {frozenset({away_abbr, home_abbr}): list[dict]} where each dict is one
    of the following (supporting doubleheaders via the list):
      - Completed:  {"away_score": int, "home_score": int, "game_time": str}
      - Non-playing: {"status": "postponed"|"cancelled"|"suspended", "game_time": str}
    """
    try:
        import requests
        r = requests.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1, "date": target_date.isoformat(),
                "gameType": "R", "hydrate": "linescore,team",
            },
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[annotate] MLB API error: {e}", file=sys.stderr)
        return {}

    _NON_PLAYING = {"Postponed", "Cancelled", "Canceled", "Suspended"}

    results: dict = {}
    for date_entry in r.json().get("dates", []):
        for g in date_entry.get("games", []):
            teams = g.get("teams", {})
            ha = teams.get("home", {}).get("team", {}).get("abbreviation", "")
            aa = teams.get("away", {}).get("team", {}).get("abbreviation", "")
            if not ha or not aa:
                continue

            key = frozenset([aa, ha])
            game_time = g.get("gameDate", "")
            status_obj = g.get("status", {})
            abstract = status_obj.get("abstractGameState", "")
            detailed = status_obj.get("detailedState", "")

            if abstract == "Final":
                hs = teams.get("home", {}).get("score")
                as_ = teams.get("away", {}).get("score")
                if hs is not None and as_ is not None:
                    results.setdefault(key, []).append({
                        "away_score": int(as_),
                        "home_score": int(hs),
                        "game_time":  game_time,
                    })
            elif detailed in _NON_PLAYING:
                results.setdefault(key, []).append({
                    "status":    detailed.lower(),
                    "game_time": game_time,
                })

    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MLB odds history tool")
    ap.add_argument("--save", action="store_true", help="Save today's odds to history/")
    ap.add_argument("--annotate", action="store_true", help="Annotate results for completed games")
    ap.add_argument("--date", default="today", help="today, yesterday, or YYYY-MM-DD")
    ap.add_argument("--data-dir", default="./data", help="Data directory")
    ap.add_argument("--history-dir", default="./history", help="History directory")
    args = ap.parse_args()

    if not args.save and not args.annotate:
        ap.error("Specify --save or --annotate")

    today_et = datetime.now(_ET).date()
    if args.date == "today":
        target = today_et
    elif args.date == "yesterday":
        target = today_et - timedelta(days=1)
    else:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit(f"Invalid date: {args.date}")

    data_dir    = Path(args.data_dir)
    history_dir = Path(args.history_dir)

    if args.save:
        save_odds_history(data_dir, history_dir, target)

    if args.annotate:
        annotate_results(history_dir, target)
