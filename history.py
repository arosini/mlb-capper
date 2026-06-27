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


# ---------------------------------------------------------------------------
# Odds helper functions
# ---------------------------------------------------------------------------

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


def _prop_best(bookmakers: list, market_key: str, outcome_name: str, description: str = None):
    """
    Best price + point across bookmakers for a props market outcome.
    description filters by outcome['description'] (used for team totals, pitcher props).
    """
    best_price, best_point = None, None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name") != outcome_name:
                    continue
                if description is not None and oc.get("description", "") != description:
                    continue
                p, pt = oc.get("price"), oc.get("point")
                if p is not None and (best_price is None or p > best_price):
                    best_price, best_point = p, pt
    return best_point, best_price


def _pitcher_names_in_props(bookmakers: list) -> list:
    """Return unique pitcher names appearing in pitcher_strikeouts or pitcher_outs markets."""
    names = set()
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] not in ("pitcher_strikeouts", "pitcher_outs"):
                continue
            for oc in mkt.get("outcomes", []):
                n = oc.get("description", "")
                if n:
                    names.add(n)
    return sorted(names)


def _build_pitcher_props(bookmakers: list, existing_by_name: dict) -> list:
    """
    Build pitcher props list from bookmakers.
    Preserves existing annotated results (actual_ks, actual_outs, *_over_hit).
    """
    pitchers = []
    for name in _pitcher_names_in_props(bookmakers):
        k_line,    k_over_pr  = _prop_best(bookmakers, "pitcher_strikeouts", "Over",  name)
        _,         k_under_pr = _prop_best(bookmakers, "pitcher_strikeouts", "Under", name)
        outs_line, outs_ov_pr = _prop_best(bookmakers, "pitcher_outs",       "Over",  name)
        _,         outs_un_pr = _prop_best(bookmakers, "pitcher_outs",       "Under", name)
        existing = existing_by_name.get(name.lower(), {})
        pitchers.append({
            "name":            name,
            "k_line":          k_line,
            "k_over_price":    k_over_pr,
            "k_under_price":   k_under_pr,
            "outs_line":       outs_line,
            "outs_over_price": outs_ov_pr,
            "outs_under_price":outs_un_pr,
            # Preserve any already-annotated results
            "actual_ks":       existing.get("actual_ks"),
            "actual_outs":     existing.get("actual_outs"),
            "k_over_hit":      existing.get("k_over_hit"),
            "outs_over_hit":   existing.get("outs_over_hit"),
        })
    return pitchers


