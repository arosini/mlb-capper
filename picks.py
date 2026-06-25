#!/usr/bin/env python3
"""
Permanent AI picks log — save suggestions and annotate final results.

picks/YYYY-MM-DD.json  (git-tracked)

Usage:
  python picks.py --save [--date YYYY-MM-DD]       # merge today's suggestions into picks log
  python picks.py --annotate [--date YYYY-MM-DD]   # fill won/lost from history/ scores
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ET = timezone(timedelta(hours=-4))

# Handigraphs code → full Odds API team name (to match history/ records)
_CODE_TO_FULL = {
    "ARI": "Arizona Diamondbacks",  "ATH": "Athletics",
    "ATL": "Atlanta Braves",        "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",        "CHC": "Chicago Cubs",
    "CHW": "Chicago White Sox",     "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",   "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",        "HOU": "Houston Astros",
    "KCR": "Kansas City Royals",    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",   "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",     "MIN": "Minnesota Twins",
    "NYM": "New York Mets",         "NYY": "New York Yankees",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
    "SDP": "San Diego Padres",      "SEA": "Seattle Mariners",
    "SFG": "San Francisco Giants",  "STL": "St. Louis Cardinals",
    "TBR": "Tampa Bay Rays",        "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",     "WSN": "Washington Nationals",
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


def _extract_picks(sugg: dict) -> list:
    """Extract normalized pick records from either new (picks[]) or old (best_bet/other_bets) schema."""
    if "picks" in sugg:
        return list(sugg["picks"])
    result = []
    best = sugg.get("best_bet")
    if best:
        result.append({**best, "is_best": True, "bet_type": best.get("bet_type", ""),
                       "team_side": None, "line": None, "period": "full_game",
                       "odds_num": None})
    for o in (sugg.get("other_bets") or []):
        result.append({**o, "is_best": False, "bet_type": o.get("bet_type", ""),
                       "team_side": None, "line": None, "period": "full_game",
                       "odds_num": None})
    return result


def save_picks(data_dir: Path, picks_dir: Path, target_date: date) -> int:
    """
    Merge picks from today's suggestions cache into picks/YYYY-MM-DD.json.
    Deduplicates by (game, bet_type, bet) — only adds truly new picks.
    Enriches each pick with away_code, home_code, and game_time_utc from history.
    Returns count of new picks added.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    sugg_path = data_dir / f"suggestions_{date_str}.json"
    sugg = _read_json(sugg_path)
    if not sugg:
        print(f"[picks] No suggestions file for {date_str} — skipping")
        return 0

    all_picks = _extract_picks(sugg)
    if not all_picks:
        print(f"[picks] No picks in suggestions for {date_str}")
        return 0

    # Build game info lookup from history file: full_name_key → {game_time_utc, away_code, home_code}
    history_path = Path("./history") / f"{date_str}.json"
    game_info: dict = {}
    for rec in (_read_json(history_path) or []):
        away_full = rec.get("away", "")
        home_full = rec.get("home", "")
        away_code = rec.get("away_code", "")
        home_code = rec.get("home_code", "")
        if not away_code and away_full:
            from history import _NAME_TO_CODE
            away_code = _NAME_TO_CODE.get(away_full, "")
            home_code = _NAME_TO_CODE.get(home_full, "")
        game_info[(away_full, home_full)] = {
            "game_time_utc": rec.get("game_time_utc", ""),
            "away_code":     away_code,
            "home_code":     home_code,
            "away":          away_full,
            "home":          home_full,
        }

    picks_path = picks_dir / f"{date_str}.json"
    existing = _read_json(picks_path) or []
    by_key = {(p["game"], p.get("bet_type", ""), p["bet"]): p for p in existing}

    found_at = datetime.now(timezone.utc).isoformat()
    added = 0

    for pick in all_picks:
        game_key = pick.get("game", "")
        if not game_key:
            continue
        key = (game_key, pick.get("bet_type", ""), pick.get("bet", ""))
        if key in by_key:
            continue

        # Enrich with game_time_utc and codes from history
        # game_key is "AWAY_CODE @ HOME_CODE" (e.g., "TEX @ MIA")
        parts = game_key.split(" @ ", 1)
        away_code = parts[0].strip() if len(parts) == 2 else ""
        home_code = parts[1].strip() if len(parts) == 2 else ""
        away_full = _CODE_TO_FULL.get(away_code, away_code)
        home_full = _CODE_TO_FULL.get(home_code, home_code)
        info = game_info.get((away_full, home_full), {})

        record = {
            "date":          date_str,
            "game":          game_key,
            "away":          info.get("away", away_full),
            "away_code":     info.get("away_code", away_code),
            "home":          info.get("home", home_full),
            "home_code":     info.get("home_code", home_code),
            "game_time_utc": info.get("game_time_utc", ""),
            "bet_type":      pick.get("bet_type", ""),
            "bet":           pick.get("bet", ""),
            "team_side":     pick.get("team_side"),
            "line":          pick.get("line"),
            "period":        pick.get("period", "full_game"),
            "odds":          pick.get("odds", ""),
            "odds_num":      pick.get("odds_num"),
            "is_best":       bool(pick.get("is_best")),
            "confidence":    pick.get("confidence", ""),
            "reason":        pick.get("reason", ""),
            "line_warning":  pick.get("line_warning", False),
            "alt_suggestion": pick.get("alt_suggestion"),
            "found_at":      found_at,
            "result":        None,
            "away_score_final": None,
            "home_score_final": None,
            "annotated_at":  None,
        }
        by_key[key] = record
        added += 1

    records = list(by_key.values())
    if records:
        picks_dir.mkdir(parents=True, exist_ok=True)
        picks_path.write_text(json.dumps(records, indent=2))
        print(f"[picks] {date_str}: {added} new pick(s), {len(records)} total — picks/{date_str}.json")

    return added


