"""Odds API parsing — extract best prices from bookmaker data, format for display."""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from teams import ODDS_TEAM


def load_odds(data_dir: Path, target_date: date) -> dict:
    """Load bulk odds JSON; returns {(away_name, home_name): [game_dict, ...]}.

    Doubleheaders produce two events with identical team names but different
    commence_time — both are kept in the list; see pick_odds_by_time().
    """
    p = data_dir / f"odds_{target_date.strftime('%Y-%m-%d')}.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    result: dict = {}
    for g in raw:
        if not isinstance(g, dict):
            continue
        result.setdefault((g.get("away_team", ""), g.get("home_team", "")), []).append(g)
    return result


def pick_odds_by_time(candidates: list, game_time_utc: str) -> Optional[dict]:
    """Disambiguate doubleheader legs: pick the candidate whose commence_time is
    closest to game_time_utc. Falls back to the first candidate if there's no
    usable time to compare against.
    """
    if not candidates:
        return None
    if len(candidates) == 1 or not game_time_utc:
        return candidates[0]
    try:
        target = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
    except Exception:
        return candidates[0]

    def _dist(c):
        try:
            return abs((datetime.fromisoformat(
                c.get("commence_time", "").replace("Z", "+00:00")) - target
            ).total_seconds())
        except Exception:
            return float("inf")

    return min(candidates, key=_dist)


# ── Best-price extraction ─────────────────────────────────────────────────────

def _best_price(bookmakers: list, market_key: str, outcome_name: str) -> Optional[float]:
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


def _best_spread(bookmakers: list, outcome_name: str, market_key: str = "spreads") -> tuple:
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


def _best_total(bookmakers: list, side: str, market_key: str = "totals") -> tuple:
    """Return (point, price) for the best-priced over or under on the given market."""
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


def _best_team_total(bookmakers: list, team_name: str, side: str,
                     market_key: str = "team_totals") -> tuple:
    """Return (point, price) for the best-priced team total over/under for a team."""
    best_price, best_point = None, None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name") == side and oc.get("description") == team_name:
                    p, pt = oc.get("price"), oc.get("point")
                    if p is not None and (best_price is None or p > best_price):
                        best_price, best_point = p, pt
    return best_point, best_price


def _find_prop_line(bookmakers: list, pitcher_name: str, market_key: str) -> Optional[dict]:
    """Find best-priced over/under line for a named pitcher on a given prop market."""
    if not pitcher_name or pitcher_name in ("TBD", ""):
        return None
    last = pitcher_name.strip().split()[-1].lower()
    best_over: Optional[dict] = None
    best_under: Optional[dict] = None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                # Odds API player props: name="Over"/"Under", description=pitcher name
                pitcher_field = (oc.get("description") or oc.get("name") or "").lower()
                if last not in pitcher_field:
                    continue
                side = (oc.get("name") or oc.get("description") or "").lower()
                p, pt = oc.get("price"), oc.get("point")
                if "over" in side:
                    if p is not None and (best_over is None or p > best_over["price"]):
                        best_over = {"point": pt, "price": p}
                elif "under" in side:
                    if p is not None and (best_under is None or p > best_under["price"]):
                        best_under = {"point": pt, "price": p}
    if best_over is None:
        return None
    return {
        "point": best_over["point"],
        "over":  best_over["price"],
        "under": best_under["price"] if best_under else None,
    }


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_ml(price) -> str:
    if price is None:
        return "—"
    return f"+{int(price)}" if price > 0 else str(int(price))


def fmt_spread(point, price) -> str:
    if point is None:
        return "—"
    pt = f"+{point}" if point >= 0 else str(point)
    pr = f"+{int(price)}" if price > 0 else str(int(price))
    return f"{pt} ({pr})"


def fmt_total(side: str, point, price) -> str:
    if point is None:
        return "—"
    pr = f"+{int(price)}" if price > 0 else str(int(price))
    return f"{side}{point} ({pr})"


def fmt_k_line(k: Optional[dict]) -> str:
    """Format pitcher K O/U as 'K O/U 5.5 (-115 / -105)'."""
    if not k or k.get("point") is None:
        return ""
    pt = k["point"]
    op = fmt_ml(k.get("over"))
    up = fmt_ml(k.get("under"))
    return f"K O/U {pt} ({op} / {up})"


def fmt_outs_line(o: Optional[dict]) -> str:
    """Format pitcher outs O/U as 'Outs O/U 17.5 (-120 / +100)'."""
    if not o or o.get("point") is None:
        return ""
    pt = o["point"]
    op = fmt_ml(o.get("over"))
    up = fmt_ml(o.get("under"))
    return f"Outs O/U {pt} ({op} / {up})"


# ── Game-level odds assembly ──────────────────────────────────────────────────