# ---------------------------------------------------------------------------
# Starters loader
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_odds_history(data_dir: Path, history_dir: Path, target_date: date) -> int:
    """
    Read current odds + props + starters; write/update history/{date}.json.
    Skips records where odds_final=True (game already started — odds locked in).
    Returns count of records written/updated.
    """
    date_str = target_date.strftime("%Y-%m-%d")

    odds_raw = _read_json(data_dir / f"odds_{date_str}.json")
    if not odds_raw:
        print(f"[history] No odds file for {date_str} — skipping")
        return 0

    odds_meta = _read_json(data_dir / f"odds_meta_{date_str}.json") or {}
    odds_recorded_at = odds_meta.get("fetched_at", datetime.now(timezone.utc).isoformat())

    # Props file keyed by event_id
    props_by_event: dict = _read_json(data_dir / f"props_{date_str}.json") or {}

    starters = _load_starters_by_team(data_dir, date_str)

    # Load existing records; keyed by (away, home, game_time_utc) to support doubleheaders
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

        # Full-game odds from bulk endpoint
        bks = game.get("bookmakers", [])
        sp_away_pt, sp_away_pr = _best_spread(bks, away_name)
        sp_home_pt, sp_home_pr = _best_spread(bks, home_name)
        over_pt, over_pr = _best_total(bks, "Over")
        _,         under_pr = _best_total(bks, "Under")

        # Per-event props (F5 + team totals + pitcher props)
        event_id = game.get("id", "")
        props_game = props_by_event.get(event_id, {})
        pbks: list = props_game.get("bookmakers", []) if isinstance(props_game, dict) else []

        # F5 ML
        f5_ml_away = _prop_best(pbks, "h2h_1st_5_innings", away_name)[1]
        f5_ml_home = _prop_best(pbks, "h2h_1st_5_innings", home_name)[1]

        # F5 spread
        f5_sp_away_pt, f5_sp_away_pr = _best_spread(pbks, away_name, "spreads_1st_5_innings")
        f5_sp_home_pt, f5_sp_home_pr = _best_spread(pbks, home_name, "spreads_1st_5_innings")

        # F5 total
        f5_total_pt, f5_over_pr  = _best_total(pbks, "Over",  "totals_1st_5_innings")
        _,           f5_under_pr = _best_total(pbks, "Under", "totals_1st_5_innings")

        # Team totals (full game)
        tt_away_pt, tt_away_ov_pr = _prop_best(pbks, "team_totals", "Over",  away_name)
        _,          tt_away_un_pr = _prop_best(pbks, "team_totals", "Under", away_name)
        tt_home_pt, tt_home_ov_pr = _prop_best(pbks, "team_totals", "Over",  home_name)
        _,          tt_home_un_pr = _prop_best(pbks, "team_totals", "Under", home_name)

        # Team totals (F5)
        f5tt_away_pt, f5tt_away_ov_pr = _prop_best(pbks, "team_totals_1st_5_innings", "Over",  away_name)
        _,            f5tt_away_un_pr = _prop_best(pbks, "team_totals_1st_5_innings", "Under", away_name)
        f5tt_home_pt, f5tt_home_ov_pr = _prop_best(pbks, "team_totals_1st_5_innings", "Over",  home_name)
        _,            f5tt_home_un_pr = _prop_best(pbks, "team_totals_1st_5_innings", "Under", home_name)

        # Pitcher props — preserve any existing annotation results
        existing_pitchers_by_name = {
            p["name"].lower(): p for p in existing_rec.get("pitchers", [])
        }
        pitchers = _build_pitcher_props(pbks, existing_pitchers_by_name)

        rec = {
            # Game identity
            "date":          date_str,
            "away":          away_name,
            "away_code":     away_code,
            "home":          home_name,
            "home_code":     home_code,
            "game_time_utc": commence,
            # Pitchers
            "away_pitcher":       away_sp.get("name", "TBD"),
            "away_pitcher_hand":  away_sp.get("hand", ""),
            "away_pitcher_id":    away_sp.get("mlbam_id"),
            "home_pitcher":       home_sp.get("name", "TBD"),
            "home_pitcher_hand":  home_sp.get("hand", ""),
            "home_pitcher_id":    home_sp.get("mlbam_id"),
            # Full-game odds
            "ml_away":           _best_price(bks, "h2h", away_name),
            "ml_home":           _best_price(bks, "h2h", home_name),
            "spread_away_line":  sp_away_pt,
            "spread_away_price": sp_away_pr,
            "spread_home_line":  sp_home_pt,
            "spread_home_price": sp_home_pr,
            "total_line":        over_pt,
            "over_price":        over_pr,
            "under_price":       under_pr,
            # F5 odds
            "f5_ml_away":           f5_ml_away,
            "f5_ml_home":           f5_ml_home,
            "f5_spread_away_line":  f5_sp_away_pt,
            "f5_spread_away_price": f5_sp_away_pr,
            "f5_spread_home_line":  f5_sp_home_pt,
            "f5_spread_home_price": f5_sp_home_pr,
            "f5_total_line":        f5_total_pt,
            "f5_over_price":        f5_over_pr,
            "f5_under_price":       f5_under_pr,
            # Team total odds (full game)
            "tt_away_line":        tt_away_pt,
            "tt_away_over_price":  tt_away_ov_pr,
            "tt_away_under_price": tt_away_un_pr,
            "tt_home_line":        tt_home_pt,
            "tt_home_over_price":  tt_home_ov_pr,
            "tt_home_under_price": tt_home_un_pr,
            # Team total odds (F5)
            "f5_tt_away_line":        f5tt_away_pt,
            "f5_tt_away_over_price":  f5tt_away_ov_pr,
            "f5_tt_away_under_price": f5tt_away_un_pr,
            "f5_tt_home_line":        f5tt_home_pt,
            "f5_tt_home_over_price":  f5tt_home_ov_pr,
            "f5_tt_home_under_price": f5tt_home_un_pr,
            # Pitcher props (list, one entry per pitcher)
            "pitchers":        pitchers,
            # Meta
            "odds_recorded_at": odds_recorded_at,
            "odds_final":       game_started,
            # Full-game results (filled by --annotate)
            "away_score":  existing_rec.get("away_score"),
            "home_score":  existing_rec.get("home_score"),
            "total_runs":  existing_rec.get("total_runs"),
            "home_win":    existing_rec.get("home_win"),
            "over_hit":    existing_rec.get("over_hit"),
            # F5 results
            "away_f5_score":        existing_rec.get("away_f5_score"),
            "home_f5_score":        existing_rec.get("home_f5_score"),
            "f5_total_runs":        existing_rec.get("f5_total_runs"),
            "f5_home_win":          existing_rec.get("f5_home_win"),
            "f5_over_hit":          existing_rec.get("f5_over_hit"),
            "f5_spread_away_covered": existing_rec.get("f5_spread_away_covered"),
            # Team total results
            "tt_away_over_hit":    existing_rec.get("tt_away_over_hit"),
            "tt_home_over_hit":    existing_rec.get("tt_home_over_hit"),
            "f5_tt_away_over_hit": existing_rec.get("f5_tt_away_over_hit"),
            "f5_tt_home_over_hit": existing_rec.get("f5_tt_home_over_hit"),
            # Spread cover result
            "spread_away_covered": existing_rec.get("spread_away_covered"),
            # Annotation timestamp
            "annotated_at": existing_rec.get("annotated_at"),
        }

        by_game[key] = rec
        written += 1

    records = list(by_game.values())
    if records:
        records.sort(key=lambda r: r.get("game_time_utc") or "")
        _write_json(hist_path, records)
        final_count = sum(1 for r in records if r.get("odds_final"))
        print(f"[history] {date_str}: {written} updated, {final_count} final, "
              f"{len(records)} total — history/{date_str}.json")

    return written