def load_valid_picks(picks_dir: Path, target_date: date, now: datetime = None) -> list:
    """Return picks for games that haven't started yet (still actionable)."""
    date_str = target_date.strftime("%Y-%m-%d")
    picks_path = picks_dir / f"{date_str}.json"
    records = _read_json(picks_path) or []
    if now is None:
        now = datetime.now(timezone.utc)
    valid = []
    for p in records:
        gt = p.get("game_time_utc", "")
        if not gt:
            valid.append(p)
            continue
        try:
            gt_dt = datetime.fromisoformat(gt.replace("Z", "+00:00"))
            if gt_dt > now:
                valid.append(p)
        except Exception:
            valid.append(p)
    return valid


def annotate_picks(picks_dir: Path, history_dir: Path, target_date: date) -> int:
    """Fill in result (won/lost/push) using final scores from history/."""
    date_str = target_date.strftime("%Y-%m-%d")
    picks_path = picks_dir / f"{date_str}.json"
    history_path = history_dir / f"{date_str}.json"

    picks = _read_json(picks_path) or []
    if not picks:
        print(f"[picks] No picks file for {date_str}")
        return 0

    unannotated = [p for p in picks if not p.get("annotated_at")]
    if not unannotated:
        print(f"[picks] {date_str}: all picks already annotated")
        return 0

    history = _read_json(history_path) or []
    scores_by_game = {
        (r.get("away", ""), r.get("home", "")): r
        for r in history
        if r.get("annotated_at") and r.get("away_score") is not None
    }

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    determined = 0

    for pick in picks:
        if pick.get("annotated_at"):
            continue
        game_rec = scores_by_game.get((pick.get("away", ""), pick.get("home", "")))
        if not game_rec:
            continue

        away_score = int(game_rec["away_score"])
        home_score = int(game_rec["home_score"])
        result = _calc_result(
            pick.get("team_side"),
            pick.get("line"),
            pick.get("period", "full_game"),
            away_score,
            home_score,
        )

        pick["result"] = result
        pick["away_score_final"] = away_score
        pick["home_score_final"] = home_score
        pick["annotated_at"] = now
        updated += 1

        if result:
            determined += 1
            icon = "WON" if result == "won" else "LOST" if result == "lost" else "PUSH"
            print(f"  {pick['game']} | {pick['bet']} → {icon} "
                  f"({away_score}-{home_score})")
        else:
            print(f"  {pick['game']} | {pick['bet']} → unresolvable "
                  f"({away_score}-{home_score}) [no team_side/line set]")

    if updated:
        picks_path.write_text(json.dumps(picks, indent=2))
        print(f"[picks] {date_str}: {determined} result(s) determined, {updated} score(s) recorded")
    else:
        pending = list(set(p["game"] for p in picks if not p.get("annotated_at")))
        if pending:
            print(f"[picks] {date_str}: scores pending for {', '.join(pending)}")

    return determined


def _calc_result(team_side, line, period, away_score, home_score):
    """Determine won/lost/push from final scores. Returns None if indeterminate."""
    if period and period not in ("full_game",):
        return None  # F5 or props — can't auto-annotate from final score

    if not team_side:
        return None

    total = away_score + home_score

    if team_side == "over":
        if line is None:
            return None
        return "won" if total > line else "lost" if total < line else "push"

    if team_side == "under":
        if line is None:
            return None
        return "won" if total < line else "lost" if total > line else "push"

    if team_side not in ("away", "home"):
        return None  # team_total sides not auto-annotatable

    if line is None:
        # Moneyline
        if team_side == "away":
            return "won" if away_score > home_score else "lost" if away_score < home_score else "push"
        else:
            return "won" if home_score > away_score else "lost" if home_score < away_score else "push"
    else:
        # Spread: bet team covers if (team_score + line) > opponent_score
        if team_side == "away":
            adj = away_score + line
            return "won" if adj > home_score else "lost" if adj < home_score else "push"
        else:
            adj = home_score + line
            return "won" if adj > away_score else "lost" if adj < away_score else "push"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MLB AI picks log")
    ap.add_argument("--save", action="store_true", help="Merge today's suggestions into picks log")
    ap.add_argument("--annotate", action="store_true", help="Annotate picks with final results")
    ap.add_argument("--date", default="today", help="today, yesterday, or YYYY-MM-DD")
    ap.add_argument("--data-dir", default="./data", help="Data directory (for suggestions)")
    ap.add_argument("--picks-dir", default="./picks", help="Picks output directory")
    ap.add_argument("--history-dir", default="./history", help="History directory (for annotation)")
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

    if args.save:
        save_picks(Path(args.data_dir), Path(args.picks_dir), target)

    if args.annotate:
        annotate_picks(Path(args.picks_dir), Path(args.history_dir), target)