def get_game_odds(odds_data: dict, away_code: str, home_code: str,
                  away_sp_name: str = "", home_sp_name: str = "",
                  props_data: Optional[dict] = None,
                  game_time_utc: str = "") -> Optional[dict]:
    """Assemble all odds/props into a unified dict for a single game.

    game_time_utc (typically MLB's scheduled start time) disambiguates doubleheader
    legs, since both share the same away/home team names in the Odds API.
    """
    away_name = ODDS_TEAM.get(away_code, "")
    home_name = ODDS_TEAM.get(home_code, "")
    game = pick_odds_by_time(odds_data.get((away_name, home_name), []), game_time_utc)
    if not game:
        return None
    bks = game.get("bookmakers", [])

    # Full-game
    away_sp_pt, away_sp_pr = _best_spread(bks, away_name)
    home_sp_pt, home_sp_pr = _best_spread(bks, home_name)
    over_pt,  over_pr  = _best_total(bks, "Over")
    under_pt, under_pr = _best_total(bks, "Under")

    # Per-event data (pitcher props + F5 odds — all from per-event endpoint)
    prop_event = pick_odds_by_time(
        (props_data or {}).get((away_name, home_name), []), game_time_utc)
    prop_bks = (prop_event or {}).get("bookmakers", [])
    away_k    = _find_prop_line(prop_bks, away_sp_name, "pitcher_strikeouts")
    home_k    = _find_prop_line(prop_bks, home_sp_name, "pitcher_strikeouts")
    away_outs = _find_prop_line(prop_bks, away_sp_name, "pitcher_outs")
    home_outs = _find_prop_line(prop_bks, home_sp_name, "pitcher_outs")

    # F5 (from per-event endpoint — not available on free bulk endpoint)
    away_f5_sp_pt, away_f5_sp_pr = _best_spread(prop_bks, away_name, "spreads_1st_5_innings")
    home_f5_sp_pt, home_f5_sp_pr = _best_spread(prop_bks, home_name, "spreads_1st_5_innings")
    f5_over_pt, f5_over_pr   = _best_total(prop_bks, "Over",  "totals_1st_5_innings")
    f5_under_pt, f5_under_pr = _best_total(prop_bks, "Under", "totals_1st_5_innings")
    has_f5 = any(v is not None for v in (
        away_f5_sp_pt, f5_over_pt,
        _best_price(prop_bks, "h2h_1st_5_innings", away_name),
    ))

    # Team totals — full game
    away_tt_ov_pt, away_tt_ov_pr = _best_team_total(prop_bks, away_name, "Over")
    away_tt_un_pt, away_tt_un_pr = _best_team_total(prop_bks, away_name, "Under")
    home_tt_ov_pt, home_tt_ov_pr = _best_team_total(prop_bks, home_name, "Over")
    home_tt_un_pt, home_tt_un_pr = _best_team_total(prop_bks, home_name, "Under")
    has_tt = away_tt_ov_pt is not None or home_tt_ov_pt is not None

    # Team totals — F5
    _f5tt = "team_totals_1st_5_innings"
    away_f5tt_ov_pt, away_f5tt_ov_pr = _best_team_total(prop_bks, away_name, "Over",  _f5tt)
    away_f5tt_un_pt, away_f5tt_un_pr = _best_team_total(prop_bks, away_name, "Under", _f5tt)
    home_f5tt_ov_pt, home_f5tt_ov_pr = _best_team_total(prop_bks, home_name, "Over",  _f5tt)
    home_f5tt_un_pt, home_f5tt_un_pr = _best_team_total(prop_bks, home_name, "Under", _f5tt)
    has_f5tt = away_f5tt_ov_pt is not None or home_f5tt_ov_pt is not None

    return {
        # Full game
        "away_ml":     fmt_ml(_best_price(bks, "h2h", away_name)),
        "home_ml":     fmt_ml(_best_price(bks, "h2h", home_name)),
        "away_spread": fmt_spread(away_sp_pt, away_sp_pr),
        "home_spread": fmt_spread(home_sp_pt, home_sp_pr),
        "over":        fmt_total("O", over_pt,  over_pr),
        "under":       fmt_total("U", under_pt, under_pr),
        # F5
        "has_f5":         has_f5,
        "away_f5_ml":     fmt_ml(_best_price(prop_bks, "h2h_1st_5_innings", away_name)),
        "home_f5_ml":     fmt_ml(_best_price(prop_bks, "h2h_1st_5_innings", home_name)),
        "away_f5_spread": fmt_spread(away_f5_sp_pt, away_f5_sp_pr),
        "home_f5_spread": fmt_spread(home_f5_sp_pt, home_f5_sp_pr),
        "f5_over":        fmt_total("O", f5_over_pt, f5_over_pr),
        "f5_under":       fmt_total("U", f5_under_pt, f5_under_pr),
        # Team totals — full game
        "has_tt":       has_tt,
        "away_tt_over":  fmt_total("O", away_tt_ov_pt, away_tt_ov_pr),
        "away_tt_under": fmt_total("U", away_tt_un_pt, away_tt_un_pr),
        "home_tt_over":  fmt_total("O", home_tt_ov_pt, home_tt_ov_pr),
        "home_tt_under": fmt_total("U", home_tt_un_pt, home_tt_un_pr),
        # Team totals — F5
        "has_f5tt":          has_f5tt,
        "away_f5tt_over":    fmt_total("O", away_f5tt_ov_pt, away_f5tt_ov_pr),
        "away_f5tt_under":   fmt_total("U", away_f5tt_un_pt, away_f5tt_un_pr),
        "home_f5tt_over":    fmt_total("O", home_f5tt_ov_pt, home_f5tt_ov_pr),
        "home_f5tt_under":   fmt_total("U", home_f5tt_un_pt, home_f5tt_un_pr),
        # Pitcher props
        "away_k":    away_k,
        "home_k":    home_k,
        "away_outs": away_outs,
        "home_outs": home_outs,
    }