# ---------------------------------------------------------------------------
# Fetch results from MLB API
# ---------------------------------------------------------------------------

def _fetch_pitcher_stats(game_pk: int) -> dict:
    """
    Fetch pitcher K and outs totals from /game/{gamePk}/boxscore.
    Returns {pitcher_name_lower: {ks, outs}} or {} on failure.
    """
    try:
        import requests
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=15)
        r.raise_for_status()
        bs = r.json()
    except Exception:
        return {}

    stats: dict = {}
    for side in ("away", "home"):
        side_data = bs.get("teams", {}).get(side, {})
        players   = side_data.get("players", {})
        for pid in side_data.get("pitchers", []):
            pdata = players.get(f"ID{pid}") or players.get(str(pid)) or {}
            name  = pdata.get("person", {}).get("fullName", "")
            pstat = pdata.get("stats", {}).get("pitching", {})
            if name and pstat:
                stats[name.lower()] = {
                    "ks":   pstat.get("strikeOuts"),
                    "outs": pstat.get("outs"),
                }
    return stats


def _fetch_game_results(target_date: date) -> dict:
    """
    Query MLB Stats API for final scores, F5 linescores, and pitcher stats.

    Returns {frozenset({away_abbr, home_abbr}): list[dict]} where each dict is:
      Completed:   {away_score, home_score, away_f5_score, home_f5_score,
                    pitcher_stats: {name_lower: {ks, outs}}, game_time}
      Non-playing: {status: "postponed"|..., game_time}
    """
    try:
        import requests
        r = requests.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": target_date.isoformat(),
                "gameType": "R",
                "hydrate": "linescore,team",
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

            key       = frozenset([aa, ha])
            game_time = g.get("gameDate", "")
            game_pk   = g.get("gamePk")
            status_obj = g.get("status", {})
            abstract   = status_obj.get("abstractGameState", "")
            detailed   = status_obj.get("detailedState", "")

            if abstract == "Final":
                hs  = teams.get("home", {}).get("score")
                as_ = teams.get("away", {}).get("score")
                if hs is None or as_ is None:
                    continue

                # F5 scores from linescore innings
                away_f5: int | None = None
                home_f5: int | None = None
                innings = g.get("linescore", {}).get("innings", [])
                if len(innings) >= 5:
                    away_f5 = sum(inn.get("away", {}).get("runs") or 0 for inn in innings[:5])
                    home_f5 = sum(inn.get("home", {}).get("runs") or 0 for inn in innings[:5])

                # Pitcher stats from per-game boxscore endpoint
                pitcher_stats = _fetch_pitcher_stats(game_pk) if game_pk else {}

                results.setdefault(key, []).append({
                    "away_score":    int(as_),
                    "home_score":    int(hs),
                    "away_f5_score": away_f5,
                    "home_f5_score": home_f5,
                    "pitcher_stats": pitcher_stats,
                    "game_time":     game_time,
                })

            elif detailed in _NON_PLAYING:
                results.setdefault(key, []).append({
                    "status":    detailed.lower(),
                    "game_time": game_time,
                })

    return results


def _over_under(actual, line):
    """True=over, False=under, None=push, or None if data missing."""
    if actual is None or line is None:
        return None
    if actual > line:
        return True
    if actual < line:
        return False
    return None  # push


def _covers(team_score, spread_line, opponent_score):
    """True=covered, False=didn't, None=push."""
    if team_score is None or spread_line is None or opponent_score is None:
        return None
    adj = team_score + spread_line
    if adj > opponent_score:
        return True
    if adj < opponent_score:
        return False
    return None


# ---------------------------------------------------------------------------
# Annotate
# ---------------------------------------------------------------------------

