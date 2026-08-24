#!/usr/bin/env python3
"""
Permanent AI picks log — save suggestions and annotate final results.

picks/YYYY-MM-DD.json  (git-tracked)

Usage:
  python picks.py --save [--date YYYY-MM-DD]       # merge today's suggestions into picks log
  python picks.py --annotate [--date YYYY-MM-DD]   # fill won/lost from history/ scores
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path

from season import ET as _ET, resolve_cli_date

from teams import ODDS_TEAM as _CODE_TO_FULL, MLB_NAME_TO_CODE as _NAME_TO_CODE, normalize_name


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


def _et_date(ts: str) -> str:
    """ET calendar date of a UTC timestamp, or '' if unparseable."""
    if not ts:
        return ""
    try:
        return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone(_ET).strftime("%Y-%m-%d"))
    except Exception:
        return ""


def _build_game_info(history: list, date_str: str) -> dict:
    """{(away_full, home_full): {game_time_utc, codes...}} for games played on date_str.

    A slate file records every game the Odds API was serving that day, and the API
    serves one to three days ahead — so history/{date}.json holds several records per
    matchup with different start times, sorted ascending. Keying on (away, home) alone
    let the LAST record win, which is the furthest-FUTURE one, and stamped every pick
    with a later day's first pitch.

    That is what made picks stick in "upcoming" after their game had finished: both the
    server-side split in _render_suggestions_html() and splitPicks() in the page JS
    compare game_time_utc against now, and the timestamp really was hours away — it just
    belonged to a different day's game.

    So: prefer the record whose own ET date matches the slate date, and fall back to the
    earliest available only when nothing matches, rather than silently taking the latest.
    """
    best: dict = {}
    for rec in history:
        away_full = rec.get("away", "")
        home_full = rec.get("home", "")
        if not away_full or not home_full:
            continue
        gt = rec.get("game_time_utc", "")
        key = (away_full, home_full)
        cand = {
            "game_time_utc": gt,
            "away_code": rec.get("away_code", "") or _NAME_TO_CODE.get(away_full, ""),
            "home_code": rec.get("home_code", "") or _NAME_TO_CODE.get(home_full, ""),
            "away": away_full,
            "home": home_full,
            "_on_date": _et_date(gt) == date_str,
        }
        prev = best.get(key)
        if prev is None:
            best[key] = cand
            continue
        # An on-date record always beats an off-date one; between two of the same kind
        # take the earlier start (the first leg of a doubleheader, which picks carry no
        # game number to distinguish anyway).
        if cand["_on_date"] != prev["_on_date"]:
            if cand["_on_date"]:
                best[key] = cand
        elif (cand["game_time_utc"] or "9") < (prev["game_time_utc"] or "9"):
            best[key] = cand
    for v in best.values():
        v.pop("_on_date", None)
    return best


def _odds_num(pick: dict):
    """The pick's odds as a number, parsed from the odds string when the model omits it.

    `odds_num` is not something the model can be relied on to fill in: 7 of 197 picks
    over 2026-08-08..23 carried a perfectly good `odds` string ("-105") and a null
    `odds_num`. Nothing errored — those picks simply vanished from every P&L number,
    because ROI is computed from `odds_num` alone. The string is the value the reader
    sees, so it is the one to trust when the two disagree.
    """
    raw = (pick.get("odds") or "").strip().replace("+", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    num = pick.get("odds_num")
    return int(num) if isinstance(num, (int, float)) else None


def _canon_pick_key(pick: dict) -> tuple:
    """
    Canonical dedup key. Once a market group is picked for a game, the whole group is
    locked — no second pick regardless of direction, line, or period.

    Groups:
    - "winner"   : ML, Spread, F5_ML, F5_Spread — correlated; one per game
    - "runstotal": Total, F5_Total, Team_Total, F5 Team Total — all express an opinion on
                   the same run environment; one per game across all four
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
    # Correlated total markets: game total, F5 total, team total, and F5 team total all
    # express an opinion on the same run environment — one pick per game across all four.
    if bt in ("total", "f5total", "teamtotal", "f5teamtotal"):
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
    Deduplicates by `_canon_pick_key`, which keys on the correlated SLOT — not on the
    line. That is what stops two rungs of one alternate ladder (Section 8A of the AI
    prompt) being logged as two picks: "Over 4.5 Ks" and "Over 6.5 Ks" on the same
    pitcher are one opinion at two prices and collapse to one entry, keeping the first.
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
    game_info: dict = _build_game_info(_read_json(history_path) or [], date_str)

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

        # Enrich with game_time_utc and codes from history
        # game_key is "AWAY_CODE @ HOME_CODE" (e.g., "TEX @ MIA")
        parts = game_key.split(" @ ", 1)
        away_code = parts[0].strip() if len(parts) == 2 else ""
        home_code = parts[1].strip() if len(parts) == 2 else ""
        away_full = _CODE_TO_FULL.get(away_code, away_code)
        home_full = _CODE_TO_FULL.get(home_code, home_code)
        info = game_info.get((away_full, home_full), {})

        # The model occasionally emits the matchup backwards ("BOS @ ATH" for a game
        # that is really ATH @ BOS). Left alone that lands as a *second* copy of a bet
        # already logged — the dedupe key contains `game` — with no game_time_utc and a
        # team_side pointing at the wrong club. Snap it back to the real orientation so
        # it dedupes normally and grades against the right team.
        if not info:
            flipped = game_info.get((home_full, away_full))
            if flipped:
                info = flipped
                away_code, home_code = home_code, away_code
                away_full, home_full = home_full, away_full
                game_key = f"{away_code} @ {home_code}"
                pick["game"] = game_key
                # Re-derive the side from the team named in the bet text rather than
                # flipping the model's prefix. In the observed case team_side was
                # already right for the true orientation and only `game` was backwards,
                # so a blind flip would have pointed the bet at the wrong club. The
                # leading code in the bet text ("BOS Team Total Over 5.5", "TBR ML") is
                # the one field that does not depend on the model's home/away framing.
                side = (pick.get("team_side") or "")
                named = (pick.get("bet", "").split() or [""])[0].strip().upper()
                if named in (away_code.upper(), home_code.upper()):
                    prefix = "away" if named == away_code.upper() else "home"
                    if "_" in side:
                        pick["team_side"] = f"{prefix}_{side.split('_', 1)[1]}"
                    elif side in ("away", "home"):
                        pick["team_side"] = prefix
                print(f"[picks] corrected reversed matchup → {game_key}: "
                      f"{pick.get('bet','')} (side={pick.get('team_side')})")

        ck = _canon_pick_key(pick)
        if ck in by_key:
            continue

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
            "odds_num":      _odds_num(pick),
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


