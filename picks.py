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

from teams import ODDS_TEAM as _CODE_TO_FULL, MLB_NAME_TO_CODE as _NAME_TO_CODE


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


def _canon_pick_key(pick: dict) -> tuple:
    """
    Canonical dedup key. Once a market group is picked for a game, the whole group is
    locked — no second pick regardless of direction, line, or period.

    Groups:
    - "winner"   : ML, Spread, F5_ML, F5_Spread — correlated; one per game
    - "runstotal": Total, F5_Total — correlated; one per game
    - "teamtotal": one per team (home/away), direction stripped from team_side
    - "pitcherks" / "pitcherouts": one per pitcher (keyed on pitcher last name)
    """
    game = pick.get("game", "")
    bt   = (pick.get("bet_type") or "").lower().replace("_", "").replace(" ", "")
    bet  = (pick.get("bet") or "").lower()
    # Normalize generic 'props' type by inferring from bet text
    if bt == "props":
        if any(x in bet for x in (" k ", "ks ", " ks", "strikeout", " k over", " k under")):
            bt = "pitcherks"
        elif "out" in bet:
            bt = "pitcherouts"
    # Pitcher props: one pick per pitcher regardless of market (Ks or Outs).
    # Both pitcherks and pitcherouts share the same "pitcher" slot so we never
    # give two prop bets on the same pitcher in the same game.
    if bt in ("pitcherks", "pitcherouts"):
        words = bet.split()
        pitcher_last = words[1] if len(words) >= 2 else (words[0] if words else "")
        return (game, "pitcher", pitcher_last)
    # Team totals: keyed on which team (home/away), direction stripped
    if bt == "teamtotal":
        ts = (pick.get("team_side") or "").lower()
        team = ts.split("_")[0] if ts else ""  # "home_over" → "home"
        return (game, "teamtotal", team)
    # Correlated total markets: full game total and F5 total share one slot
    if bt in ("total", "f5total"):
        return (game, "runstotal")
    # Correlated winner markets: ML, Spread, F5 ML, F5 Spread share one slot
    if bt in ("ml", "moneyline", "spread", "f5ml", "f5spread"):
        return (game, "winner")
    # Unknown market type — fall back to (game, bt) so nothing is silently dropped
    return (game, bt)