def annotate_results(history_dir: Path, target_date: date) -> int:
    """
    Fill in final scores and results for all markets in history/{date}.json.
    Returns count of newly annotated games.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    hist_path = history_dir / f"{date_str}.json"
    records: list[dict] = _read_json(hist_path) or []

    unannotated = [r for r in records if not r.get("annotated_at")]
    if not unannotated:
        print(f"[annotate] {date_str}: all games already annotated")
        return 0

    game_results = _fetch_game_results(target_date)
    if not game_results:
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

        candidates = game_results.get(frozenset([away_mlb, home_mlb]))
        if not candidates:
            continue

        result = _pick_best_result(candidates, rec.get("game_time_utc", ""))

        # Postponed / cancelled / suspended
        if result.get("status"):
            rec["status"] = result["status"]
            rec["annotated_at"] = now
            annotated += 1
            print(f"  {rec['away']} @ {rec['home']} → {result['status'].upper()}")
            continue

        away_score = result["away_score"]
        home_score = result["home_score"]
        away_f5    = result.get("away_f5_score")
        home_f5    = result.get("home_f5_score")
        pitcher_stats = result.get("pitcher_stats", {})

        # Full-game results
        total_runs = away_score + home_score
        rec["away_score"]        = away_score
        rec["home_score"]        = home_score
        rec["total_runs"]        = total_runs
        rec["home_win"]          = home_score > away_score
        rec["over_hit"]          = _over_under(total_runs, rec.get("total_line"))
        rec["spread_away_covered"] = _covers(away_score, rec.get("spread_away_line"), home_score)

        # F5 results
        if away_f5 is not None and home_f5 is not None:
            f5_total = away_f5 + home_f5
            rec["away_f5_score"]          = away_f5
            rec["home_f5_score"]          = home_f5
            rec["f5_total_runs"]          = f5_total
            rec["f5_home_win"]            = home_f5 > away_f5
            rec["f5_over_hit"]            = _over_under(f5_total, rec.get("f5_total_line"))
            rec["f5_spread_away_covered"] = _covers(away_f5, rec.get("f5_spread_away_line"), home_f5)
            rec["f5_tt_away_over_hit"]    = _over_under(away_f5, rec.get("f5_tt_away_line"))
            rec["f5_tt_home_over_hit"]    = _over_under(home_f5, rec.get("f5_tt_home_line"))

        # Team total results (full game — always available from final scores)
        rec["tt_away_over_hit"] = _over_under(away_score, rec.get("tt_away_line"))
        rec["tt_home_over_hit"] = _over_under(home_score, rec.get("tt_home_line"))

        # Pitcher stats
        pitchers = rec.get("pitchers", [])
        for p in pitchers:
            pname = p.get("name", "")
            stats = pitcher_stats.get(pname.lower())
            if stats is None:
                # Fallback: match on last name
                last = pname.split()[-1].lower() if pname else ""
                for k, v in pitcher_stats.items():
                    if last and k.split()[-1] == last:
                        stats = v
                        break
            if stats:
                actual_ks   = stats.get("ks")
                actual_outs = stats.get("outs")
                p["actual_ks"]    = actual_ks
                p["actual_outs"]  = actual_outs
                p["k_over_hit"]   = _over_under(actual_ks,   p.get("k_line"))
                p["outs_over_hit"]= _over_under(actual_outs, p.get("outs_line"))

        rec["annotated_at"] = now
        annotated += 1

        f5_str = f", F5 {away_f5}-{home_f5}" if away_f5 is not None else ""
        print(f"  {rec['away']} {away_score} @ {rec['home']} {home_score}"
              f"  (total {total_runs}, line {rec.get('total_line')} → "
              f"{'OVER' if rec.get('over_hit') else 'UNDER' if rec.get('over_hit') is False else 'PUSH'}"
              f"{f5_str})")

    if annotated:
        _write_json(hist_path, records)
        print(f"[annotate] {date_str}: annotated {annotated} game(s)")
    else:
        pending = [f"{r['away']} @ {r['home']}" for r in records if not r.get("annotated_at")]
        if pending:
            print(f"[annotate] {date_str}: no new results (still pending: {', '.join(pending)})")
        else:
            print(f"[annotate] {date_str}: all games already annotated")

    return annotated


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MLB odds history tool")
    ap.add_argument("--save",     action="store_true", help="Save today's odds to history/")
    ap.add_argument("--annotate", action="store_true", help="Annotate results for completed games")
    ap.add_argument("--date", default="today", help="today, yesterday, or YYYY-MM-DD")
    ap.add_argument("--data-dir",    default="./data",    help="Data directory")
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