def fix_game_times(picks_dir: Path, history_dir: Path, target_date: date) -> int:
    """Re-derive game_time_utc on already-saved picks. Returns count corrected.

    Repairs picks written before _build_game_info() preferred the on-date history
    record. This does NOT touch the bet, line, price or reason, so the immutability
    guarantee in save_picks() is intact — it only corrects a start time that was
    always wrong and that decides which section of the page a pick renders in.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    picks_path = picks_dir / f"{date_str}.json"
    picks = _read_json(picks_path) or []
    if not picks:
        print(f"[picks] No picks file for {date_str}")
        return 0
    info = _build_game_info(_read_json(history_dir / f"{date_str}.json") or [], date_str)

    fixed = 0
    for p in picks:
        want = info.get((p.get("away", ""), p.get("home", "")))
        if not want:
            continue
        new = want.get("game_time_utc", "")
        old = p.get("game_time_utc", "")
        if new and new != old:
            p["game_time_utc"] = new
            fixed += 1
            print(f"  {p['game']:14s} {p['bet'][:34]:36s} {old or '(none)'} -> {new}")
    if fixed:
        picks_path.write_text(json.dumps(picks, indent=2))
        print(f"[picks] {date_str}: corrected {fixed} game time(s)")
    else:
        print(f"[picks] {date_str}: all game times already correct")
    return fixed


def load_all_picks(picks_dir: Path, target_date: date) -> list:
    """Return all picks for the date regardless of game start time."""
    date_str = target_date.strftime("%Y-%m-%d")
    picks_path = picks_dir / f"{date_str}.json"
    return _read_json(picks_path) or []


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


def _invert(result: str | None) -> str | None:
    """Flip an over-side grade onto the under side.

    _ou() always grades as if the bet were the over, so every under is that answer
    turned around. A push is a push either way, and an ungraded pick stays ungraded.
    """
    return {"won": "lost", "lost": "won"}.get(result, result)


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
    bet    = normalize_name(pick.get("bet") or "")

    away = game_rec.get("away_score")
    home = game_rec.get("home_score")

    # --- Full game ---
    if period == "full_game":
        if bt == "teamtotal":
            # side is "away_over", "away_under", "home_over", "home_under"
            score = away if side.startswith("away") else home
            if score is None or line is None:
                return None
            raw = _ou(score, line)   # graded as if it were the over
            return _invert(raw) if side.endswith("under") else raw
        if side in ("over", "under"):
            if away is None or home is None or line is None:
                return None
            raw = _ou(away + home, line)  # graded as if it were the over
            return raw if side == "over" else _invert(raw)
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
        if bt == "teamtotal":
            # F5 team totals arrive as bet_type "Team_Total" + period "f5" — there is
            # no F5_Team_Total in the tool schema. Without this case they fell through
            # every branch below (side is "home_over", never a bare "home") and graded
            # as None forever: all three ever picked before 2026-08-18 sat ungraded.
            score = af5 if side.startswith("away") else hf5
            if line is None:
                return None
            raw = _ou(score, line)   # graded as if it were the over
            return _invert(raw) if side.endswith("under") else raw
        if bt in ("f5total", "total"):
            if line is None:
                return None
            raw = _ou(af5 + hf5, line)  # graded as if it were the over
            return raw if side == "over" else _invert(raw)
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
        actual  = None
        p_line  = None
        for p in pitchers:
            name_parts = normalize_name(p.get("name") or "").split()
            last = name_parts[-1] if name_parts else ""
            if not last or last not in bet:
                continue
            # Older records can carry the same pitcher twice — a line-only entry
            # from the props feed and a stats-only entry from the boxscore, from
            # before normalize_name() unified accented and unaccented spellings
            # of the same name. Merge across every matching entry instead of
            # stopping at the first, so one half-populated copy doesn't shadow
            # the other.
            if actual is None:
                actual = p.get("actual_ks") if is_ks else p.get("actual_outs")
            if p_line is None:
                p_line = p.get("k_line") if is_ks else p.get("outs_line")
        # Grade against the line the pick was actually taken at. The history entry's
        # line is whatever the books last showed and can be missing entirely once
        # they pull the prop, but `line` on the pick is fixed at pick time.
        #
        # The pick's own line WINS. It used to be a fallback used only when history
        # had nothing, which was invisible while every prop was taken at the main
        # posted number — the two agreed. Alternate ladders break that: a pick at
        # "Over 4.5 Ks" off the alt market would have been graded against the 6.5 the
        # books led with, turning a won bet into a lost one with no error anywhere.
        if line is not None:
            p_line = line
        if actual is None or p_line is None:
            return None
        raw = _ou(actual, p_line)  # graded as if it were the over
        return raw if is_over else _invert(raw)

    return None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MLB AI picks log")
    ap.add_argument("--save", action="store_true", help="Merge today's suggestions into picks log")
    ap.add_argument("--annotate", action="store_true", help="Annotate picks with final results")
    ap.add_argument("--fix-times", action="store_true",
                    help="Re-derive game_time_utc on saved picks from history/")
    ap.add_argument("--date", default="today", help="today, yesterday, or YYYY-MM-DD")
    ap.add_argument("--data-dir", default="./data", help="Data directory (for suggestions)")
    ap.add_argument("--picks-dir", default="./picks", help="Picks output directory")
    ap.add_argument("--history-dir", default="./history", help="History directory (for annotation)")
    args = ap.parse_args()

    if not args.save and not args.annotate and not args.fix_times:
        ap.error("Specify --save, --annotate or --fix-times")

    target = resolve_cli_date(args.date)

    if args.save:
        save_picks(Path(args.data_dir), Path(args.picks_dir), target, Path(args.history_dir))

    if args.fix_times:
        fix_game_times(Path(args.picks_dir), Path(args.history_dir), target)

    if args.annotate:
        annotate_picks(Path(args.picks_dir), Path(args.history_dir), target)