def save_picks(data_dir: Path, picks_dir: Path, target_date: date,
               history_dir: Path = Path("./history")) -> int:
    """
    Merge picks from today's suggestions cache into picks/YYYY-MM-DD.json.
    Deduplicates by canonical (game, bet_type, line, team_side) — keeps first price found.
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
    history_path = history_dir / f"{date_str}.json"
    game_info: dict = {}
    for rec in (_read_json(history_path) or []):
        away_full = rec.get("away", "")
        home_full = rec.get("home", "")
        away_code = rec.get("away_code", "") or _NAME_TO_CODE.get(away_full, "")
        home_code = rec.get("home_code", "") or _NAME_TO_CODE.get(home_full, "")
        game_info[(away_full, home_full)] = {
            "game_time_utc": rec.get("game_time_utc", ""),
            "away_code":     away_code,
            "home_code":     home_code,
            "away":          away_full,
            "home":          home_full,
        }

    picks_path = picks_dir / f"{date_str}.json"
    existing = _read_json(picks_path) or []
    # Build by canonical key, keeping first occurrence (= first price found)
    by_key: dict[tuple, dict] = {}
    for p in existing:
        ck = _canon_pick_key(p)
        if ck not in by_key:
            by_key[ck] = p

    found_at = datetime.now(timezone.utc).isoformat()
    added = 0

    for pick in all_picks:
        game_key = pick.get("game", "")
        if not game_key:
            continue
        ck = _canon_pick_key(pick)
        if ck in by_key:
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
        by_key[ck] = record
        added += 1

    records = list(by_key.values())
    if records:
        picks_dir.mkdir(parents=True, exist_ok=True)
        picks_path.write_text(json.dumps(records, indent=2))
        print(f"[picks] {date_str}: {added} new pick(s), {len(records)} total — picks/{date_str}.json")

    return added


def load_all_picks(picks_dir: Path, target_date: date) -> list:
    """Return all picks for the date regardless of game start time."""
    date_str = target_date.strftime("%Y-%m-%d")
    picks_path = picks_dir / f"{date_str}.json"
    return _read_json(picks_path) or []


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
    """Fill in result (won/lost/push) for all pick types using enriched history/."""
    date_str = target_date.strftime("%Y-%m-%d")
    picks_path   = picks_dir   / f"{date_str}.json"
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
    # Keyed by (away, home) — picks store full team names.
    # Include any record that has been annotated by history.py (scores or status set).
    game_by_key = {
        (r.get("away", ""), r.get("home", "")): r
        for r in history
        if r.get("annotated_at")
    }

    now = datetime.now(timezone.utc).isoformat()
    updated = determined = 0

    for pick in picks:
        if pick.get("annotated_at"):
            continue
        game_rec = game_by_key.get((pick.get("away", ""), pick.get("home", "")))
        if not game_rec:
            continue

        # Game did not complete — void the pick
        if game_rec.get("status") in ("postponed", "cancelled", "canceled", "suspended"):
            pick["result"]           = "void"
            pick["away_score_final"] = None
            pick["home_score_final"] = None
            pick["annotated_at"]     = now
            updated += 1
            determined += 1
            print(f"  {pick['game']} | {pick['bet']} → VOID ({game_rec['status']})")
            continue

        away_score = game_rec.get("away_score")
        home_score = game_rec.get("home_score")
        if away_score is None or home_score is None:
            continue

        pick["away_score_final"] = int(away_score)
        pick["home_score_final"] = int(home_score)

        result = _resolve_pick(pick, game_rec)
        pick["result"]       = result
        pick["annotated_at"] = now
        updated += 1

        if result:
            determined += 1
            icon = "WON" if result == "won" else "LOST" if result == "lost" else "PUSH"
            print(f"  {pick['game']} | {pick['bet']} → {icon} ({away_score}-{home_score})")
        else:
            print(f"  {pick['game']} | {pick['bet']} → {away_score}-{home_score} [pending manual]")

    if updated:
        picks_path.write_text(json.dumps(picks, indent=2))
        print(f"[picks] {date_str}: {determined} result(s) determined, {updated} score(s) recorded")
    else:
        pending = sorted(set(p["game"] for p in picks if not p.get("annotated_at")))
        if pending:
            print(f"[picks] {date_str}: scores pending for {', '.join(pending)}")

    return determined


def _ou(actual, line) -> str | None:
    """True/False/'push' comparison: actual vs line. True → actual > line (over hit)."""
    if actual is None or line is None:
        return None
    if actual > line:
        return "won"   # over hit
    if actual < line:
        return "lost"  # under hit
    return "push"


def _ml_or_spread(team_score, line, opponent_score) -> str | None:
    """won/lost/push for ML (line=None) or spread. team_score is the bet side."""
    if team_score is None or opponent_score is None:
        return None
    if line is None:
        # Moneyline
        if team_score > opponent_score:
            return "won"
        if team_score < opponent_score:
            return "lost"
        return "push"
    adj = team_score + line
    if adj > opponent_score:
        return "won"
    if adj < opponent_score:
        return "lost"
    return "push"


def _resolve_pick(pick: dict, game_rec: dict) -> str | None:
    """
    Determine won/lost/push for any pick type using the enriched history record.
    Returns None when results aren't available yet.
    """
    bt     = (pick.get("bet_type") or "").lower().replace("_", "").replace(" ", "")
    period = pick.get("period", "full_game")
    side   = (pick.get("team_side") or "").lower()
    line   = pick.get("line")
    bet    = (pick.get("bet") or "").lower()

    away = game_rec.get("away_score")
    home = game_rec.get("home_score")

    # --- Full game ---
    if period == "full_game":
        if bt == "teamtotal":
            # side is "away_over", "away_under", "home_over", "home_under"
            score = away if side.startswith("away") else home
            if score is None or line is None:
                return None
            raw = _ou(score, line)   # "won"=over, "lost"=under, "push"
            if side.endswith("under"):
                if raw == "won":   return "lost"
                if raw == "lost":  return "won"
            return raw
        if side in ("over", "under"):
            if away is None or home is None or line is None:
                return None
            raw = _ou(away + home, line)  # "won"=over hit
            return raw if side == "over" else (
                "won" if raw == "lost" else "lost" if raw == "won" else "push"
            )
        if side == "away":
            return _ml_or_spread(away, line, home)
        if side == "home":
            return _ml_or_spread(home, line, away)
        return None

    # --- First 5 innings ---
    if period == "f5":
        af5 = game_rec.get("away_f5_score")
        hf5 = game_rec.get("home_f5_score")
        if af5 is None or hf5 is None:
            return None
        if bt in ("f5total", "total"):
            if line is None:
                return None
            raw = _ou(af5 + hf5, line)
            return raw if side == "over" else (
                "won" if raw == "lost" else "lost" if raw == "won" else "push"
            )
        if side == "away":
            return _ml_or_spread(af5, line, hf5)
        if side == "home":
            return _ml_or_spread(hf5, line, af5)
        return None

    # --- Pitcher props ---
    if period == "props":
        pitchers = game_rec.get("pitchers", [])
        is_ks   = bt == "pitcherks"
        is_over = "over" in bet
        for p in pitchers:
            last = (p.get("name") or "").split()[-1].lower()
            if not last or last not in bet:
                continue
            actual = p.get("actual_ks")   if is_ks else p.get("actual_outs")
            p_line = p.get("k_line")      if is_ks else p.get("outs_line")
            if actual is None or p_line is None:
                return None
            raw = _ou(actual, p_line)  # "won"=over hit
            return raw if is_over else (
                "won" if raw == "lost" else "lost" if raw == "won" else "push"
            )
        return None

    return None


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
        save_picks(Path(args.data_dir), Path(args.picks_dir), target, Path(args.history_dir))

    if args.annotate:
        annotate_picks(Path(args.picks_dir), Path(args.history_dir), target)
