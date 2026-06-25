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
import csv
import json
import re
import sys
from datetime import date, timedelta, datetime, timezone

_ET = timezone(timedelta(hours=-4))  # EDT (UTC-4); correct for MLB season Apr-Oct
from pathlib import Path
from typing import Optional

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ── Team code normalization ───────────────────────────────────────────────────
# Starters CSV uses codes that differ slightly from team_stats CSVs and MLB API
_STATS_MAP = {"CHW": "CWS", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB", "WSN": "WSH"}
_MLB_MAP = {**_STATS_MAP, "ARI": "AZ"}  # MLB API uses "AZ" for Diamondbacks; ATH stays as-is

# ESPN CDN logo codes (keyed by Handigraphs team codes)
_LOGO = {
    "ARI": "ari", "ATH": "oak", "ATL": "atl", "BAL": "bal", "BOS": "bos",
    "CHC": "chc", "CHW": "cws", "CIN": "cin", "CLE": "cle", "COL": "col",
    "DET": "det", "HOU": "hou", "KCR": "kc",  "LAA": "laa", "LAD": "lad",
    "MIA": "mia", "MIL": "mil", "MIN": "min", "NYM": "nym", "NYY": "nyy",
    "PHI": "phi", "PIT": "pit", "SDP": "sd",  "SEA": "sea", "SFG": "sf",
    "STL": "stl", "TBR": "tb",  "TEX": "tex", "TOR": "tor", "WSN": "wsh",
}

def _logo_img(team: str) -> str:
    code = _LOGO.get(team, team.lower())
    url = f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/mlb/500/{code}.png&h=28&w=28"
    return f'<img src="{url}" class="tm-logo" alt="{team}" onerror="this.style.display=\'none\'">'

def to_stats(t: str) -> str:
    return _STATS_MAP.get(t, t)

def to_mlb(t: str) -> str:
    return _MLB_MAP.get(t, t)

# Odds API team names (keyed by Handigraphs team codes)
_ODDS_TEAM = {
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
# Reverse mapping: MLB API team name → Handigraphs code
_MLB_NAME_TO_CODE: dict[str, str] = {v: k for k, v in _ODDS_TEAM.items()}
_MLB_NAME_TO_CODE.update({
    "Oakland Athletics": "ATH",  # pre-relocation name still used in some MLB API responses
})

def load_odds(data_dir: Path, target_date: date) -> dict:
    """Load Odds API JSON; returns {(away_name, home_name): game_dict}."""
    p = data_dir / f"odds_{target_date.strftime('%Y-%m-%d')}.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {(g["away_team"], g["home_team"]): g for g in raw if isinstance(g, dict)}

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
    """Return (point, price) for the best-priced team total over or under for the given team."""
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
                # Some books flip it, so check both fields for the pitcher name
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

def _fmt_ml(price) -> str:
    if price is None: return "—"
    return f"+{int(price)}" if price > 0 else str(int(price))

def _fmt_spread(point, price) -> str:
    if point is None: return "—"
    pt = f"+{point}" if point >= 0 else str(point)
    pr = f"+{int(price)}" if price > 0 else str(int(price))
    return f"{pt} ({pr})"

def _fmt_total(side: str, point, price) -> str:
    if point is None: return "—"
    pr = f"+{int(price)}" if price > 0 else str(int(price))
    return f"{side}{point} ({pr})"

def _fmt_k_line(k: Optional[dict]) -> str:
    """Format pitcher K O/U as 'K O/U 5.5 (-115 / -105)'."""
    if not k or k.get("point") is None:
        return ""
    pt = k["point"]
    op = _fmt_ml(k.get("over"))
    up = _fmt_ml(k.get("under"))
    return f"K O/U {pt} ({op} / {up})"

def _fmt_outs_line(o: Optional[dict]) -> str:
    """Format pitcher outs O/U as 'Outs O/U 17.5 (-120 / +100)'."""
    if not o or o.get("point") is None:
        return ""
    pt = o["point"]
    op = _fmt_ml(o.get("over"))
    up = _fmt_ml(o.get("under"))
    return f"Outs O/U {pt} ({op} / {up})"

def get_game_odds(odds_data: dict, away_code: str, home_code: str,
                  away_sp_name: str = "", home_sp_name: str = "",
                  props_data: Optional[dict] = None) -> Optional[dict]:
    away_name = _ODDS_TEAM.get(away_code, "")
    home_name = _ODDS_TEAM.get(home_code, "")
    game = odds_data.get((away_name, home_name))
    if not game:
        return None
    bks = game.get("bookmakers", [])

    # Full-game
    away_sp_pt, away_sp_pr = _best_spread(bks, away_name)
    home_sp_pt, home_sp_pr = _best_spread(bks, home_name)
    over_pt, over_pr   = _best_total(bks, "Over")
    under_pt, under_pr = _best_total(bks, "Under")

    # Per-event data (pitcher props + F5 odds — all from per-event endpoint)
    prop_bks = (props_data or {}).get((away_name, home_name), [])
    away_k    = _find_prop_line(prop_bks, away_sp_name, "pitcher_strikeouts")
    home_k    = _find_prop_line(prop_bks, home_sp_name, "pitcher_strikeouts")
    away_outs = _find_prop_line(prop_bks, away_sp_name, "pitcher_outs")
    home_outs = _find_prop_line(prop_bks, home_sp_name, "pitcher_outs")

    # F5 — from per-event endpoint (not available on free bulk endpoint)
    away_f5_sp_pt, away_f5_sp_pr = _best_spread(prop_bks, away_name, "spreads_1st_5_innings")
    home_f5_sp_pt, home_f5_sp_pr = _best_spread(prop_bks, home_name, "spreads_1st_5_innings")
    f5_over_pt, f5_over_pr   = _best_total(prop_bks, "Over",  "totals_1st_5_innings")
    f5_under_pt, f5_under_pr = _best_total(prop_bks, "Under", "totals_1st_5_innings")
    has_f5 = any(v is not None for v in (away_f5_sp_pt, f5_over_pt,
                                          _best_price(prop_bks, "h2h_1st_5_innings", away_name)))

    # Team totals — full game (from per-event endpoint)
    away_tt_ov_pt, away_tt_ov_pr = _best_team_total(prop_bks, away_name, "Over")
    away_tt_un_pt, away_tt_un_pr = _best_team_total(prop_bks, away_name, "Under")
    home_tt_ov_pt, home_tt_ov_pr = _best_team_total(prop_bks, home_name, "Over")
    home_tt_un_pt, home_tt_un_pr = _best_team_total(prop_bks, home_name, "Under")
    has_tt = away_tt_ov_pt is not None or home_tt_ov_pt is not None

    # Team totals — F5 (from per-event endpoint)
    _f5tt = "team_totals_1st_5_innings"
    away_f5tt_ov_pt, away_f5tt_ov_pr = _best_team_total(prop_bks, away_name, "Over",  _f5tt)
    away_f5tt_un_pt, away_f5tt_un_pr = _best_team_total(prop_bks, away_name, "Under", _f5tt)
    home_f5tt_ov_pt, home_f5tt_ov_pr = _best_team_total(prop_bks, home_name, "Over",  _f5tt)
    home_f5tt_un_pt, home_f5tt_un_pr = _best_team_total(prop_bks, home_name, "Under", _f5tt)
    has_f5tt = away_f5tt_ov_pt is not None or home_f5tt_ov_pt is not None

    return {
        # Full game
        "away_ml":       _fmt_ml(_best_price(bks, "h2h", away_name)),
        "home_ml":       _fmt_ml(_best_price(bks, "h2h", home_name)),
        "away_spread":   _fmt_spread(away_sp_pt, away_sp_pr),
        "home_spread":   _fmt_spread(home_sp_pt, home_sp_pr),
        "over":          _fmt_total("O", over_pt,  over_pr),
        "under":         _fmt_total("U", under_pt, under_pr),
        # F5
        "has_f5":        has_f5,
        "away_f5_ml":    _fmt_ml(_best_price(prop_bks, "h2h_1st_5_innings", away_name)),
        "home_f5_ml":    _fmt_ml(_best_price(prop_bks, "h2h_1st_5_innings", home_name)),
        "away_f5_spread":_fmt_spread(away_f5_sp_pt, away_f5_sp_pr),
        "home_f5_spread":_fmt_spread(home_f5_sp_pt, home_f5_sp_pr),
        "f5_over":       _fmt_total("O", f5_over_pt, f5_over_pr),
        "f5_under":      _fmt_total("U", f5_under_pt, f5_under_pr),
        # Team totals — full game
        "has_tt":         has_tt,
        "away_tt_over":   _fmt_total("O", away_tt_ov_pt, away_tt_ov_pr),
        "away_tt_under":  _fmt_total("U", away_tt_un_pt, away_tt_un_pr),
        "home_tt_over":   _fmt_total("O", home_tt_ov_pt, home_tt_ov_pr),
        "home_tt_under":  _fmt_total("U", home_tt_un_pt, home_tt_un_pr),
        # Team totals — F5
        "has_f5tt":           has_f5tt,
        "away_f5tt_over":     _fmt_total("O", away_f5tt_ov_pt, away_f5tt_ov_pr),
        "away_f5tt_under":    _fmt_total("U", away_f5tt_un_pt, away_f5tt_un_pr),
        "home_f5tt_over":     _fmt_total("O", home_f5tt_ov_pt, home_f5tt_ov_pr),
        "home_f5tt_under":    _fmt_total("U", home_f5tt_un_pt, home_f5tt_un_pr),
        # Pitcher props
        "away_k":        away_k,
        "home_k":        home_k,
        "away_outs":     away_outs,
        "home_outs":     home_outs,
    }


# ── File finding ─────────────────────────────────────────────────────────────
def _find_file(data_dir: Path, prefix: str, target_date: date, ext: str) -> Optional[Path]:
    ds = target_date.strftime("%Y-%m-%d")
    for p in data_dir.glob(f"{prefix}*{ds}*.{ext}"):
        return p
    return None

# ── CSV loading (fallback) ────────────────────────────────────────────────────
def _load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        seen: dict[str, int] = {}
        clean = []
        for h in headers:
            if h in seen:
                seen[h] += 1
                clean.append(f"{h}_{seen[h]}")
            else:
                seen[h] = 0
                clean.append(h)
        return [dict(zip(clean, row)) for row in reader]

# ── JSON loading (primary) ────────────────────────────────────────────────────
def _load_starters_json(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    rows = raw.get("starters", raw) if isinstance(raw, dict) else raw
    result = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        s = p.get("stats") or {}
        result.append({
            "Name":         p.get("name", ""),
            "Throws":       p.get("throws", ""),
            "Team":         p.get("team", ""),
            "Opponent":     p.get("opponent", ""),
            "mlbam_id":     p.get("mlbam_id") or p.get("id"),
            "lineup_status": p.get("lineup_status", ""),
            "IP":           s.get("ip"),
            "TBF":          s.get("tbf"),
            "ERA":          s.get("era"),
            "xERA":         s.get("xera"),
            "FIP":          s.get("fip"),
            "xFIP":         s.get("xfip"),
            "K-BB%":        s.get("k_bb_pct"),
            "CSW%":         s.get("csw_pct"),
            "K%":           s.get("k_pct"),
            "BB%":          s.get("bb_pct"),
            "SwStr%":       s.get("swstr_pct"),
            "Whiff%":       s.get("whiff_pct"),
            "O-Swing%":     s.get("o_swing_pct"),
            "Zone%":        s.get("zone_pct"),
            "FPS%":         s.get("fps_pct"),
            "Avg EV":       s.get("avg_ev"),
            "Hard-Hit%":    s.get("hard_hit_pct"),
            "Barrel%":      s.get("barrel_pct"),
            "HR/9":         s.get("hr_per_9"),
            "GB%":          s.get("gb_pct"),
            "FB%":          s.get("fb_pct"),
            "LD%":          s.get("ld_pct"),
            "xBA":          s.get("xba"),
            "xSLG":         s.get("xslg"),
            "wOBA":         s.get("woba"),
            "xwOBA":        s.get("xwoba"),
            "BABIP (ag)":   s.get("babip_ag"),
            "ISO (ag)":     s.get("iso_ag"),
            "SLG (ag)":     s.get("slg_ag"),
            "WHIP":         s.get("whip"),
            "LOB%":         s.get("lob_pct"),
            "Outs/GS":      s.get("outs_per_gs"),
            "Pitches/PA":   s.get("pitches_per_pa"),
            "H":            s.get("h") or s.get("hit_cnt"),
            "Games":        s.get("games"),
        })
    return [r for r in result if r.get("Name")]

def _team_stats_entry(r: dict) -> dict:
    return {
        "Team":      r.get("team", ""),
        "wRC+":      r.get("wrc_plus"),
        "wOBA":      r.get("woba"),
        "BABIP":     r.get("babip"),
        "OPS":       r.get("ops"),
        "ISO":       r.get("iso"),
        "GB/FB":     r.get("gb_fb"),
        "K%":        r.get("k_perc") or r.get("k_pct"),
        "BB%":       r.get("bb_perc") or r.get("bb_pct"),
        "HardHit%":  r.get("hard_perc"),
        "FB%":       r.get("fb_perc"),
        "LD%":       r.get("ld_perc"),
        "GB%":       r.get("gb_perc"),
    }

def _load_team_stats_json(path: Path) -> dict:
    raw = json.loads(path.read_text())
    rows = raw if isinstance(raw, list) else raw.get("data", [])
    result = {}
    for r in rows:
        team = r.get("team", "")
        if not team:
            continue
        entry = _team_stats_entry(r)
        result[team] = entry
        norm = to_stats(team)
        if norm != team:
            result[norm] = entry
    return result

def _load_bullpen_json(path: Path) -> dict:
    raw = json.loads(path.read_text())
    rows = raw if isinstance(raw, list) else raw.get("data", [])
    result = {}
    for r in rows:
        team = r.get("team", "")
        if not team:
            continue
        entry = {
            "Team":  team,
            "ERA":   r.get("era"),
            "xERA":  r.get("xera"),
            "FIP":   r.get("fip"),
            "xFIP":  r.get("xfip"),
            "K%":    r.get("k_perc") or r.get("k_pct"),
            "BB%":   r.get("bb_perc") or r.get("bb_pct"),
            "BABIP": r.get("babip"),
            "wOBA":  r.get("woba"),
            "SwStr%": r.get("swstr_pct"),
            "CSW%":  r.get("csw_pct"),
            "Hard%":   r.get("hard_hit_pct") or r.get("hard_contact_pct"),
            "Barrel%": r.get("barrel_pct"),
            "GB%":     r.get("gb_pct") or r.get("ground_ball_pct"),
            "FB%":   r.get("fb_pct") or r.get("fly_ball_pct"),
            "LD%":   r.get("ld_pct") or r.get("line_drive_pct"),
            "HR/9":  r.get("hr_per_9") or r.get("hr_per_nine"),
        }
        result[team] = entry
        norm = to_stats(team)
        if norm != team:
            result[norm] = entry
    return result

# ── Public loaders (JSON primary, CSV fallback) ───────────────────────────────
def load_starters(data_dir: Path, target_date: date) -> list[dict]:
    p = _find_file(data_dir, "starters_last3g", target_date, "json")
    if p:
        return _load_starters_json(p)
    p = _find_file(data_dir, "starters_last3g", target_date, "csv")
    if p:
        return [r for r in _load_csv(p) if r.get("Name", "").strip()]
    sys.exit(f"ERROR: No starters data in {data_dir} for {target_date}. Run with --refresh.")

def load_team_stats(data_dir: Path, target_date: date) -> tuple[dict, dict]:
    rj = _find_file(data_dir, "team_stats_L12RHP", target_date, "json")
    lj = _find_file(data_dir, "team_stats_L12LHP", target_date, "json")
    if rj and lj:
        return _load_team_stats_json(rj), _load_team_stats_json(lj)
    rp = _find_file(data_dir, "team_stats_L12RHP", target_date, "csv")
    lp = _find_file(data_dir, "team_stats_L12LHP", target_date, "csv")
    if rp and lp:
        return ({r["Team"]: r for r in _load_csv(rp)},
                {r["Team"]: r for r in _load_csv(lp)})
    sys.exit(f"ERROR: Missing team stats data in {data_dir} for {target_date}.")

def load_bullpen(data_dir: Path, target_date: date) -> dict:
    p = _find_file(data_dir, "bullpen_stats_last12g", target_date, "json")
    if p:
        return _load_bullpen_json(p)
    p = _find_file(data_dir, "bullpen_stats_last12g", target_date, "csv")
    if p:
        return {r["Team"]: r for r in _load_csv(p)}
    sys.exit(f"ERROR: No bullpen data in {data_dir} for {target_date}.")

def load_odds_meta(data_dir: Path, target_date: date) -> str:
    """Return odds fetch timestamp as a UTC ISO string, or '' if not found."""
    p = data_dir / f"odds_meta_{target_date.strftime('%Y-%m-%d')}.json"
    if not p.exists():
        return ""
    try:
        meta = json.loads(p.read_text())
        return meta.get("fetched_at", "")
    except Exception:
        return ""

def load_pitcher_props(data_dir: Path, target_date: date) -> dict:
    """Load per-event pitcher props; returns {(away_team, home_team): bookmakers_list}."""
    p = _find_file(data_dir, "props", target_date, "json")
    if not p:
        return {}
    try:
        raw = json.loads(p.read_text())
        result = {}
        for event_data in raw.values():
            away = event_data.get("away_team", "")
            home = event_data.get("home_team", "")
            if away and home:
                result[(away, home)] = event_data.get("bookmakers", [])
        return result
    except Exception:
        return {}


def load_ballpark_weather(data_dir: Path, target_date: date) -> dict:
    """Returns dict keyed by frozenset({away_team, home_team}) → game weather dict."""
    p = _find_file(data_dir, "ballpark_weather", target_date, "json")
    if not p:
        return {}
    raw = json.loads(p.read_text())
    games = raw.get("games", []) if isinstance(raw, dict) else raw
    result = {}
    for g in games:
        away = g.get("away_team", "")
        home = g.get("home_team", "")
        if away and home:
            result[frozenset([away, home])] = g
    return result


# ── Type helpers ──────────────────────────────────────────────────────────────
def flt(val) -> Optional[float]:
    try:
        return float(str(val).rstrip("%"))
    except (TypeError, ValueError):
        return None

def pct_val(s: str) -> Optional[float]:
    return flt(s.rstrip("%")) if s else None

def fp1(val) -> str:
    """Format a percentage or rate to 1 decimal place (handles float or '22.6%' string)."""
    v = flt(val)
    return f"{v:.1f}" if v is not None else "?"

def fp3(val) -> str:
    """Format a rate to 3 decimal places."""
    v = flt(val)
    return f"{v:.3f}" if v is not None else "?"


# ── Qualitative labels ────────────────────────────────────────────────────────
def wrc_label(v: Optional[float]) -> str:
    if v is None: return ""
    if v >= 130: return "elite"
    if v >= 115: return "above avg"
    if v >= 95:  return "avg"
    if v >= 80:  return "below avg"
    return "poor"

def xera_label(v: Optional[float]) -> str:
    if v is None: return ""
    if v < 3.00: return "elite"
    if v < 3.75: return "good"
    if v < 4.50: return "avg"
    if v < 5.25: return "below avg"
    return "poor"


# ── Game pairing ──────────────────────────────────────────────────────────────
def build_games(starters: list[dict]) -> list[tuple[dict, dict]]:
    by_team = {r["Team"]: r for r in starters if r.get("Team")}
    seen: set[tuple] = set()
    games = []
    for row in starters:
        team = (row.get("Team") or "").strip()
        opp  = (row.get("Opponent") or "").strip()
        if not team or not opp:
            continue
        key = tuple(sorted([team, opp]))
        if key in seen:
            continue
        seen.add(key)
        games.append((row, by_team.get(opp, {"Name": "TBD", "Team": opp, "Throws": "?"})))
    return games


# ── Pitcher flags (from Handigraphs aggregate, last 3 starts) ─────────────────
def pitcher_csv_flags(row: dict) -> list[str]:
    flags = []
    status = row.get("lineup_status", "")
    if status and status not in ("confirmed", "expected", ""):
        flags.append(f"lineup: {status}")

    ip     = flt(row.get("IP"))
    xera   = flt(row.get("xERA"))

    if ip is None and xera is None:
        flags.append("first start of the season — no stats available yet")
        return flags

    if ip is not None and ip < 9:
        flags.append(f"small sample ({ip:.1f} IP over 3 starts) — stats may not reflect true ability")

    hh     = flt(row.get("Hard-Hit%", ""))
    barrel = flt(row.get("Barrel%", ""))
    bb     = flt(row.get("BB%"))
    ogs    = flt(row.get("Outs/GS"))

    if hh is not None and hh > 44:
        flags.append(f"HH% {hh:.0f}% — batters are squaring up the ball at an elevated rate")
    if barrel is not None and barrel > 12:
        flags.append(f"Barrel% {barrel:.0f}% — high hard contact rate, elevated home run risk")
    if bb is not None and bb > 12:
        flags.append(f"BB% {bb:.0f}% — command concerns, elevated walk rate")
    if ogs is not None and (ogs / 3) < 4.0:
        flags.append(f"avg {ogs/3:.1f} IP/gs — short outings, bullpen likely needed early")

    return flags


def bullpen_flags(row: dict) -> list[str]:
    flags = []
    xera = flt(row.get("xERA"))
    if xera is not None and xera > 5.0:
        flags.append(f"bullpen xERA {xera:.2f} — bullpen performing well below average by expected ERA")
    return flags


# ── MLB Stats API ─────────────────────────────────────────────────────────────
MLB_API = "https://statsapi.mlb.com/api/v1"

def get_mlb_schedule(target_date: date) -> dict:
    if not HAS_REQUESTS:
        return {}
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": target_date.isoformat(),
                "hydrate": "probablePitcher,venue,team",
                "gameType": "R",
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"Warning: MLB API unavailable ({e})", file=sys.stderr)
        return {}

    games = {}
    for date_entry in r.json().get("dates", []):
        for g in date_entry.get("games", []):
            teams = g.get("teams", {})
            home  = teams.get("home", {})
            away  = teams.get("away", {})
            ha    = home.get("team", {}).get("abbreviation", "")
            aa    = away.get("team", {}).get("abbreviation", "")
            hp    = home.get("probablePitcher", {})
            ap    = away.get("probablePitcher", {})
            games[frozenset([ha, aa])] = {
                "home": ha, "away": aa,
                "home_mlb_id": home.get("team", {}).get("id"),
                "away_mlb_id": away.get("team", {}).get("id"),
                "venue": g.get("venue", {}).get("name", ""),
                "home_pid": hp.get("id"), "home_pname": hp.get("fullName", ""),
                "away_pid": ap.get("id"), "away_pname": ap.get("fullName", ""),
                "game_date": g.get("gameDate", ""),
            }
    return games


def get_recent_starts(player_id: int) -> list[dict]:
    if not HAS_REQUESTS or not player_id:
        return []
    current_year = datetime.now(_ET).year
    all_splits: list[dict] = []
    for season in [current_year - 1, current_year]:  # oldest first so chronological order is preserved
        try:
            r = requests.get(
                f"{MLB_API}/people/{player_id}/stats",
                params={"stats": "gameLog", "season": season, "group": "pitching"},
                timeout=10,
            )
            r.raise_for_status()
            splits = r.json().get("stats", [{}])[0].get("splits", [])
            all_splits.extend(
                s for s in splits
                if flt(s.get("stat", {}).get("inningsPitched")) is not None
            )
        except Exception:
            pass
    return all_splits


def get_team_schedule(team_id: int, season: int) -> list[dict]:
    """Fetch completed game results for a team in the given season."""
    if not HAS_REQUESTS or not team_id:
        return []
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={"teamId": team_id, "season": season, "sportId": 1, "gameType": "R"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception:
        return []
    results = []
    for date_entry in r.json().get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            teams  = g.get("teams", {})
            home   = teams.get("home", {})
            away   = teams.get("away", {})
            is_home = home.get("team", {}).get("id") == team_id
            my    = home if is_home else away
            opp   = away if is_home else home
            results.append({
                "game_pk":      g.get("gamePk"),
                "date":         date_entry.get("date", ""),
                "is_home":      is_home,
                "won":          bool(my.get("isWinner")),
                "runs_scored":  int(my.get("score") or 0),
                "runs_allowed": int(opp.get("score") or 0),
            })
    return results


def _team_trends(
    team_record: list[dict],
    pitcher_hist_cur: list[dict],
    is_home_today: bool,
    today_s: str,
) -> Optional[dict]:
    if not team_record:
        return None
    completed = [g for g in team_record if g["date"] != today_s]

    def wl(games):
        w = sum(1 for g in games if g["won"])
        return w, len(games) - w

    def avg_rs(games):
        return round(sum(g["runs_scored"] for g in games) / len(games), 1) if games else None

    last10      = completed[-10:]
    side10      = [g for g in completed if g["is_home"] == is_home_today][-10:]

    start_pks = {
        s.get("game", {}).get("gamePk")
        for s in pitcher_hist_cur
        if int(s.get("stat", {}).get("gamesStarted", 0)) > 0
        and s.get("game", {}).get("gamePk")
    }
    in_starts   = [g for g in completed if g["game_pk"] in start_pks]
    last5       = in_starts[-5:]
    last5_side  = [g for g in in_starts if g["is_home"] == is_home_today][-5:]

    # Win/loss streak
    streak_count = 0
    streak_type: Optional[str] = None
    for g in reversed(completed):
        if streak_type is None:
            streak_type = "W" if g["won"] else "L"
            streak_count = 1
        elif g["won"] == (streak_type == "W"):
            streak_count += 1
        else:
            break

    return {
        "is_home":       is_home_today,
        "last10":        wl(last10),
        "last10_side":   wl(side10),
        "last5":         wl(last5),
        "last5_side":    wl(last5_side),
        "avg_runs":      avg_rs(last5),
        "avg_runs_side": avg_rs(last5_side),
        "n_last10":      len(last10),
        "n_side10":      len(side10),
        "n_last5":       len(last5),
        "n_side5":       len(last5_side),
        "streak_type":   streak_type if streak_count >= 4 else None,
        "streak_count":  streak_count if streak_count >= 4 else 0,
    }


def pitcher_history_flags(
    starts: list[dict],
    hand: str,
    rhp_pool: dict,
    lhp_pool: dict,
    today: "date",
) -> list[str]:
    """Derive context flags from MLB game log entries."""
    flags = []
    if not starts:
        return flags

    def _raw_date(s: dict) -> str:
        return (s.get("date") or s.get("game", {}).get("officialDate", ""))[:10]

    def _parse_date(s: dict):
        try:
            return datetime.strptime(_raw_date(s), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    # Exclude today's game — the log may already contain tonight's entry mid-game
    today_s = today.isoformat() if today else ""
    all_appearances = [s for s in starts if _raw_date(s) != today_s]
    start_entries   = [s for s in all_appearances if int(s.get("stat", {}).get("gamesStarted", 0)) > 0]
    recent_3        = start_entries[-3:]

    # ── Days since last start ─────────────────────────────────────────────────
    if start_entries:
        last_dt = _parse_date(start_entries[-1])
        if last_dt:
            days = (today - last_dt).days
            if days > 10:
                flags.append(f"{days} days since last start ({_raw_date(start_entries[-1])}) — may not be fully stretched out")

    # ── Recent relief appearances ─────────────────────────────────────────────
    relief_dates = []
    for s in all_appearances[-6:]:
        if int(s.get("stat", {}).get("gamesStarted", 0)) == 0:
            relief_dates.append(_raw_date(s))
    if relief_dates:
        flags.append("recent bullpen appearance: " + ", ".join(sorted(relief_dates, reverse=True)[:2]) + " — may affect pitch count or availability")

    # ── Pitch count on last start ─────────────────────────────────────────────
    if start_entries:
        last_stat = start_entries[-1].get("stat", {})
        pc = last_stat.get("numberOfPitches")
        if pc is not None:
            pc = int(pc)
            if pc < 80:
                flags.append(f"last start: {pc} pitches — short outing, possible injury concern or early hook")
            elif pc > 100:
                flags.append(f"last start: {pc} pitches — high pitch count, may be on shorter leash today")

    # ── One rough outing skewing the 3-game ERA ───────────────────────────────
    if len(recent_3) >= 2:
        outings = []
        for s in recent_3:
            stat = s.get("stat", {})
            oip = flt(stat.get("inningsPitched"))
            oer = int(stat.get("earnedRuns") or 0)
            if oip and oip > 0:
                outings.append({"ip": oip, "er": oer, "era_eq": (oer / oip) * 9, "date": _raw_date(s)})
        if len(outings) >= 2:
            worst  = max(outings, key=lambda x: x["era_eq"])
            others = [o for o in outings if o is not worst]
            avg_other = sum(o["era_eq"] for o in others) / len(others)
            if worst["era_eq"] >= 7.0 and avg_other <= 4.50:
                outing_str = (
                    f"{worst['er']} ER in {worst['ip']:.1f} IP"
                    + (f" (ERA equiv {worst['era_eq']:.0f})" if worst["ip"] >= 2.0 else "")
                )
                flags.append(f"{worst['date']}: {outing_str} skewing 3-game ERA — other starts look better, don't overweight the ERA")

    # ── K outlier in last 3 starts ────────────────────────────────────────────
    k_pairs = []
    for s in recent_3:
        stat = s.get("stat", {})
        k_val = stat.get("strikeOuts")
        if k_val is not None:
            k_pairs.append((int(k_val), _raw_date(s)))
    if len(k_pairs) >= 2:
        avg_k = sum(k for k, _ in k_pairs) / len(k_pairs)
        for k, d in k_pairs:
            if k >= max(avg_k * 1.75, 9) and k >= avg_k + 3:
                flags.append(f"high-K outing {d} ({k} Ks vs avg {avg_k:.1f}) — stuff can dominate; may not repeat")
            elif avg_k >= 5 and k <= avg_k * 0.4 and k <= avg_k - 3:
                flags.append(f"low-K outing {d} ({k} Ks vs avg {avg_k:.1f}) — stuff was flat that day")

    # ── Opponent K-rate context ───────────────────────────────────────────────
    opp_pool = lhp_pool if hand == "L" else rhp_pool
    opp_ks: list[tuple[str, float]] = []
    for s in recent_3:
        opp_full = (s.get("opponent") or {}).get("name", "")
        opp_code = _MLB_NAME_TO_CODE.get(opp_full, "")
        if not opp_code:
            continue
        row = opp_pool.get(opp_code) or opp_pool.get(to_stats(opp_code), {})
        k = flt(row.get("K%")) if row else None
        if k is not None:
            opp_ks.append((opp_code, k))
    if len(opp_ks) >= 2:
        high_k = [(t, k) for t, k in opp_ks if k > 25]
        low_k  = [(t, k) for t, k in opp_ks if k < 19]
        if len(high_k) >= 2:
            detail = ", ".join(f"{t} {k:.0f}%" for t, k in high_k)
            flags.append(f"recent opponents high-K: {detail} — K stats may be inflated vs strikeout-prone lineups")
        elif len(low_k) >= 2:
            detail = ", ".join(f"{t} {k:.0f}%" for t, k in low_k)
            flags.append(f"recent opponents low-K: {detail} — recent opponents make less contact; today's lineup may be tougher")

    return flags


# ── Weather ───────────────────────────────────────────────────────────────────
# (lat, lon, city, IANA timezone)
STADIUMS: dict[str, tuple] = {
    "ARI": (33.4453, -112.0667, "Phoenix",           "America/Phoenix"),
    "ATH": (38.5802, -121.4687, "Sacramento",         "America/Los_Angeles"),
    "OAK": (38.5802, -121.4687, "Sacramento",         "America/Los_Angeles"),
    "ATL": (33.8908,  -84.4677, "Atlanta",            "America/New_York"),
    "BAL": (39.2838,  -76.6218, "Baltimore",          "America/New_York"),
    "BOS": (42.3467,  -71.0972, "Boston",             "America/New_York"),
    "CHC": (41.9484,  -87.6553, "Chicago (Wrigley)",  "America/Chicago"),
    "CWS": (41.8300,  -87.6338, "Chicago (Sox)",      "America/Chicago"),
    "CHW": (41.8300,  -87.6338, "Chicago (Sox)",      "America/Chicago"),
    "CIN": (39.0978,  -84.5081, "Cincinnati",         "America/New_York"),
    "CLE": (41.4962,  -81.6852, "Cleveland",          "America/New_York"),
    "COL": (39.7559, -104.9942, "Denver",             "America/Denver"),
    "DET": (42.3390,  -83.0485, "Detroit",            "America/Detroit"),
    "HOU": (29.7573,  -95.3555, "Houston",            "America/Chicago"),
    "KC":  (39.0517,  -94.4803, "Kansas City",        "America/Chicago"),
    "KCR": (39.0517,  -94.4803, "Kansas City",        "America/Chicago"),
    "LAA": (33.8003, -117.8827, "Anaheim",            "America/Los_Angeles"),
    "LAD": (34.0739, -118.2400, "Los Angeles",        "America/Los_Angeles"),
    "MIA": (25.7781,  -80.2197, "Miami",              "America/New_York"),
    "MIL": (43.0280,  -87.9712, "Milwaukee",          "America/Chicago"),
    "MIN": (44.9817,  -93.2781, "Minneapolis",        "America/Chicago"),
    "NYM": (40.7571,  -73.8458, "New York (Mets)",    "America/New_York"),
    "NYY": (40.8296,  -73.9262, "New York (Yankees)", "America/New_York"),
    "PHI": (39.9061,  -75.1665, "Philadelphia",       "America/New_York"),
    "PIT": (40.4469,  -80.0058, "Pittsburgh",         "America/New_York"),
    "SD":  (32.7076, -117.1570, "San Diego",          "America/Los_Angeles"),
    "SDP": (32.7076, -117.1570, "San Diego",          "America/Los_Angeles"),
    "SEA": (47.5914, -122.3325, "Seattle",            "America/Los_Angeles"),
    "SF":  (37.7786, -122.3893, "San Francisco",      "America/Los_Angeles"),
    "SFG": (37.7786, -122.3893, "San Francisco",      "America/Los_Angeles"),
    "STL": (38.6226,  -90.1928, "St. Louis",          "America/Chicago"),
    "TB":  (27.7682,  -82.6534, "St. Petersburg",     "America/New_York"),
    "TBR": (27.7682,  -82.6534, "St. Petersburg",     "America/New_York"),
    "TEX": (32.7473,  -97.0824, "Arlington",          "America/Chicago"),
    "TOR": (43.6414,  -79.3894, "Toronto",            "America/Toronto"),
    "WSH": (38.8730,  -77.0074, "Washington",         "America/New_York"),
    "WSN": (38.8730,  -77.0074, "Washington",         "America/New_York"),
}

def get_weather(home_team: str, target_date: date) -> dict:
    if not HAS_REQUESTS:
        return {}
    s = STADIUMS.get(home_team)
    if not s:
        return {}
    lat, lon, city, tz = s
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "precipitation_probability_max,temperature_2m_max,windspeed_10m_max",
                "timezone": tz,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "wind_speed_unit": "mph",
                "temperature_unit": "fahrenheit",
            },
            timeout=10,
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        precip = (daily.get("precipitation_probability_max") or [None])[0]
        temp   = (daily.get("temperature_2m_max") or [None])[0]
        wind   = (daily.get("windspeed_10m_max") or [None])[0]
        return {
            "venue_name":        city,
            "roof_status":       "Open Air",
            "temperature":       temp,
            "precip_probability": precip,
            "precip_risk_during_game": precip is not None and precip >= 50,
            "wind_speed":        wind,
            "wind_effect_label": ("Out" if wind and wind > 15 else ""),
        }
    except Exception:
        return {}

def weather_flags(wx: dict) -> list[str]:
    """Generate flags from Handigraphs ballpark-weather data."""
    flags = []
    if not wx:
        return flags

    roof = wx.get("roof_status", "")
    if not roof or roof in ("Open Air", "N/A") or "open" in roof.lower():
        is_outdoor = True
    elif "dome" in roof.lower() or "closed" in roof.lower():
        is_outdoor = False
    else:
        is_outdoor = True

    if not is_outdoor:
        return flags

    precip_risk = wx.get("precip_risk_during_game", False)
    precip_prob = wx.get("precip_probability")
    wind_lbl    = wx.get("wind_effect_label", "")
    wind_speed  = wx.get("wind_speed")
    apf         = wx.get("adjusted_park_factor")

    if precip_risk:
        prob_s = f" {precip_prob:.0f}%" if precip_prob is not None else ""
        flags.append(f"rain risk{prob_s} — game delay or conditions may affect performance")
    elif precip_prob is not None and precip_prob >= 30:
        flags.append(f"rain chance {precip_prob:.0f}% — monitor for delays")

    if wind_lbl and wind_lbl not in ("Calm", "Indoor", ""):
        speed_s = f" {wind_speed:.0f} mph" if wind_speed is not None else ""
        flags.append(f"wind: {wind_lbl}{speed_s} — factor into total and HR expectations")

    if apf is not None:
        if apf >= 108:
            flags.append(f"hitter-friendly park (APF {apf:.0f}) — park boosts offense, favor the over and HR props")
        elif apf <= 92:
            flags.append(f"pitcher-friendly park (APF {apf:.0f}) — park suppresses offense, favor the under")

    return flags


# ── Terminal colors ───────────────────────────────────────────────────────────
class C:
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
    CYAN   = "\033[36m"
    YELLOW = "\033[33m"
    DIM    = "\033[2m"

_use_color = True

def bold(s):   return f"{C.BOLD}{s}{C.RESET}" if _use_color else s
def cyan(s):   return f"{C.CYAN}{s}{C.RESET}" if _use_color else s
def yellow(s): return f"{C.YELLOW}{s}{C.RESET}" if _use_color else s
def dim(s):    return f"{C.DIM}{s}{C.RESET}" if _use_color else s


def _extract_outings(history: list[dict], n: int = 5) -> list[dict]:
    """Return the n most-recent outings from a game-log list, newest first."""
    result = []
    for s in reversed(history):
        stat = s.get("stat", {})
        if not stat:
            continue
        raw_date = s.get("date") or s.get("game", {}).get("officialDate", "")
        try:
            dt = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            date_s = dt.strftime("%b %-d")
        except Exception:
            date_s = raw_date[:10]

        is_win = s.get("isWin")
        if is_win is True:
            result_s = "W"
        elif is_win is False:
            result_s = "L"
        else:
            result_s = "ND"

        opp_full = (s.get("opponent") or {}).get("name", "")
        opp_code = _MLB_NAME_TO_CODE.get(opp_full, opp_full[:3].upper() if opp_full else "?")
        is_home  = s.get("isHome")
        is_relief = int(stat.get("gamesStarted", 1)) == 0

        result.append({
            "date":      date_s,
            "ha":        "H" if is_home else ("@" if is_home is False else "?"),
            "opp":       opp_code,
            "result":    result_s,
            "ip":        stat.get("inningsPitched", "?"),
            "pc":        stat.get("numberOfPitches"),
            "k":         stat.get("strikeOuts"),
            "h":         stat.get("hits"),
            "bb":        stat.get("baseOnBalls"),
            "er":        stat.get("earnedRuns"),
            "r":         stat.get("runs"),
            "is_relief": is_relief,
        })
        if len(result) >= n:
            break
    return result


# ── Per-game output ───────────────────────────────────────────────────────────
def _situational_avg(entries: list[dict]) -> Optional[dict]:
    """Average pitching stats (starts only) over a list of game log splits."""
    starts = [
        s for s in entries
        if int(s.get("stat", {}).get("gamesStarted", 0)) > 0
        and flt(s.get("stat", {}).get("inningsPitched")) is not None
    ]
    if not starts:
        return None
    n = len(starts)
    total_ip = sum(flt(s["stat"]["inningsPitched"]) or 0 for s in starts)
    total_er = sum(int(s["stat"].get("earnedRuns") or 0) for s in starts)
    total_k  = sum(int(s["stat"].get("strikeOuts") or 0) for s in starts)
    total_h  = sum(int(s["stat"].get("hits") or 0) for s in starts)
    total_bb = sum(int(s["stat"].get("baseOnBalls") or 0) for s in starts)
    era = (total_er / total_ip * 9) if total_ip > 0 else None
    return {
        "n":     n,
        "ip":    f"{total_ip / n:.1f}",
        "era":   f"{era:.2f}" if era is not None else "?",
        "era_f": era,
        "k":     f"{total_k / n:.1f}",
        "h":     f"{total_h / n:.1f}",
        "bb":    f"{total_bb / n:.1f}",
        "er":    f"{total_er / n:.1f}",
    }


def analyze_game(
    p1: dict, p2: dict,
    rhp: dict, lhp: dict,
    bullpen: dict,
    mlb_info: dict,
    wx: dict,
    today: Optional[date] = None,
) -> dict:
    """Return structured analysis dict — used by both terminal and HTML renderers."""
    today = today or date.today()
    t1, t2 = p1.get("Team", "?"), p2.get("Team", "?")
    home_abbr = mlb_info.get("home", "")
    away_abbr = mlb_info.get("away", "")

    if home_abbr and away_abbr:
        if to_mlb(t1) == away_abbr:
            away_team, home_team, p_away, p_home = t1, t2, p1, p2
        else:
            away_team, home_team, p_away, p_home = t2, t1, p2, p1
    else:
        away_team, home_team, p_away, p_home = t1, t2, p1, p2

    def _sp(p: dict) -> dict:
        hand = (p.get("Throws") or "?")[0]
        xera = flt(p.get("xERA"))
        kbb  = flt(p.get("K-BB%", ""))
        ogs  = flt(p.get("Outs/GS"))
        ip   = flt(p.get("IP"))
        if ogs is not None:
            depth = f"{ogs/3:.1f} IP/gs"
        elif ip is not None:
            depth = f"{ip:.1f} IP (3gs)"
        else:
            depth = "—"
        era = flt(p.get("ERA"))
        has_stats = xera is not None or era is not None or flt(p.get("K%")) is not None
        return {
            "name":      p.get("Name", "TBD"),
            "hand":      hand,
            "has_stats": has_stats,
            "xera":   xera,
            "xera_s": f"{xera:.2f}" if xera is not None else "?",
            "era":    era,
            "era_s":  f"{era:.2f}" if era is not None else "?",
            "label":  xera_label(xera) if xera is not None else "",
            "kbb":    kbb,
            "kbb_s":  f"{kbb:.1f}%" if kbb is not None else "?",
            "depth":  depth,
            "k":      fp1(p.get("K%")),
            "bb":     fp1(p.get("BB%")),
            "hard":   fp1(p.get("Hard-Hit%")),
            "barrel": fp1(p.get("Barrel%")),
            "h_per_gs": (lambda h, g: f"{h/g:.1f}" if h and g else "?")(
                flt(p.get("H")), flt(p.get("Games"))),
        }

    def _off(batting: str, pitcher: dict) -> Optional[dict]:
        hand = (pitcher.get("Throws") or "?")[0]
        if hand not in ("R", "L"):
            return None
        pool = rhp if hand == "R" else lhp
        s = pool.get(to_stats(batting), {})
        if not s:
            return None
        wrc = flt(s.get("wRC+"))
        return {
            "wrc":      wrc,
            "wrc_s":    f"{wrc:.0f}" if wrc is not None else "N/A",
            "label":    wrc_label(wrc) if wrc is not None else "",
            "woba":     fp3(s.get("wOBA")),
            "k":        fp1(s.get("K%")),
            "hard":     fp1(s.get("HardHit%")),
            "vs_hand":  "RHP" if hand == "R" else "LHP",
        }

    def _bp(team: str) -> dict:
        b = bullpen.get(team, bullpen.get(to_stats(team), {}))
        xera = flt(b.get("xERA"))
        era  = flt(b.get("ERA"))
        return {
            "xera":   xera,
            "xera_s": f"{xera:.2f}" if xera is not None else "N/A",
            "era_s":  f"{era:.2f}" if era is not None else "N/A",
            "label":  xera_label(xera) if xera is not None else "",
            "k":      fp1(b.get("K%") or b.get("k_pct") or b.get("k_perc")),
            "bb":     fp1(b.get("BB%") or b.get("bb_pct") or b.get("bb_perc")),
            "hard":   fp1(b.get("Hard%")),
            "barrel": fp1(b.get("Barrel%")),
            "raw":    b,
        }

    away_sp = _sp(p_away)
    home_sp = _sp(p_home)
    xa, xh  = away_sp["xera"], home_sp["xera"]
    pitch_edge = (
        None if xa is None or xh is None or abs(xa - xh) < 0.5
        else (away_team if xa < xh else home_team)
    )

    away_off = _off(away_team, p_home)
    home_off = _off(home_team, p_away)
    wrc_a = away_off["wrc"] if away_off else None
    wrc_h = home_off["wrc"] if home_off else None
    off_edge = (
        None if wrc_a is None or wrc_h is None or abs(wrc_a - wrc_h) < 10
        else (away_team if wrc_a > wrc_h else home_team)
    )

    away_bp = _bp(away_team)
    home_bp = _bp(home_team)
    xbp_a, xbp_h = away_bp["xera"], home_bp["xera"]
    bp_edge = (
        None if xbp_a is None or xbp_h is None or abs(xbp_a - xbp_h) < 0.3
        else (away_team if xbp_a < xbp_h else home_team)
    )

    tally: dict[str, int] = {away_team: 0, home_team: 0}
    cat_edges = []
    for cat, winner in [("Pitching", pitch_edge), ("Offense", off_edge), ("Bullpen", bp_edge)]:
        cat_edges.append((cat, winner))
        if winner:
            tally[winner] += 1

    best = max(tally.values())
    leaders = [t for t, v in tally.items() if v == best]
    if best == 0:
        verdict, verdict_team = "TOSS-UP / no clear edge", None
    elif best == 1:
        verdict, verdict_team = f"Lean {leaders[0]}  (1 of 3)", leaders[0]
    else:
        verdict, verdict_team = f"{leaders[0]}  ({best} of 3 categories)", leaders[0]

    # ── SP situational splits ─────────────────────────────────────────────────
    away_full = _ODDS_TEAM.get(away_team, "")
    home_full = _ODDS_TEAM.get(home_team, "")
    away_hist = mlb_info.get(f"history_{away_team}", [])
    home_hist = mlb_info.get(f"history_{home_team}", [])
    # Current-season-only history for outings table and flags — prior-season starts
    # are only relevant for the vs-opp / at-park situational splits.
    cur_year = str(today.year) if today else str(datetime.now(_ET).year)
    away_hist_cur = [s for s in away_hist if s.get("date", "").startswith(cur_year)]
    home_hist_cur = [s for s in home_hist if s.get("date", "").startswith(cur_year)]

    away_sp_splits = {
        "vs": _situational_avg(
            [s for s in away_hist
             if (s.get("opponent") or {}).get("name", "") == home_full][-3:]
        ),
        "at": _situational_avg(
            [s for s in away_hist
             if s.get("isHome") is False
             and (s.get("opponent") or {}).get("name", "") == home_full][-3:]
        ),
    }
    home_sp_splits = {
        "vs": _situational_avg(
            [s for s in home_hist
             if (s.get("opponent") or {}).get("name", "") == away_full][-3:]
        ),
        "at": _situational_avg(
            [s for s in home_hist if s.get("isHome") is True][-3:]
        ),
    }

    today_s = today.isoformat() if today else ""
    away_trends = _team_trends(mlb_info.get("away_record", []), away_hist_cur, False, today_s)
    home_trends = _team_trends(mlb_info.get("home_record", []), home_hist_cur, True,  today_s)

    # H2H record this season — match games by game_pk overlap
    away_rec = mlb_info.get("away_record", [])
    home_rec = mlb_info.get("home_record", [])
    away_pks = {g["game_pk"] for g in away_rec if g.get("game_pk")}
    home_pks = {g["game_pk"] for g in home_rec if g.get("game_pk")}
    h2h_pks  = away_pks & home_pks
    h2h_games = [g for g in away_rec if g.get("game_pk") in h2h_pks]
    away_h2h_w = sum(1 for g in h2h_games if g["won"])
    h2h = {"away_wins": away_h2h_w, "home_wins": len(h2h_games) - away_h2h_w,
           "total": len(h2h_games)}

    flags: list[str] = []
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        name = p.get("Name", "?")
        for f in pitcher_csv_flags(p):
            flags.append(f"{team} — {name}: {f}")
    for team in [away_team, home_team]:
        b = bullpen.get(team, bullpen.get(to_stats(team), {}))
        for f in bullpen_flags(b):
            flags.append(f"{team} bullpen: {f}")
    hist_cur_map = {away_team: away_hist_cur, home_team: home_hist_cur}
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        hand = (p.get("Throws") or "?")[0]
        for f in pitcher_history_flags(
            hist_cur_map[team],
            hand, rhp, lhp, today,
        ):
            flags.append(f"{team} — {p.get('Name', '?')}: {f}")
    for f in weather_flags(wx):
        flags.append(f"WEATHER: {f}")

    return {
        "away":         away_team,
        "home":         home_team,
        "venue":        mlb_info.get("venue", ""),
        "game_date":    mlb_info.get("game_date", ""),
        "away_sp":      away_sp,
        "home_sp":      home_sp,
        "pitch_edge":   pitch_edge,
        "away_off":     away_off,
        "home_off":     home_off,
        "off_edge":     off_edge,
        "away_bp":      away_bp,
        "home_bp":      home_bp,
        "bp_edge":      bp_edge,
        "cat_edges":    cat_edges,
        "verdict":      verdict,
        "verdict_team": verdict_team,
        "verdict_count": best,
        "wx":           wx,
        "flags":        flags,
        "away_sp_outings": _extract_outings(away_hist_cur),
        "home_sp_outings": _extract_outings(home_hist_cur),
        "away_sp_splits":  away_sp_splits,
        "home_sp_splits":  home_sp_splits,
        "away_trends":     away_trends,
        "home_trends":     home_trends,
        "h2h":             h2h,
    }


def print_game(
    p1: dict, p2: dict,
    rhp: dict, lhp: dict,
    bullpen: dict,
    mlb_info: dict,
    wx: dict,
):
    g = analyze_game(p1, p2, rhp, lhp, bullpen, mlb_info, wx)
    away, home = g["away"], g["home"]
    away_sp, home_sp = g["away_sp"], g["home_sp"]
    away_off, home_off = g["away_off"], g["home_off"]
    away_bp, home_bp = g["away_bp"], g["home_bp"]
    venue = g["venue"]
    W = 64

    title = f"{away} @ {home}" if mlb_info.get("home") else f"{away} vs {home}"
    print()
    print(bold("═" * W))
    print(bold(f" {title}" + (f"  ·  {venue}" if venue else "")))
    print(bold("═" * W))

    def _sp_line(team, sp):
        lbl = f"({sp['label']:<10})" if sp["label"] else ""
        return f"  {team:<5} {sp['name']} ({sp['hand']}HP)   xERA {sp['xera_s']}  {lbl:<12}  K-BB% {sp['kbb_s']}  {sp['depth']}"

    print(cyan("\nSTARTERS"))
    print(_sp_line(away, away_sp))
    print(_sp_line(home, home_sp))
    xa, xh = away_sp["xera"], home_sp["xera"]
    if xa is not None and xh is not None:
        pe = g["pitch_edge"]
        if not pe:
            print(f"  → Pitching: EVEN  (gap {abs(xa-xh):.2f})")
        elif pe == away:
            print(f"  → Pitching edge: {away}  (xERA {xa:.2f} vs {xh:.2f})")
        else:
            print(f"  → Pitching edge: {home}  (xERA {xh:.2f} vs {xa:.2f})")

    def _off_line(team, off):
        if off is None:
            return f"  {team:<5} vs ???: no data"
        lbl = f"({off['label']:<10})" if off["label"] else ""
        return (f"  {team:<5} vs {off['vs_hand']}: wRC+ {off['wrc_s']} {lbl:<12}  "
                f"wOBA {off['woba']}  K% {off['k']}  Hard% {off['hard']}")

    print(cyan("\nOFFENSE vs STARTER HAND"))
    print(_off_line(away, away_off))
    print(_off_line(home, home_off))
    wrc_a = away_off["wrc"] if away_off else None
    wrc_h = home_off["wrc"] if home_off else None
    if wrc_a is not None and wrc_h is not None:
        oe = g["off_edge"]
        if not oe:
            print(f"  → Offense: EVEN  (gap {abs(wrc_a - wrc_h):.0f} wRC+)")
        elif oe == away:
            print(f"  → Offense edge: {away}  (wRC+ {wrc_a:.0f} vs {wrc_h:.0f})")
        else:
            print(f"  → Offense edge: {home}  (wRC+ {wrc_h:.0f} vs {wrc_a:.0f})")

    def _bp_line(team, bp):
        lbl = f"({bp['label']:<10})" if bp["label"] else ""
        return (f"  {team:<5} xERA {bp['xera_s']} {lbl:<12}  ERA {bp['era_s']}  "
                f"K% {bp['k']}  BB% {bp['bb']}  Hard% {bp['hard']}")

    print(cyan("\nBULLPENS  (last 12g)"))
    print(_bp_line(away, away_bp))
    print(_bp_line(home, home_bp))
    xbp_a, xbp_h = away_bp["xera"], home_bp["xera"]
    if xbp_a is not None and xbp_h is not None:
        be = g["bp_edge"]
        if not be:
            print(f"  → Bullpen: EVEN  (gap {abs(xbp_a - xbp_h):.2f})")
        elif be == away:
            print(f"  → Bullpen edge: {away}  (xERA {xbp_a:.2f} vs {xbp_h:.2f})")
        else:
            print(f"  → Bullpen edge: {home}  (xERA {xbp_h:.2f} vs {xbp_a:.2f})")

    if g["wx"]:
        print(cyan("\nWEATHER"))
        w = g["wx"]
        venue = w.get("venue_name") or w.get("city", "?")
        roof = w.get("roof_status", "")
        roof_s = f" ({roof})" if roof and roof not in ("Open Air", "N/A") else ""
        time_s = f"  ·  {w['game_time_local']}" if w.get("game_time_local") else ""
        print(f"  {venue}{roof_s}{time_s}")
        parts = []
        if w.get("temperature")   is not None: parts.append(f"{w['temperature']:.0f}°F")
        if w.get("weather_description"):        parts.append(w["weather_description"])
        if w.get("wind_speed") is not None:
            wd = w.get("wind_direction_label", "")
            parts.append(f"Wind {w['wind_speed']:.0f} mph {wd}".strip())
        if w.get("precip_probability") is not None: parts.append(f"Rain {w['precip_probability']:.0f}%")
        if parts:
            print(f"  {', '.join(parts)}")
        apf = w.get("adjusted_park_factor")
        hit = w.get("hitting_conditions", "")
        pit = w.get("pitching_conditions", "")
        if apf is not None:
            print(f"  Park factor {apf:.0f}  |  Hitting: {hit}  |  Pitching: {pit}")

    print(cyan("\nEDGE SUMMARY"))
    for cat, winner in g["cat_edges"]:
        print(f"  {cat:<9}  {winner if winner else 'EVEN'}")
    print(bold(f"  Overall    {g['verdict']}"))

    if g["flags"]:
        print(cyan("\nFLAGS / CONSIDERATIONS"))
        for f in g["flags"]:
            print(yellow(f"  ⚠  {f}"))


# ── HTML output ───────────────────────────────────────────────────────────────
_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:15px;line-height:1.5;background:#f3f4f6;color:#111827;padding-bottom:2rem}
header{background:#0f172a;color:white;padding:.875rem 1rem;text-align:center;position:sticky;top:0;z-index:10}
header h1{font-size:1.15rem;font-weight:700;letter-spacing:-.01em}
.sub{font-size:.73rem;color:#94a3b8;margin-top:.2rem}
main{max-width:580px;margin:0 auto;padding:.5rem .625rem}
.game{background:white;margin-bottom:.5rem;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden}
.game>summary{list-style:none;cursor:pointer;padding:.7rem .875rem;display:flex;justify-content:space-between;align-items:center;gap:.5rem;-webkit-tap-highlight-color:transparent;user-select:none}
.game>summary::-webkit-details-marker{display:none}
.game[open]>summary{border-bottom:1px solid #f0f0f0}
.gs-matchup{flex:1;min-width:0}
.gs-teams{font-size:.975rem;font-weight:700;display:flex;align-items:center;gap:.3rem}
.gs-venue{font-size:.7rem;color:#9ca3af;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tm-logo{width:22px;height:22px;object-fit:contain;flex-shrink:0}
.tm-logo-sm{width:14px;height:14px;object-fit:contain;vertical-align:middle}
.gd{padding:.7rem .875rem .875rem;display:flex;flex-direction:column;gap:.7rem}
.sec-hd{font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af;margin-bottom:.28rem}
.hb{background:#e5e7eb;color:#374151;font-size:.63rem;font-weight:700;padding:.04rem .26rem;border-radius:3px}
.xr{font-weight:600}
.era-elite{color:#16a34a}.era-good{color:#2563eb}.era-avg{color:#6b7280}.era-below{color:#d97706}.era-poor{color:#dc2626}.era-na{color:#9ca3af}
.wrc-elite{color:#16a34a}.wrc-above{color:#2563eb}.wrc-avg{color:#6b7280}.wrc-below{color:#d97706}.wrc-poor{color:#dc2626}
.dim{color:#9ca3af;font-size:.795rem}
.mu-outer{display:grid;grid-template-columns:1fr 1px 1fr;gap:0 .55rem;align-items:start}
.mu-col{display:flex;flex-direction:column;gap:.4rem;min-width:0}
.mu-divider{background:rgba(0,0,0,.1);align-self:stretch}
.sec{border:1px solid rgba(0,0,0,.09);border-radius:.42rem;overflow:hidden}
.sec-sum{display:flex;align-items:center;padding:.38rem .55rem;cursor:pointer;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af;list-style:none;user-select:none}
.sec-sum::-webkit-details-marker{display:none}
.sec-sum::after{content:'▾';margin-left:auto;font-size:.6rem;opacity:.7}
.sec:not([open])>.sec-sum::after{content:'▸'}
.sec-body{padding:.3rem .5rem .5rem}
.mu-card{background:rgba(0,0,0,.028);border-radius:.35rem;padding:.35rem .5rem}
.mu-card-hd{font-size:.75rem;font-weight:700;margin-bottom:.25rem}
.mu-2c{display:grid;grid-template-columns:auto 1fr;gap:.13rem .5rem;font-size:.82rem;align-items:baseline}
.mu-lbl{color:#9ca3af;font-size:.75rem;white-space:nowrap}
.mu-v{font-weight:600;font-variant-numeric:tabular-nums}
.ot-wrap{font-size:.74rem}
.ot-row{display:grid;grid-template-columns:3rem 3.2rem 2rem 2.4rem 2.2rem 1.5rem 1.5rem 1.5rem 1.5rem 1.5rem;gap:.06rem .18rem;align-items:center;padding:.04rem 0}
.ot-hd span{font-size:.62rem;font-weight:700;color:#9ca3af;text-align:center}
.ot-hd span:first-child{text-align:left}
.ot-row span{text-align:center}
.ot-row span:first-child{text-align:left}
.ot-w{color:#16a34a;font-weight:700}
.ot-l{color:#dc2626;font-weight:700}
.ot-nd{color:#9ca3af}
.bp-row{display:flex;align-items:flex-start;gap:.4rem;font-size:.845rem;padding:.18rem 0}
.tm{font-weight:700;font-size:.77rem;min-width:2.3rem;padding-top:.1rem}
.bp-body{flex:1;min-width:0}
.stats{display:flex;flex-wrap:wrap;gap:.15rem .5rem;font-size:.8rem;color:#6b7280}
.stats b{color:#374151;font-weight:600}
.odds-grid{display:grid;grid-template-columns:2.4rem 1fr 1fr 1fr;gap:.18rem .4rem;font-size:.82rem;align-items:center}
.odds-hd{font-size:.6rem;font-weight:700;color:#9ca3af;text-align:center;text-transform:uppercase;letter-spacing:.04em}
.odds-val{text-align:center;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.odds-sub{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af;margin-top:.45rem;margin-bottom:.1rem}
.odds-prop-row{display:flex;align-items:center;gap:.4rem;font-size:.82rem;margin:.15rem 0}
.odds-prop-lbl{font-size:.6rem;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.section-hd{font-size:.85rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;border-top:1px solid #e5e7eb;margin:1.2rem 0 .5rem;padding-top:.9rem}
@media(prefers-color-scheme:dark){.section-hd{color:#9ca3af;border-top-color:#374151}}
.flags{list-style:none}
.flags li{font-size:.78rem;color:#92400e;background:#fffbeb;border-left:3px solid #f59e0b;padding:.18rem .45rem;margin-top:.2rem;border-radius:0 4px 4px 0}
.trends{list-style:none;display:flex;flex-direction:column;gap:.15rem}
.trends li{font-size:.79rem;padding:.12rem 0}
.trend-hd{font-size:.7rem;font-weight:700;color:#374151;padding:.3rem 0 .05rem;border-top:1px solid rgba(0,0,0,.07);margin-top:.2rem}
.trend-hd:first-child{border-top:none;margin-top:0;padding-top:0}
.tw{color:#16a34a;font-weight:700}.tl{color:#dc2626;font-weight:700}
.wx-badge{font-size:.63rem;font-weight:700;background:#e0f2fe;color:#0369a1;padding:.05rem .35rem;border-radius:3px;white-space:nowrap;margin-left:.4rem}
.wx-badge.wx-warn{background:#fef3c7;color:#92400e}
.wx-badge.wx-hot{background:#fee2e2;color:#b91c1c}
.wx-badge.wx-hitter{background:#fef3c7;color:#92400e}
.wx-badge.wx-pitcher{background:#d1fae5;color:#065f46}
@media(prefers-color-scheme:dark){
body{background:#0f0f0f;color:#e5e5e5}
header{background:#030712}
.game{background:#1a1a1a;border-color:#2a2a2a}
.game[open]>summary{border-bottom-color:#2a2a2a}
.gs-venue{color:#6b7280}
.sec-hd{color:#6b7280}
.mu-card{background:rgba(255,255,255,.05)}
.mu-divider{background:rgba(255,255,255,.12)}
.sec{border-color:#2a2a2a}
.mu-lbl{color:#6b7280}
.ot-hd span{color:#6b7280}
.stats b{color:#d1d5db}
.hb{background:#374151;color:#d1d5db}
.flags li{background:#1c1400;border-left-color:#b45309;color:#fbbf24}
.trend-hd{color:#d1d5db;border-top-color:rgba(255,255,255,.1)}
.wx-badge{background:#0c2a3a;color:#7dd3fc}
.wx-badge.wx-warn{background:#2d1a00;color:#fbbf24}
.wx-badge.wx-hot{background:#2d0a0a;color:#fca5a5}
.wx-badge.wx-hitter{background:#2d1a00;color:#fbbf24}
.wx-badge.wx-pitcher{background:#022c22;color:#6ee7b7}
}
.spl-row{display:grid;grid-template-columns:6rem 2.4rem 2.8rem 1.8rem 1.8rem 1.8rem 1.5rem;gap:.05rem .3rem;align-items:center;padding:.15rem 0;font-size:.79rem}
.spl-hd span{font-size:.6rem;font-weight:700;color:#9ca3af;text-align:center;text-transform:uppercase;letter-spacing:.03em}
.spl-hd span:first-child{text-align:left}
.spl-ctx{font-weight:600;font-size:.75rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spl-val{text-align:center;font-variant-numeric:tabular-nums;font-weight:600}
.spl-n{text-align:center;color:#9ca3af;font-size:.65rem}
.spl-sp-hd{font-size:.72rem;font-weight:700;color:#374151;padding:.32rem 0 .08rem;border-top:1px solid rgba(0,0,0,.07)}
.spl-sp-hd:first-child{border-top:none;padding-top:0}
@media(prefers-color-scheme:dark){
.spl-hd span{color:#6b7280}
.spl-sp-hd{color:#d1d5db;border-top-color:rgba(255,255,255,.1)}
}
.ai-picks{background:white;margin:.5rem 0 .75rem;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden}
.ai-picks-hd{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#9ca3af;padding:.45rem .875rem .3rem;cursor:pointer;list-style:none}
.ai-picks[open] .ai-picks-hd{border-bottom:1px solid #f0f0f0}
.ai-game{font-size:.76rem;font-weight:700;color:#374151}
.ai-bet{font-size:.95rem;font-weight:700;margin:.12rem 0}
.ai-odds{font-size:.76rem;color:#6b7280;font-variant-numeric:tabular-nums}
.ai-reason{font-size:.76rem;color:#374151;margin-top:.28rem;line-height:1.45}
.ai-conf{font-size:.54rem;background:#fde68a;color:#92400e;padding:.04rem .26rem;border-radius:3px;font-weight:700;vertical-align:middle;margin-left:.3rem;text-transform:uppercase;letter-spacing:.04em}
.ai-line-warn{font-size:.71rem;color:#b45309;background:#fff7ed;border-left:3px solid #f97316;padding:.15rem .42rem;margin-top:.28rem;border-radius:0 4px 4px 0}
.ai-no-best{font-size:.77rem;color:#6b7280;padding:.5rem .875rem;font-style:italic}
.ai-others-wrap{padding:.45rem .875rem .5rem}
.ai-others-label{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;margin-bottom:.28rem}
.ai-other{border:1px solid #e5e7eb;border-radius:7px;padding:.38rem .55rem;margin-bottom:.32rem}
.ai-other:last-child{margin-bottom:0}
.ai-disclaimer{font-size:.61rem;color:#9ca3af;text-align:center;padding:.3rem .875rem .45rem;border-top:1px solid #f0f0f0;margin-top:.1rem}
.ai-check{display:inline-flex;align-items:center;justify-content:center;width:1.1rem;height:1.1rem;background:#16a34a;color:#fff;border-radius:50%;font-size:.62rem;font-weight:800;margin-left:.4rem;vertical-align:middle;flex-shrink:0;line-height:1}
.ai-pick-card .sec-sum{color:#15803d}
.ai-pick-inline{font-size:.78rem;padding:.05rem 0}
.ai-pick-inline .ai-bet{font-size:.88rem;font-weight:700;margin:.1rem 0}
.ai-pick-inline .ai-odds{font-size:.74rem;color:#6b7280}
.ai-pick-inline .ai-reason{font-size:.73rem;color:#374151;margin-top:.2rem;line-height:1.45}
.ai-pass-reason{font-size:.76rem;color:#6b7280;font-style:italic;padding:.1rem 0}
.ai-found-at{font-size:.65rem;color:#9ca3af;margin-top:.15rem}
.ai-active-wrap{padding:.55rem .875rem .5rem}
.ai-started-wrap{padding:.45rem .875rem .5rem;border-top:1px solid #f0f0f0}
.ai-started-label{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;margin-bottom:.28rem}
.ai-conf-dim{font-size:.54rem;background:#f3f4f6;color:#6b7280;padding:.04rem .26rem;border-radius:3px;font-weight:700;vertical-align:middle;margin-left:.3rem;text-transform:uppercase;letter-spacing:.04em}
.ai-pick-row{border:1px solid #e5e7eb;border-radius:7px;margin-bottom:.32rem;overflow:hidden}
.ai-pick-row:last-child{margin-bottom:0}
.ai-pick-sum{display:flex;align-items:center;padding:.38rem .55rem;cursor:pointer;font-size:.8rem;font-weight:600;color:#374151;list-style:none;user-select:none;gap:.4rem}
.ai-pick-sum::-webkit-details-marker{display:none}
.ai-pick-sum::after{content:'▸';margin-left:auto;font-size:.6rem;opacity:.7;flex-shrink:0}
.ai-pick-row[open]>.ai-pick-sum::after{content:'▾'}
.ai-pick-body{padding:.3rem .55rem .45rem;border-top:1px solid #f0f0f0}
@media(prefers-color-scheme:dark){
.ai-picks{background:#1a1a1a;border-color:#2a2a2a}
.ai-picks[open] .ai-picks-hd{border-bottom-color:#2a2a2a}
.ai-game{color:#d1d5db}
.ai-reason{color:#d1d5db}
.ai-conf{background:#92400e;color:#fde68a}
.ai-line-warn{background:#2a1500;color:#fbbf24;border-left-color:#f97316}
.ai-no-best{color:#9ca3af}
.ai-other{border-color:#2a2a2a}
.ai-others-wrap .ai-game{color:#d1d5db}
.ai-others-wrap .ai-reason{color:#9ca3af}
.ai-disclaimer{border-top-color:#2a2a2a;color:#6b7280}
.ai-pick-card .sec-sum{color:#4ade80}
.ai-pick-inline .ai-reason{color:#d1d5db}
.ai-pass-reason{color:#9ca3af}
.ai-found-at{color:#4b5563}
.ai-started-wrap{border-top-color:#2a2a2a}
.ai-started-label{color:#4b5563}
.ai-conf-dim{background:#2a2a2a;color:#9ca3af}
.ai-pick-row{border-color:#2a2a2a}
.ai-pick-sum{color:#d1d5db}
.ai-pick-body{border-top-color:#2a2a2a}
}
"""

def _h(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _era_cls(label: str) -> str:
    return {"elite": "era-elite", "good": "era-good", "avg": "era-avg",
            "below avg": "era-below", "poor": "era-poor"}.get(label, "era-na")

def _wrc_cls(label: str) -> str:
    return {"elite": "wrc-elite", "above avg": "wrc-above", "avg": "wrc-avg",
            "below avg": "wrc-below", "poor": "wrc-poor"}.get(label, "")

def _k_sp_cls(v):
    if v is None: return "era-na"
    if v >= 28: return "era-elite"
    if v >= 23: return "era-good"
    if v >= 17: return "era-avg"
    if v >= 12: return "era-below"
    return "era-poor"

def _k_sp_lbl(v):
    if v is None: return ""
    if v >= 28: return "elite"
    if v >= 23: return "good"
    if v >= 17: return "avg"
    if v >= 12: return "below avg"
    return "poor"

def _k_bat_cls(v):
    """High lineup K% = more strikeouts = bad for offense."""
    if v is None: return ""
    if v >= 28: return "wrc-poor"
    if v >= 24: return "wrc-below"
    if v >= 20: return "wrc-avg"
    if v >= 16: return "wrc-above"
    return "wrc-elite"

def _k_bat_lbl(v):
    if v is None: return ""
    if v >= 28: return "poor"
    if v >= 24: return "below avg"
    if v >= 20: return "avg"
    if v >= 16: return "above avg"
    return "elite"

def _hh_sp_cls(v):
    """Low HH% allowed = good for pitcher."""
    if v is None: return "era-na"
    if v <= 30: return "era-elite"
    if v <= 35: return "era-good"
    if v <= 40: return "era-avg"
    if v <= 45: return "era-below"
    return "era-poor"

def _hh_sp_lbl(v):
    if v is None: return ""
    if v <= 30: return "elite"
    if v <= 35: return "good"
    if v <= 40: return "avg"
    if v <= 45: return "below avg"
    return "poor"

def _hh_bat_cls(v):
    """High HH% = good for offense (they hit the ball hard)."""
    if v is None: return ""
    if v >= 45: return "wrc-elite"
    if v >= 40: return "wrc-above"
    if v >= 35: return "wrc-avg"
    if v >= 30: return "wrc-below"
    return "wrc-poor"

def _hh_bat_lbl(v):
    if v is None: return ""
    if v >= 45: return "elite"
    if v >= 40: return "above avg"
    if v >= 35: return "avg"
    if v >= 30: return "below avg"
    return "poor"

def _barrel_sp_cls(v):
    """Low Barrel% allowed = good for pitcher."""
    if v is None: return "era-na"
    if v <= 5:  return "era-elite"
    if v <= 8:  return "era-good"
    if v <= 11: return "era-avg"
    if v <= 15: return "era-below"
    return "era-poor"

def _barrel_sp_lbl(v):
    if v is None: return ""
    if v <= 5:  return "elite"
    if v <= 8:  return "good"
    if v <= 11: return "avg"
    if v <= 15: return "below avg"
    return "poor"

def _apf_cls_lbl(v):
    if v is None: return "era-avg", "Neutral"
    if v >= 108: return "era-poor",  "Hitter Friendly"
    if v >= 103: return "era-below", "Hitter Friendly"
    if v >= 97:  return "era-avg",   "Neutral"
    if v >= 93:  return "era-good",  "Pitcher Friendly"
    return "era-elite", "Pitcher Friendly"

def _wx_summary(wx: dict) -> tuple[str, str]:
    """Return (label, css_class) for weather badge. Empty label = no badge."""
    if not wx:
        return "", ""
    desc = (wx.get("weather_description") or "").lower()
    if any(x in desc for x in ("thunder", "lightning", "storm")):
        return "Lightning", "wx-warn"
    parts = []
    cls = ""
    if wx.get("precip_risk_during_game") or any(x in desc for x in ("rain", "drizzle", "shower")):
        parts.append("Rainy")
    temp = wx.get("temperature")
    if temp is not None:
        if temp < 50:
            parts.append("Cold")
        elif temp > 90:
            parts.append("Hot")
            cls = "wx-hot"
    wind = wx.get("wind_speed")
    if wind is not None and wind > 15:
        parts.append("Windy")
    return ", ".join(parts), cls or ("wx-warn" if parts else "")


def _pick_dom_id(pick: dict) -> str:
    """Stable DOM id for a pick <details> row, used for localStorage persistence."""
    raw = f"pick-{pick.get('game','')} {pick.get('bet_type','')} {pick.get('bet','')}"
    return re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')[:64]


def _pick_summary_title(pick: dict) -> str:
    """Return 'Bet (Odds)' for use as a collapsed pick row title."""
    bet_type  = (pick.get("bet_type") or "").lower()
    bet       = pick.get("bet", "")
    game      = pick.get("game", "")
    odds      = pick.get("odds", "")
    team_side = (pick.get("team_side") or "")
    line      = pick.get("line")

    is_f5 = bet_type.startswith("f5")

    f5_tag = "F5 " if is_f5 else ""

    # Totals: concise "TEAM [F5] u/o{line}" format
    if "total" in bet_type and game and line is not None and team_side:
        if team_side in ("over", "under"):
            ou = "u" if team_side == "under" else "o"
            bet_text = f"{game} {f5_tag}{ou}{line}"
        elif "_" in team_side:
            # Team total — extract relevant team from game string
            parts = game.split(" @ ", 1)
            away_team = parts[0].strip() if len(parts) == 2 else game
            home_team = parts[1].strip() if len(parts) == 2 else game
            team = away_team if team_side.startswith("away") else home_team
            ou = "u" if "under" in team_side else "o"
            bet_text = f"{team} {f5_tag}{ou}{line}"
        else:
            bet_text = bet.replace("Over ", "o").replace("Under ", "u")
            if game:
                bet_text = bet_text.replace("Game Total", game)
    else:
        # Normalize Over/Under → o/u (covers Pitcher_Ks, Pitcher_Outs, old-schema, etc.)
        bet_text = bet.replace("Over ", "o").replace("Under ", "u")
        if game and "total" in bet_type:
            bet_text = bet_text.replace("Game Total", game)
        if is_f5 and "F5" not in bet_text.upper():
            first, _, rest = bet_text.partition(" ")
            bet_text = f"{first} F5 {rest}" if rest else f"F5 {bet_text}"
    title = bet_text
    if odds:
        title += f" ({odds})"
    return title


def _html_game(g: dict, ai_pick: Optional[dict] = None) -> str:
    away, home = g["away"], g["home"]
    sp_a, sp_h = g["away_sp"], g["home_sp"]
    of_a, of_h = g["away_off"], g["home_off"]
    bp_a, bp_h = g["away_bp"], g["home_bp"]

    wx = g["wx"] or {}
    roof = wx.get("roof_status", "")
    # Determine indoor label (Dome / Roof Closed) — None means open air
    if not roof or roof in ("Open Air", "N/A") or "open" in roof.lower():
        indoor_label = None
    elif "dome" in roof.lower():
        indoor_label = "Dome"
    elif "closed" in roof.lower():
        indoor_label = "Roof Closed"
    else:
        indoor_label = roof
    is_open_air = indoor_label is None
    roof_paren = f" ({indoor_label})" if indoor_label else ""
    venue_str = (g["venue"] or "") + roof_paren
    # Use MLB schedule game_date as primary time source; wx.game_time_local is fallback only
    # (Handigraphs weather may reflect tomorrow's slate by evening).
    time_str = ""
    if g.get("game_date"):
        try:
            _dt = datetime.fromisoformat(g["game_date"].replace("Z", "+00:00")).astimezone(_ET)
            _h12 = _dt.hour % 12 or 12
            time_str = f"{_h12}:{_dt.minute:02d} {'PM' if _dt.hour >= 12 else 'AM'}"
        except Exception:
            pass
    if not time_str:
        time_str = wx.get("game_time_local", "").replace(" ET", "").strip()
    venue_parts = [p for p in [time_str, venue_str] if p.strip()]
    wx_lbl, wx_cls = _wx_summary(wx)
    apf_raw = wx.get("adjusted_park_factor") if wx else None
    _apf_cls_pre, apf_lbl_pre = _apf_cls_lbl(apf_raw)
    apf_display = "Neutral Conditions" if apf_lbl_pre == "Neutral" else apf_lbl_pre
    # Effective label only for open-air parks — prevents indoor leakage
    if is_open_air:
        if wx_lbl:
            effective_wx_lbl = wx_lbl
            effective_wx_cls = wx_cls
        elif apf_raw is not None:
            effective_wx_lbl = apf_display
            if "Hitter" in apf_lbl_pre:
                effective_wx_cls = "wx-hitter"
            elif "Pitcher" in apf_lbl_pre:
                effective_wx_cls = "wx-pitcher"
            else:
                effective_wx_cls = ""
        else:
            effective_wx_lbl = ""
            effective_wx_cls = ""
    else:
        effective_wx_lbl = ""
        effective_wx_cls = ""
    wx_badge_html = (f'<span class="wx-badge {effective_wx_cls}">{_h(effective_wx_lbl)}</span>'
                     if effective_wx_lbl else "")
    venue_html = (f'<span class="gs-venue">{_h("  ·  ".join(venue_parts))}{wx_badge_html}</span>'
                  if venue_parts else "")

    def _row(lbl, val_s, cls="", lbl_txt=""):
        if val_s == "?":
            return f'<span class="mu-lbl">{_h(lbl)}</span><span class="dim">?</span>'
        lbl_part = f' <span class="dim">({_h(lbl_txt)})</span>' if lbl_txt else ""
        cls_attr = f' class="mu-v {cls}"' if cls else ' class="mu-v"'
        return f'<span class="mu-lbl">{_h(lbl)}</span><span{cls_attr}>{_h(val_s)}{lbl_part}</span>'

    def _outing_avg(outings, key, n=3):
        vals = [o[key] for o in outings[:n] if o.get(key) is not None]
        return f"{sum(vals)/len(vals):.0f}" if vals else None

    def _sp_card(sp, k_line=None, outs_line=None, pc_avg=None):
        ec = _era_cls(sp["label"])
        rows  = _row("xERA",    sp["xera_s"], ec,             sp["label"])
        k_v   = flt(sp["k"])
        rows += _row("K%",      sp["k"],      _k_sp_cls(k_v),  _k_sp_lbl(k_v))
        hh_v  = flt(sp["hard"])
        rows += _row("HH%",     sp["hard"],   _hh_sp_cls(hh_v), _hh_sp_lbl(hh_v))
        bv = flt(sp["barrel"])
        rows += _row("Barrel%", sp["barrel"], _barrel_sp_cls(bv), _barrel_sp_lbl(bv))
        rows += _row("ERA",     sp["era_s"])
        rows += f'<span class="mu-lbl">IP/gs</span><span class="dim">{_h(sp["depth"])}</span>'
        rows += f'<span class="mu-lbl">H/gs</span><span class="dim">{_h(sp["h_per_gs"])}</span>'
        pc_display = pc_avg if (pc_avg and sp.get("has_stats")) else "?"
        rows += f'<span class="mu-lbl">PC/gs</span><span class="dim">{_h(pc_display)}</span>'
        rows += f'<span class="mu-lbl">BB%</span><span class="dim">{_h(sp["bb"])}</span>'
        hb = f'<span class="hb">{_h(sp["hand"])}</span>' if sp["hand"] != "?" else ""
        return (f'<div class="mu-card"><div class="mu-card-hd">{_h(sp["name"])} {hb}</div>'
                f'<div class="mu-2c">{rows}</div></div>')

    def _bat_card(team, off):
        if off:
            wc = _wrc_cls(off["label"])
            rows  = _row("wRC+", off["wrc_s"], wc, off["label"])
            k_v   = flt(off["k"])
            rows += _row("K%",  off["k"],   _k_bat_cls(k_v),  _k_bat_lbl(k_v))
            hh_v  = flt(off["hard"])
            rows += _row("HH%", off["hard"], _hh_bat_cls(hh_v), _hh_bat_lbl(hh_v))
            vs = f'vs {off["vs_hand"]}'
        else:
            rows = f'<span class="dim" style="grid-column:1/-1;font-size:.8rem">No data</span>'
            vs = ""
        return (f'<div class="mu-card"><div class="mu-card-hd">'
                f'{_h(team)} <span class="dim" style="font-weight:400">{_h(vs)}</span></div>'
                f'<div class="mu-2c">{rows}</div></div>')

    def _outing_table(outings):
        if not outings:
            return ""
        def _v(v): return "—" if v is None else str(v)
        hdr = ('<div class="ot-row ot-hd">'
               '<span>Date</span><span>Opp</span><span>Res</span>'
               '<span>IP</span><span>PC</span><span>K</span><span>H</span><span>BB</span><span>ER</span><span>R</span>'
               '</div>')
        rows = ""
        for o in outings:
            rc  = "ot-w" if o["result"] == "W" else "ot-l" if o["result"] == "L" else "ot-nd"
            pfx = "@" if o["ha"] == "@" else "vs"
            opp_code = o["opp"]
            opp_slug = _LOGO.get(opp_code, opp_code.lower())
            opp_url  = f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/mlb/500/{opp_slug}.png&h=28&w=28"
            opp_logo = f'<img src="{opp_url}" class="tm-logo-sm" alt="{_h(opp_code)}" onerror="this.style.display=\'none\'">'
            ip_s = (_v(o["ip"]) + "*") if o.get("is_relief") else _v(o["ip"])
            rows += (f'<div class="ot-row">'
                     f'<span class="dim">{_h(o["date"])}</span>'
                     f'<span class="dim">{pfx} {opp_logo}</span>'
                     f'<span class="{rc}">{_h(o["result"])}</span>'
                     f'<span>{_h(ip_s)}</span>'
                     f'<span class="dim">{_h(_v(o["pc"]))}</span>'
                     f'<span>{_h(_v(o["k"]))}</span>'
                     f'<span class="dim">{_h(_v(o["h"]))}</span>'
                     f'<span class="dim">{_h(_v(o["bb"]))}</span>'
                     f'<span>{_h(_v(o["er"]))}</span>'
                     f'<span class="dim">{_h(_v(o["r"]))}</span>'
                     f'</div>')
        return f'<div class="ot-wrap">{hdr}{rows}</div>'

    def _bp_row(team, bp):
        ec = _era_cls(bp["label"])
        lbl = f' <span class="dim">({_h(bp["label"])})</span>' if bp["label"] else ""
        return (f'<div class="bp-row">'
                f'<span class="tm">{_h(team)}</span>'
                f'<div class="bp-body stats">'
                f'<span class="xr {ec}"><b>xERA</b> {_h(bp["xera_s"])}{lbl}</span>'
                f'<span><b>ERA</b> {_h(bp["era_s"])}</span>'
                f'</div></div>')

    g_id = f"{_h(away)}-{_h(home)}"

    wx_html = ""
    if indoor_label:
        wx_html = (
            f'<details class="sec" id="{g_id}-weather">'
            f'<summary class="sec-sum">Weather · {_h(indoor_label)}</summary>'
            f'<div class="sec-body"><span class="dim">{_h(indoor_label)}</span></div>'
            f'</details>'
        )
    elif wx:
        parts = []
        if wx.get("temperature") is not None:
            parts.append(f"{wx['temperature']:.0f}°F")
        if wx.get("weather_description"):
            parts.append(wx["weather_description"])
        if wx.get("wind_speed") is not None:
            wd = wx.get("wind_direction_label", "")
            parts.append(f"Wind {wx['wind_speed']:.0f} mph {wd}".strip())
        rain_html = ""
        if wx.get("precip_risk_during_game"):
            prob = wx.get("precip_probability")
            rain_s = f"Rain possible ({prob:.0f}%)" if prob is not None else "Rain possible"
            rain_html = f' · <span class="era-below">{_h(rain_s)}</span>'
        apf = wx.get("adjusted_park_factor")
        apf_html = ""
        if apf is not None:
            apf_cls, apf_lbl = _apf_cls_lbl(apf)
            apf_html = f'<span class="{apf_cls}">APF {apf:.0f} — {apf_lbl}</span>'
        cond_line = ""
        if apf_html or rain_html:
            cond_line = f'<div>{apf_html}{rain_html}</div>'
        wx_body = (f'<div class="dim">{_h(", ".join(parts))}</div>' if parts else "") + cond_line
        wx_sum_lbl = f"Weather · {effective_wx_lbl}" if effective_wx_lbl else "Weather"
        wx_html = (
            f'<details class="sec" id="{g_id}-weather">'
            f'<summary class="sec-sum">{_h(wx_sum_lbl)}</summary>'
            f'<div class="sec-body">{wx_body}</div>'
            f'</details>'
        )

    flags_html = ""
    if g["flags"]:
        n = len(g["flags"])
        items = "".join(f'<li>{_h(f)}</li>' for f in g["flags"])
        flags_html = (
            f'<details class="sec" id="{g_id}-flags">'
            f'<summary class="sec-sum">Flags · {n}</summary>'
            f'<div class="sec-body"><ul class="flags">{items}</ul></div>'
            f'</details>'
        )

    _sub = ' style="text-transform:none;font-weight:400;font-size:.62rem"'
    od = g.get("odds")
    odds_html = ""
    if od:
        def _odds_rows(away_ml, home_ml, away_sp, home_sp, ov, un):
            return (
                f'<span></span><span class="odds-hd">ML</span>'
                f'<span class="odds-hd">Spread</span><span class="odds-hd">Total</span>'
                f'<span class="tm">{_h(away)}</span><span class="odds-val">{_h(away_ml)}</span>'
                f'<span class="odds-val">{_h(away_sp)}</span><span class="odds-val">{_h(ov)}</span>'
                f'<span class="tm">{_h(home)}</span><span class="odds-val">{_h(home_ml)}</span>'
                f'<span class="odds-val">{_h(home_sp)}</span><span class="odds-val">{_h(un)}</span>'
            )
        f5_html = ""
        if od.get("has_f5"):
            f5_html = (
                f'<div class="odds-sub">First 5 Innings</div>'
                f'<div class="odds-grid">'
                + _odds_rows(od["away_f5_ml"], od["home_f5_ml"],
                             od["away_f5_spread"], od["home_f5_spread"],
                             od["f5_over"], od["f5_under"])
                + f'</div>'
            )
        tt_html = ""
        if od.get("has_tt") or od.get("has_f5tt"):
            def _tt_row(team_name, over_s, under_s, f5_over_s="", f5_under_s=""):
                has_f5 = bool(f5_over_s and f5_over_s != "—")
                f5_cells = (f'<span class="odds-val dim">{_h(f5_over_s)}</span>'
                            f'<span class="odds-val dim">{_h(f5_under_s)}</span>') if has_f5 else ""
                return (f'<span class="tm">{_h(team_name)}</span>'
                        f'<span class="odds-val">{_h(over_s)}</span>'
                        f'<span class="odds-val">{_h(under_s)}</span>'
                        + f5_cells)
            show_f5tt = od.get("has_f5tt")
            cols = "1fr 1fr 1fr 1fr 1fr" if show_f5tt else "1fr 1fr 1fr"
            f5_hdrs = ('<span class="odds-hd">F5 O</span>'
                       '<span class="odds-hd">F5 U</span>') if show_f5tt else ""
            tt_html = (
                f'<div class="odds-sub">Team Totals</div>'
                f'<div class="odds-grid" style="grid-template-columns:{cols}">'
                f'<span></span><span class="odds-hd">Over</span><span class="odds-hd">Under</span>{f5_hdrs}'
                + _tt_row(away, od["away_tt_over"], od["away_tt_under"],
                          od.get("away_f5tt_over",""), od.get("away_f5tt_under",""))
                + _tt_row(home, od["home_tt_over"], od["home_tt_under"],
                          od.get("home_f5tt_over",""), od.get("home_f5tt_under",""))
                + f'</div>'
            )
        props_html = ""
        away_k_s = _fmt_k_line(od.get("away_k"))
        home_k_s = _fmt_k_line(od.get("home_k"))
        away_outs_s = _fmt_outs_line(od.get("away_outs"))
        home_outs_s = _fmt_outs_line(od.get("home_outs"))
        if away_k_s or home_k_s or away_outs_s or home_outs_s:
            has_outs = away_outs_s or home_outs_s
            cols = "1fr 1fr 1fr" if has_outs else "1fr 1fr"
            def _prop_val(s): return _h(re.sub(r'^(?:K|Outs) O/U ', '', s)) if s else "—"
            def _prop_row(name, k_s, outs_s):
                outs_cell = f'<span class="odds-val">{_prop_val(outs_s)}</span>' if has_outs else ""
                return (f'<span class="tm">{_h(name)}</span>'
                        f'<span class="odds-val">{_prop_val(k_s)}</span>'
                        + outs_cell)
            outs_hd = '<span class="odds-hd">Outs O/U</span>' if has_outs else ""
            props_html = (
                f'<div class="odds-sub">Pitcher Props</div>'
                f'<div class="odds-grid" style="grid-template-columns:{cols}">'
                f'<span></span><span class="odds-hd">K O/U</span>{outs_hd}'
                + _prop_row(sp_a["name"], away_k_s, away_outs_s)
                + _prop_row(sp_h["name"], home_k_s, home_outs_s)
                + f'</div>'
            )
        odds_html = (
            f'<details class="sec" id="{g_id}-odds">'
            f'<summary class="sec-sum">Betting Odds <span class="dim"{_sub}>· best of DK / FanDuel / Fanatics</span></summary>'
            f'<div class="sec-body">'
            f'<div class="odds-sub">Full Game</div>'
            f'<div class="odds-grid">'
            + _odds_rows(od["away_ml"], od["home_ml"],
                         od["away_spread"], od["home_spread"],
                         od["over"], od["under"])
            + f'</div>{f5_html}{tt_html}{props_html}</div></details>'
        )

    away_outings = g.get("away_sp_outings", [])
    home_outings = g.get("home_sp_outings", [])
    away_pc = _outing_avg(away_outings, "pc")
    home_pc = _outing_avg(home_outings, "pc")

    matchup_html = (
        f'<details class="sec" id="{g_id}-matchup" open>'
        f'<summary class="sec-sum">Matchup · SP Last 3 / Team Last 12</summary>'
        f'<div class="sec-body">'
        f'<div class="mu-outer">'
        f'<div class="mu-col">{_sp_card(sp_a, pc_avg=away_pc)}{_bat_card(home, of_h)}</div>'
        f'<div class="mu-divider"></div>'
        f'<div class="mu-col">{_sp_card(sp_h, pc_avg=home_pc)}{_bat_card(away, of_a)}</div>'
        f'</div></div></details>'
    )

    outings_a = _outing_table(away_outings)
    outings_h = _outing_table(home_outings)
    outings_html = ""
    if outings_a:
        outings_html += (
            f'<details class="sec" id="{g_id}-outings-away">'
            f'<summary class="sec-sum">{_h(sp_a["name"])} · Last 5 Outings</summary>'
            f'<div class="sec-body">{outings_a}</div>'
            f'</details>'
        )
    if outings_h:
        outings_html += (
            f'<details class="sec" id="{g_id}-outings-home">'
            f'<summary class="sec-sum">{_h(sp_h["name"])} · Last 5 Outings</summary>'
            f'<div class="sec-body">{outings_h}</div>'
            f'</details>'
        )

    def _spl_row(ctx: str, stats: Optional[dict]) -> str:
        if not stats:
            return (f'<div class="spl-row">'
                    f'<span class="spl-ctx dim">{_h(ctx)}</span>'
                    f'<span class="dim" style="grid-column:2/-1">—</span>'
                    f'</div>')
        era_f = stats.get("era_f")
        ec = _era_cls(xera_label(era_f)) if era_f is not None else "era-na"
        return (
            f'<div class="spl-row">'
            f'<span class="spl-ctx">{_h(ctx)}</span>'
            f'<span class="spl-val">{_h(stats["ip"])}</span>'
            f'<span class="spl-val {ec}">{_h(stats["era"])}</span>'
            f'<span class="spl-val">{_h(stats["k"])}</span>'
            f'<span class="spl-val">{_h(stats["h"])}</span>'
            f'<span class="spl-val">{_h(stats["bb"])}</span>'
            f'<span class="spl-n">({stats["n"]})</span>'
            f'</div>'
        )

    def _spl_hdr() -> str:
        return (
            '<div class="spl-row spl-hd">'
            '<span></span><span>IP</span><span>ERA</span>'
            '<span>K</span><span>H</span><span>BB</span><span></span>'
            '</div>'
        )

    def _spl_block(sp_name: str, spl: dict, vs_lbl: str, at_lbl: str) -> str:
        if not spl.get("vs") and not spl.get("at"):
            return ""
        return (
            f'<div class="spl-sp-hd">{_h(sp_name)}</div>'
            + _spl_hdr()
            + _spl_row(vs_lbl, spl.get("vs"))
            + _spl_row(at_lbl, spl.get("at"))
        )

    away_spl = g.get("away_sp_splits", {})
    home_spl = g.get("home_sp_splits", {})
    splits_inner = (
        _spl_block(sp_a["name"], away_spl, f"vs {home}", f"at {home}")
        + _spl_block(sp_h["name"], home_spl, f"vs {away}", "home starts")
    )
    splits_html = (
        f'<details class="sec" id="{g_id}-splits">'
        f'<summary class="sec-sum">SP vs Opp / At Park · last 3 (2 seasons)</summary>'
        f'<div class="sec-body">{splits_inner}</div>'
        f'</details>'
    ) if splits_inner.strip() else ""

    def _trend_block(team: str, sp_name: str, tr: Optional[dict], is_away: bool) -> str:
        if not tr:
            return ""
        side_lbl = "home" if tr["is_home"] else "away"
        opp = home if is_away else away
        lines = []

        def _wl_s(w, l):
            return f'<span class="tw">{w}</span>-<span class="tl">{l}</span>'

        # H2H record this season
        h2h = g.get("h2h", {})
        if h2h and h2h.get("total", 0) >= 2:
            my_w = h2h["away_wins"] if is_away else h2h["home_wins"]
            op_w = h2h["home_wins"] if is_away else h2h["away_wins"]
            n_h2h = h2h["total"]
            lines.append(f'{_h(team)} are {_wl_s(my_w, op_w)} vs {_h(opp)} this season ({n_h2h} games).')

        n10 = tr["n_last10"]
        if n10:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last10"])} in their last {n10} games.')
        n10s = tr["n_side10"]
        if n10s:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last10_side"])} in their last {n10s} {side_lbl} games.')

        # 4+ game win/loss streak
        if tr.get("streak_count", 0) >= 4:
            verb = "won" if tr["streak_type"] == "W" else "lost"
            lines.append(f'{_h(team)} have {verb} {tr["streak_count"]} straight.')

        n5 = tr["n_last5"]
        if n5:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last5"])} in {_h(sp_name)}\'s last {n5} starts.')
        n5s = tr["n_side5"]
        if n5s:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last5_side"])} in {_h(sp_name)}\'s last {n5s} {side_lbl} starts.')
        if tr["avg_runs"] is not None and n5:
            lines.append(f'{_h(team)} average {tr["avg_runs"]:.1f} runs/game in {_h(sp_name)}\'s last {n5} starts.')
        if tr["avg_runs_side"] is not None and n5s:
            lines.append(f'{_h(team)} average {tr["avg_runs_side"]:.1f} runs/game in {_h(sp_name)}\'s last {n5s} {side_lbl} starts.')

        if not lines:
            return ""
        items = "".join(f"<li>{ln}</li>" for ln in lines)
        return f'<ul class="trends">{items}</ul>'

    away_tr = g.get("away_trends")
    home_tr = g.get("home_trends")
    def _trends_section(team: str, sp_name: str, tr, tid: str, is_away: bool) -> str:
        inner = _trend_block(team, sp_name, tr, is_away)
        if not inner.strip():
            return ""
        return (
            f'<details class="sec" id="{tid}">'
            f'<summary class="sec-sum">Trends · {_h(team)}</summary>'
            f'<div class="sec-body">{inner}</div>'
            f'</details>'
        )
    trends_html = (
        _trends_section(away, sp_a["name"], away_tr, f"{g_id}-trends-away", True)
        + _trends_section(home, sp_h["name"], home_tr, f"{g_id}-trends-home", False)
    )

    bullpen_html = (
        f'<details class="sec" id="{g_id}-bullpen">'
        f'<summary class="sec-sum">Bullpens · Last 12</summary>'
        f'<div class="sec-body">{_bp_row(away,bp_a)}{_bp_row(home,bp_h)}</div>'
        f'</details>'
    )

    # AI pick section for this game card
    ai_check = ""
    ai_sec_html = ""
    if ai_pick is not None:
        game_picks = ai_pick.get("picks") or []
        pass_reason = ai_pick.get("pass_reason", "")
        if game_picks:
            ai_check = '<span class="ai-check">✓</span>'
            sections = []
            for i, pick in enumerate(game_picks):
                sec_id = f'{g_id}-ai-{i}'
                sections.append(
                    f'<details class="sec ai-pick-card" id="{sec_id}">'
                    f'<summary class="sec-sum">{_h(_pick_summary_title(pick))}</summary>'
                    f'<div class="sec-body">'
                    f'<div class="ai-pick-inline">'
                    f'<div class="ai-reason">{_h(pick.get("reason",""))}</div>'
                    f'</div>'
                    f'</div>'
                    f'</details>'
                )
            ai_sec_html = "".join(sections)
        elif pass_reason:
            ai_sec_html = (
                f'<details class="sec" id="{g_id}-ai">'
                f'<summary class="sec-sum">AI Analysis</summary>'
                f'<div class="sec-body"><div class="ai-pass-reason">{_h(pass_reason)}</div></div>'
                f'</details>'
            )

    return (
        f'\n<details class="game" data-start-min="{_time_sort_key(g)}" id="{g_id}">'
        f'\n  <summary>'
        f'\n    <div class="gs-matchup"><div class="gs-teams">{_logo_img(away)}{_h(away)} @ {_logo_img(home)}{_h(home)}{ai_check}</div>{venue_html}</div>'
        f'\n  </summary>'
        f'\n  <div class="gd">'
        f'\n    {matchup_html}'
        f'\n    {odds_html}'
        f'\n    {outings_html}'
        f'\n    {splits_html}'
        f'\n    {trends_html}'
        f'\n    {bullpen_html}'
        f'\n    {wx_html}'
        f'\n    {flags_html}'
        f'\n    {ai_sec_html}'
        f'\n  </div>'
        f'\n</details>'
    )


def _time_sort_key(g: dict) -> int:
    # MLB schedule game_date is authoritative (queried for the correct target date).
    # wx.game_time_local is a fallback only — Handigraphs may show tomorrow's times by evening.
    gd = g.get("game_date", "")
    if gd:
        try:
            dt_utc = datetime.fromisoformat(gd.replace("Z", "+00:00"))
            dt_et = dt_utc.astimezone(_ET)
            return dt_et.hour * 60 + dt_et.minute
        except Exception:
            pass
    t = (g.get("wx") or {}).get("game_time_local", "")
    m = re.match(r'(\d+):(\d+)\s*(AM|PM)', t)
    if m:
        h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "PM" and h != 12: h += 12
        elif ampm == "AM" and h == 12: h = 0
        return h * 60 + mn
    return 9999


_SPLIT_SCRIPT = """
<script>
(function(){
  var GAME_STORE='mlb_open';
  var SEC_STORE='mlb_sec_closed';
  var PICKS_STORE='mlb_picks_open';
  function etMin(){
    var et=new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
    return et.getHours()*60+et.getMinutes();
  }
  function split(){
    var now=etMin();
    var main=document.querySelector('main');
    if(!main)return;
    var cards=Array.from(main.querySelectorAll('details.game[data-start-min]'));
    var started=cards.filter(function(c){return +c.dataset.startMin<=now&&+c.dataset.startMin<1440;});
    if(!started.length)return;
    var hd=document.createElement('h2');
    hd.className='section-hd';
    hd.textContent='In Progress / Completed';
    main.appendChild(hd);
    started.forEach(function(c){main.appendChild(c);});
  }
  function saveGames(){
    var open=Array.from(document.querySelectorAll('details.game[open]')).map(function(d){return d.id;});
    try{localStorage.setItem(GAME_STORE,JSON.stringify(open));}catch(e){}
  }
  function restoreGames(){
    var saved;
    try{saved=JSON.parse(localStorage.getItem(GAME_STORE)||'[]');}catch(e){saved=[];}
    if(!saved.length)return;
    var ids=new Set(saved);
    document.querySelectorAll('details.game').forEach(function(d){
      if(ids.has(d.id))d.setAttribute('open','');
    });
  }
  function saveSections(){
    var state={};
    document.querySelectorAll('details.sec').forEach(function(d){
      if(d.id)state[d.id]=d.hasAttribute('open');
    });
    try{localStorage.setItem(SEC_STORE,JSON.stringify(state));}catch(e){}
  }
  function restoreSections(){
    var saved;
    try{saved=JSON.parse(localStorage.getItem(SEC_STORE)||'null');}catch(e){saved=null;}
    if(!saved)return;
    document.querySelectorAll('details.sec').forEach(function(d){
      if(d.id in saved){
        if(saved[d.id])d.setAttribute('open','');
        else d.removeAttribute('open');
      }
    });
  }
  function savePicksState(){
    var state={};
    var card=document.getElementById('ai-picks-card');
    if(card)state['ai-picks-card']=card.hasAttribute('open');
    document.querySelectorAll('details.ai-pick-row[id]').forEach(function(d){
      state[d.id]=d.hasAttribute('open');
    });
    try{localStorage.setItem(PICKS_STORE,JSON.stringify(state));}catch(e){}
  }
  function restorePicksState(){
    var saved;
    try{saved=JSON.parse(localStorage.getItem(PICKS_STORE)||'null');}catch(e){saved=null;}
    if(!saved)return;
    var card=document.getElementById('ai-picks-card');
    if(card&&'ai-picks-card' in saved){
      if(saved['ai-picks-card'])card.setAttribute('open','');
      else card.removeAttribute('open');
    }
    document.querySelectorAll('details.ai-pick-row[id]').forEach(function(d){
      if(d.id in saved){
        if(saved[d.id])d.setAttribute('open','');
        else d.removeAttribute('open');
      }
    });
  }
  function localTs(){
    document.querySelectorAll('.local-ts[data-utc]').forEach(function(el){
      try{
        var d=new Date(el.dataset.utc);
        el.textContent=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit',timeZoneName:'short'});
      }catch(e){}
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    split();
    restoreGames();
    restoreSections();
    restorePicksState();
    localTs();
    document.querySelectorAll('details.game').forEach(function(d){
      d.addEventListener('toggle',saveGames);
    });
    document.querySelectorAll('details.sec').forEach(function(d){
      d.addEventListener('toggle',saveSections);
    });
    var card=document.getElementById('ai-picks-card');
    if(card)card.addEventListener('toggle',savePicksState);
    document.querySelectorAll('details.ai-pick-row[id]').forEach(function(d){
      d.addEventListener('toggle',savePicksState);
    });
  });
})();
</script>"""


def _ts_span(iso: str) -> str:
    """Render a UTC ISO timestamp as a span the JS will localize to browser TZ."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        fallback = dt.strftime("%H:%M UTC")
    except Exception:
        fallback = iso
    return f'<span class="local-ts" data-utc="{_h(iso)}">{_h(fallback)}</span>'


# ── AI Betting Suggestions ─────────────────────────────────────────────────────

_AI_SYSTEM_PROMPT = """\
You are a sharp MLB sports betting analyst. Identify high-confidence betting opportunities for today's games.

PHILOSOPHY: Be VERY conservative. Only recommend a bet when multiple factors clearly align AND there are no disqualifying factors. "No strong plays today" is a valid and often correct answer. The goal is to eliminate early-season noise and focus on what's happening RIGHT NOW and in THIS specific matchup.

═══════════════════════════════════════════
DATA HIERARCHY — how to weight the inputs
═══════════════════════════════════════════

The baseball season is 162 games. Early-season results add noise. Always weight data in this order:

1. RECENT FORM (last 3 starts) — the strongest signal for how a pitcher is performing TODAY.
   - Recent ERA from the last 3 starts tells you the pitcher's current trajectory.
   - Compare recent ERA to season xERA: if recent ERA is meaningfully lower than xERA, the pitcher is locked in and outperforming expectations right now. If recent ERA is meaningfully higher, they're struggling despite the season-long numbers.
   - Look at pitch counts and innings: is workload increasing (building up) or decreasing (fatigue/trouble)?

2. MATCHUP-SPECIFIC HISTORY — how has this pitcher done vs THIS team and at THIS park?
   - "vs opponent" splits: ERA and IP/gs vs today's specific opponent over the last 2 seasons. A pitcher who has historically dominated a lineup is more likely to repeat that.
   - "at venue" splits: performance at this specific ballpark. Some pitchers thrive or struggle at specific parks independent of the opponent.
   - Small sample warnings apply (n<2 starts), but even 1-2 data points in a specific matchup are more relevant than season aggregates.

3. SEASON xERA — the baseline quality indicator. Use xERA (expected ERA, luck-adjusted) rather than ERA to assess true pitcher quality. xERA corrects for batted ball luck.

4. TEAM OFFENSE (L12 wRC+) — already recency-filtered. This is 12 recent games, not season average. A team with wRC+ 75 in L12 is currently cold, regardless of what they did in April.

5. TEAM TRENDS in pitcher's recent starts — W/L record and run support in THIS pitcher's last 5 starts tells you how the team performs as a unit when this pitcher takes the mound, which is more specific than general team record.

6. SEASON ERA — least reliable signal; heavily influenced by luck (BABIP, strand rate). Primarily useful when compared to xERA to detect luck gaps (see below).

KEY DERIVED SIGNALS:
- Recent ERA << xERA (e.g., recent 2.00 vs xERA 3.75): pitcher is currently outperforming, hot streak → confidence signal for under or side bets
- Recent ERA >> xERA (e.g., recent 6.50 vs xERA 3.50): pitcher is struggling recently despite good xERA → caution; lean over for this game
- Season ERA << xERA (e.g., season ERA 3.00 but xERA 4.50): pitcher has been LUCKY all season; market sets total too low based on good-looking ERA → over opportunity
- Season ERA >> xERA (e.g., season ERA 5.50 but xERA 3.80): pitcher has been UNLUCKY; real skill will reassert → under opportunity, don't fade based on inflated ERA

═══════════════════════════════════════════
DISQUALIFYING FACTORS
═══════════════════════════════════════════
Any one of these eliminates the bet:
- Pitcher has "NO STATS" — never bet on an unknown first-time starter
- RAIN RISK flag — avoid any game with meaningful precipitation risk
- Bullpen xERA > 5.0 disqualifies full-game ML/spread for that team — but does NOT eliminate F5, pitcher props, team total over for the offense, or game total bets; route to the appropriate bet type instead
- Pitcher on fewer than 5 days rest AND had 100+ pitch count last start
- Recent ERA is 3+ runs higher than xERA (pitcher in acute struggle this month, not just bad luck)

═══════════════════════════════════════════
GAME TOTAL ANALYSIS (Over / Under)
═══════════════════════════════════════════

OVER signals (need 3+ for confidence):
1. Both starters have high recent ERA (≥4.50 over last 3 starts) — struggling RIGHT NOW
2. Both starters have high season xERA (≥4.25) — neither is an ace at baseline
3. Season ERA << xERA for either starter — market underestimates run risk (luck gap)
4. Both offenses are dangerous in L12 (wRC+ ≥110)
5. Bullpens are average or worse on both sides (xERA >4.00)
6. Park or weather favors offense (APF >105, wind blowing out)
7. Both teams averaging ≥9 combined runs in pitcher's recent starts

UNDER signals (need 3+ for confidence):
1. Both starters have low recent ERA (≤3.00 over last 3 starts) — sharp form right now
2. Both starters have low season xERA (≤3.75) — ace vs ace
3. Season ERA >> xERA for either starter — market overestimates run risk
4. Both offenses are cold in L12 (wRC+ ≤95)
5. Strong bullpens on both sides (xERA <3.75)
6. Park or weather favors pitchers (APF <95, wind blowing in, cold temps)

BULLPEN → FULL GAME VS F5:
- Default: full-game totals
- Stay full-game OVER if both bullpens are shaky — more bad innings = more runs
- Stay full-game UNDER if both bullpens are solid — they protect the low number through 9
- F5 only makes sense as a deliberate choice: e.g., elite starters + shaky pens (bet the starters, limit bullpen exposure)

═══════════════════════════════════════════
SIDE ANALYSIS (ML / Spread / Team Total)
═══════════════════════════════════════════

CRITICAL RULE: Match the bet type to the actual edge. A pitching edge is not an offensive edge. Don't express one as the other.

── PITCHING EDGE (one starter is clearly dominant) ──
Signals: better xERA, better recent form, good matchup history vs today's opponent, opposing wRC+ is weak.
The dominant starter IS the edge. The key question is: do you also like your team's offense vs the opposing pitcher?

IF you like the pitcher AND the team's offense vs the opposing starter (compound edge in first 5):
- Own bullpen strong: full-game ML or spread
- Own bullpen average/weak: F5 ML or F5 spread — captures both the dominant starter and the offense scoring, cuts off before your shaky pen enters

IF you like the pitcher but NOT the team's offense (or you're neutral on it):
- Opponent F5 UNDER team total — pure pitcher dominance bet; you're saying the opposing offense scores few runs in the first 5 innings, no dependency on your own offense or bullpen
- Opponent F5 under the total (F5 Under) also works if both pitchers are decent but one is clearly better
- Pitcher K or Outs prop — purest single-pitcher bet with zero team dependency
- Own bullpen strong: full-game ML or spread still works since the starter limits damage and the pen holds

── OFFENSIVE EDGE (one offense outmatches the opposing starter) ──
Signals: hot offense in L12 (wRC+ ≥110) vs a weak or struggling opposing pitcher (high xERA, high recent ERA), backed by good run-support trends.
Express this as scoring, not winning:

- Team total OVER for the hot offense — bet they score regardless of game outcome; own bullpen irrelevant
- F5 team total OVER — if you specifically like the offense vs this starter in the first 5 innings (before a better reliever might enter)
- If own pitcher is also decent: F5 ML or full-game ML becomes reasonable as a compound play

── COMPOUND EDGE (pitching + offense both favor one team in both directions) ──
Both a dominant starter AND a stronger offense vs a weaker opposing starter and weaker opposing offense — full-game ML or spread is justified. Still prefer F5 if own bullpen is shaky.

═══════════════════════════════════════════
PITCHER PROPS
═══════════════════════════════════════════

K props (Over): pitcher K% ≥23% AND opponent K% ≥22% AND going deep (≥6.0 IP/gs avg)
Outs O/U X (X÷3 = innings equivalent):
- Over if recent avg IP ≥ (X÷3)+0.5 AND avg pitch count ≥95
- Under if pitcher exits early recently, avg PC <85, or bullpen xERA >5 (trigger-happy manager)
- Never suggest props for NO STATS pitchers

═══════════════════════════════════════════
BET TYPE PRIORITY
═══════════════════════════════════════════
1. Full-game totals — most common strong play
2. Team totals — when one side has edge but bullpen is unreliable for ML
3. Full-game ML or spread — only with clear edge + reliable bullpen
4. F5 lines — deliberate choice only, not default
5. Pitcher props — when workload + matchup data strongly align

═══════════════════════════════════════════
LINE VALIDATION — the line is the truth
═══════════════════════════════════════════

The matchup analysis tells you the direction. The betting line tells you if there is actually edge to bet. ALWAYS validate the line against what your analysis implies. The line reflects sharp market opinion — when the line already prices in your edge, there is no bet.

GENERAL PRINCIPLE:
- You find a matchup you like. Then you look at the line. If the line is already set where you'd expect given the matchup, the market sees it too — no edge. Only bet when you believe the line is mispriced relative to the actual situation.

FOR PITCHER K PROPS specifically:
1. Find a good K matchup (high pitcher K%, high opponent K% vs this hand).
2. Look at the K line point (e.g., Over 7.5 Ks).
3. Check the pitcher's recent avg K/start AND who they faced. Each outing shows "Xk vs OPP" so you can see if those Ks came against high-K or low-K teams.
   - Calibrate for today's opponent: compare today's opponent K% (shown in OFFENSE section) to the typical K rates of the opponents the pitcher recently faced. If recent high Ks came against strikeout-prone lineups and today's opponent has a lower K rate, adjust expectations down. Conversely, if the pitcher was hitting good K numbers against contact-oriented teams and today's opponent strikes out more, the recent numbers understate what's likely today.
   - Apply this calibration to estimate an adjusted expected K total before comparing to the line.
4. Evaluate the adjusted K expectation vs the line point:
   - Adjusted expectation WELL ABOVE the line (by ≥1.5 Ks): strong Over signal.
   - Adjusted expectation NEAR the line (within 0.5–1.0): marginal. Only bet if both the pitcher's K% and today's opponent K% are clearly above average.
   - Adjusted expectation BELOW the line: the line is already pricing in more than the pitcher will likely deliver — lean Under or skip.
5. Cross-reference with Outs prop: if the K line seems low but the Outs line is ALSO low (short outing expected by the market), don't fight it — the pitcher may not get enough innings to accumulate Ks. A low K line with a normal/high Outs line is the green light — pitcher will have innings and the K line is simply undervalued.
6. A mediocre matchup with a clearly low K line (vs adjusted expectation) beats a great-looking matchup where the line already reflects the edge.

FOR GAME TOTALS:
- Great over matchup (bad starters + hot offenses) but total already at 11.5? The market sees it. Skip or find a different angle.
- Modest over matchup but total sitting at 7.5 for a game with two mediocre starters? That's the gap — that might be the play even though the matchup isn't flashy.
- Same logic applies to unders: if both pitchers are elite but the total is already 6.5, the market agrees with you. Look for 8.0+ totals with two good starters where the market seems to be ignoring the pitching.

FOR SIDE BETS (ML/Spread):
- Clear favorite on the mound and in the matchup, but the ML is -200? The market agrees completely. That's not a bet — it's just paying vig to be right.
- Find the game where the matchup favors one side but the line hasn't fully moved to reflect it. That's the value.

PRICING RULES (CRITICAL):
- NEVER suggest American odds more negative than -150
- If you like a play at -151 to -200: include in picks with "line_warning": true and "alt_suggestion" (e.g., "Try Ks Over 6.5 at a better price instead")
- Nothing at -201 or worse. No parlays.

MULTIPLE PICKS PER GAME: You may include more than one pick for the same game if multiple edges are independent (e.g., Game Total Under AND a pitcher K prop — different markets, different edges). Do not stack correlated bets on the same game.

If there are no strong plays, return an empty picks array.

When you have completed your analysis, call the report_betting_suggestions tool with your results.
"""


def _serialize_game_for_ai(g: dict) -> str:
    """Serialize a compiled game dict into a compact text block for the AI prompt."""
    away, home = g["away"], g["home"]
    sp_a = g["away_sp"]
    sp_h = g["home_sp"]
    of_a = g.get("away_off") or {}
    of_h = g.get("home_off") or {}
    bp_a = g.get("away_bp") or {}
    bp_h = g.get("home_bp") or {}
    od   = g.get("odds") or {}
    wx   = g.get("wx") or {}
    tr_a = g.get("away_trends") or {}
    tr_h = g.get("home_trends") or {}
    outs_a = g.get("away_sp_outings", [])
    outs_h = g.get("home_sp_outings", [])
    flags  = g.get("flags", [])

    # Time
    time_s = ""
    if g.get("game_date"):
        try:
            dt = datetime.fromisoformat(g["game_date"].replace("Z", "+00:00")).astimezone(_ET)
            h12 = dt.hour % 12 or 12
            time_s = f" | {h12}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'} ET"
        except Exception:
            pass

    # Venue / weather
    venue = g.get("venue", "")
    roof  = (wx.get("roof_status") or "").lower()
    if "dome" in roof or "retractable" in roof:
        venue_tag = "Dome"
    elif "closed" in roof:
        venue_tag = "Roof Closed"
    else:
        apf = wx.get("adjusted_park_factor")
        apf_s = f", APF {apf:.0f}" if apf else ""
        venue_tag = f"Open Air{apf_s}"

    wx_parts = []
    if wx.get("precip_risk_during_game"):
        prob = wx.get("precip_probability")
        wx_parts.append(f"RAIN RISK {prob:.0f}%" if prob else "RAIN RISK")
    elif (wx.get("precip_probability") or 0) >= 30:
        wx_parts.append(f"Rain {wx['precip_probability']:.0f}%")
    wind_lbl = wx.get("wind_effect_label", "")
    wind_mph = wx.get("wind_speed")
    if wind_lbl and wind_lbl not in ("Calm", "Indoor", ""):
        mph = f" {wind_mph:.0f}mph" if wind_mph else ""
        wx_parts.append(f"Wind: {wind_lbl}{mph}")
    wx_s = ", ".join(wx_parts) if wx_parts else "Clear/Calm"

    def _recent_stats(outings):
        """Compute recent ERA and avg Ks from last 3 starts."""
        starts = [o for o in outings[-3:] if flt(o.get("ip")) and not o.get("is_relief")]
        if not starts:
            return None, None
        total_ip = sum(flt(o["ip"]) or 0 for o in starts)
        total_er = sum(int(o["er"] or 0) for o in starts if o.get("er") is not None)
        k_vals = [o["k"] for o in starts if o.get("k") is not None]
        era_s = f"{total_er / total_ip * 9:.2f}" if total_ip > 0 else None
        avg_k  = f"{sum(k_vals) / len(k_vals):.1f}" if k_vals else None
        return era_s, avg_k

    def _sp_line(sp, outings):
        name = sp["name"]
        hand = (sp.get("hand") or "?")[0]
        if not sp.get("has_stats"):
            return f"  {name} ({hand}): NO STATS (first start this season)"
        parts = []
        if sp.get("label"):
            parts.append(f"xERA {sp['xera_s']} ({sp['label']})")
        else:
            parts.append(f"xERA {sp['xera_s']}")
        for key, lbl in [("k", "K%"), ("hard", "HH%"), ("bb", "BB%"), ("barrel", "Barrel%"), ("era_s", "ERA")]:
            val = sp.get(key)
            if val not in ("?", "—", None):
                parts.append(f"{lbl} {val}")
        if sp.get("depth") not in ("—", None):
            parts.append(sp["depth"])
        base = f"  {name} ({hand}): " + ", ".join(parts)
        if outings:
            outing_strs = []
            for o in outings[-3:]:
                pc_s = f"/{o['pc']}pc" if o.get("pc") else ""
                er = o.get("er") if o.get("er") is not None else "?"
                k_s = f"/{o['k']}K" if o.get("k") is not None else ""
                opp_s = f" vs {o['opp']}" if o.get("opp") and o["opp"] != "?" else ""
                outing_strs.append(f"{o['ip']}IP/{er}ER{k_s}{opp_s}{pc_s}")
            recent_era, avg_k = _recent_stats(outings)
            trajectory = ""
            if recent_era and sp.get("xera_s") not in ("?", None):
                xera_f = flt(sp["xera_s"])
                rec_f  = flt(recent_era)
                if xera_f and rec_f:
                    diff = rec_f - xera_f
                    if diff <= -1.0:
                        trajectory = " ↑ HOT"
                    elif diff >= 1.0:
                        trajectory = " ↓ STRUGGLING"
            k_context = f", avg {avg_k} K/start" if avg_k else ""
            base += (
                f"\n    Recent 3: {' | '.join(outing_strs)}"
                f" — recent ERA {recent_era or '?'}{trajectory}{k_context}"
            )
        return base

    def _off_line(team, off, vs_hand):
        if not off:
            return f"  {team} vs {vs_hand}HP: No data"
        lbl = f" ({off['label']})" if off.get("label") else ""
        parts = [f"wRC+ {off.get('wrc_s', '?')}{lbl}"]
        if off.get("k") not in ("?", None):    parts.append(f"K% {off['k']}")
        if off.get("hard") not in ("?", None): parts.append(f"HH% {off['hard']}")
        return f"  {team} vs {vs_hand}HP: " + ", ".join(parts)

    def _bp_line(team, bp):
        if not bp:
            return f"  {team}: No data"
        parts = [f"xERA {bp.get('xera_s', '?')}"]
        era_s = bp.get("era_s")
        if era_s not in ("?", "N/A", None):
            parts.append(f"ERA {era_s}")
        return f"  {team}: " + ", ".join(parts)

    def _trend_line(team, tr):
        if not tr:
            return f"  {team}: No trend data"
        side = "home" if tr.get("is_home") else "away"
        w10, l10 = tr["last10"]
        ws10, ls10 = tr["last10_side"]
        parts = [f"{w10}-{l10} L{tr['n_last10']}", f"{ws10}-{ls10} {side} L{tr['n_side10']}"]
        w5, l5 = tr["last5"]
        if tr["n_last5"]:
            parts.append(f"{w5}-{l5} SP L{tr['n_last5']}")
            if tr["avg_runs"] is not None:
                parts.append(f"avg {tr['avg_runs']:.1f} RS")
        ws5, ls5 = tr["last5_side"]
        if tr["n_side5"]:
            parts.append(f"{ws5}-{ls5} SP {side} L{tr['n_side5']}")
            if tr["avg_runs_side"] is not None:
                parts.append(f"{side} avg {tr['avg_runs_side']:.1f} RS")
        return f"  {team}: " + ", ".join(parts)

    hand_h = (sp_h.get("hand") or "?")[0]  # away offense bats vs home pitcher
    hand_a = (sp_a.get("hand") or "?")[0]  # home offense bats vs away pitcher

    odds_lines = []
    def _o(s): return s if s and s != "—" else None
    if _o(od.get("away_ml")):
        odds_lines.append(f"  ML: {away} {od['away_ml']} / {home} {od['home_ml']}")
    if _o(od.get("away_spread")):
        odds_lines.append(f"  Spread: {away} {od['away_spread']} / {home} {od['home_spread']}")
    if _o(od.get("over")):
        odds_lines.append(f"  Total: {od['over']} / {od['under']}")
    if od.get("has_f5"):
        if _o(od.get("away_f5_ml")):
            odds_lines.append(f"  F5 ML: {away} {od['away_f5_ml']} / {home} {od['home_f5_ml']}")
        if _o(od.get("f5_over")):
            odds_lines.append(f"  F5 Total: {od['f5_over']} / {od['f5_under']}")
        if _o(od.get("away_f5_spread")):
            odds_lines.append(f"  F5 Spread: {away} {od['away_f5_spread']} / {home} {od['home_f5_spread']}")
    if od.get("has_tt"):
        if _o(od.get("away_tt_over")):
            odds_lines.append(f"  {away} Team Total: {od['away_tt_over']} / {od['away_tt_under']}")
        if _o(od.get("home_tt_over")):
            odds_lines.append(f"  {home} Team Total: {od['home_tt_over']} / {od['home_tt_under']}")
    if od.get("has_f5tt"):
        if _o(od.get("away_f5tt_over")):
            odds_lines.append(f"  {away} F5 Team Total: {od['away_f5tt_over']} / {od['away_f5tt_under']}")
        if _o(od.get("home_f5tt_over")):
            odds_lines.append(f"  {home} F5 Team Total: {od['home_f5tt_over']} / {od['home_f5tt_under']}")
    k_a  = _fmt_k_line(od.get("away_k"))
    k_h  = _fmt_k_line(od.get("home_k"))
    ou_a = _fmt_outs_line(od.get("away_outs"))
    ou_h = _fmt_outs_line(od.get("home_outs"))
    prop_parts = []
    if k_a or ou_a:
        prop_parts.append(f"{sp_a['name']}: {', '.join(p for p in [k_a, ou_a] if p)}")
    if k_h or ou_h:
        prop_parts.append(f"{sp_h['name']}: {', '.join(p for p in [k_h, ou_h] if p)}")
    if prop_parts:
        odds_lines.append("  Props: " + " | ".join(prop_parts))

    # Matchup-specific splits
    spl_a = g.get("away_sp_splits") or {}
    spl_h = g.get("home_sp_splits") or {}

    def _spl_line(name, spl, vs_label, venue_label):
        parts = []
        vs = spl.get("vs")
        if vs:
            parts.append(
                f"vs {vs_label}: {vs['n']}gs, {vs['era']} ERA, {vs['ip']} IP/gs, {vs['k']} K/gs"
            )
        else:
            parts.append(f"vs {vs_label}: no data")
        at = spl.get("at")
        if at:
            parts.append(
                f"at {venue_label}: {at['n']}gs, {at['era']} ERA, {at['ip']} IP/gs"
            )
        else:
            parts.append(f"at {venue_label}: no data")
        return f"  {name}: " + " | ".join(parts)

    lines = [f"=== {away} @ {home}{time_s} | {venue} ({venue_tag}) ==="]
    lines.append(f"Weather: {wx_s}")
    lines.append("PITCHERS:")
    lines.append(_sp_line(sp_a, outs_a))
    lines.append(_sp_line(sp_h, outs_h))
    lines.append("OFFENSE (L12):")
    lines.append(_off_line(away, of_a, hand_h))
    lines.append(_off_line(home, of_h, hand_a))
    lines.append("BULLPEN (L12):")
    lines.append(_bp_line(away, bp_a))
    lines.append(_bp_line(home, bp_h))
    lines.append("MATCHUP HISTORY (last 3 starts, 2yr):")
    lines.append(_spl_line(sp_a["name"], spl_a, home, "this park"))
    lines.append(_spl_line(sp_h["name"], spl_h, away, "home"))
    lines.append("ODDS:")
    lines.extend(odds_lines if odds_lines else ["  None available"])
    lines.append("TEAM TRENDS (in this starter's recent starts):")
    lines.append(_trend_line(away, tr_a))
    lines.append(_trend_line(home, tr_h))
    if flags:
        lines.append("FLAGS:")
        lines.extend(f"  {f}" for f in flags)
    return "\n".join(lines)


def generate_suggestions(games: list[dict], data_dir: Path, target_date: "date") -> Optional[dict]:
    """
    Call Claude to generate betting suggestions. Caches to data/suggestions_{date}.json
    and regenerates whenever odds are updated. Returns parsed dict or None on failure.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    sugg_path = data_dir / f"suggestions_{date_str}.json"
    sugg_meta = data_dir / f"suggestions_meta_{date_str}.json"
    odds_meta  = data_dir / f"odds_meta_{date_str}.json"

    # Serve cached result if it's still fresh (generated after last odds update)
    if sugg_path.exists() and sugg_meta.exists():
        try:
            s_ts = datetime.fromisoformat(json.loads(sugg_meta.read_text())["generated_at"])
            if odds_meta.exists():
                o_ts = datetime.fromisoformat(json.loads(odds_meta.read_text())["fetched_at"])
                if s_ts >= o_ts:
                    return json.loads(sugg_path.read_text())
            else:
                from datetime import timezone as _tz
                if (datetime.now(_tz.utc) - s_ts).total_seconds() < 14400:
                    return json.loads(sugg_path.read_text())
        except Exception:
            pass

    try:
        import anthropic as _ant
    except ImportError:
        print("[suggestions] anthropic package not installed — skipping", file=__import__("sys").stderr)
        return None

    api_key = ""
    try:
        import config as _cfg
        api_key = _cfg.ANTHROPIC_API_KEY
    except Exception:
        pass
    if not api_key:
        import os as _os
        api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[suggestions] ANTHROPIC_API_KEY not set — skipping", file=__import__("sys").stderr)
        return None

    if not games:
        return None

    # Only analyze games that haven't started yet — no point betting on live/finished games
    from datetime import timezone as _tz2
    _now = datetime.now(_tz2.utc)
    unstarted = []
    for _g in games:
        _gt = _g.get("game_time_utc", "")
        if _gt:
            try:
                if datetime.fromisoformat(_gt.replace("Z", "+00:00")) > _now:
                    unstarted.append(_g)
                    continue
            except Exception:
                pass
        unstarted.append(_g)  # no time = include by default
    if not unstarted:
        print("[suggestions] All games have started — skipping AI call", file=__import__("sys").stderr)
        return json.loads(sugg_path.read_text()) if sugg_path.exists() else None

    n_skipped = len(games) - len(unstarted)
    if n_skipped:
        print(f"[suggestions] Skipping {n_skipped} already-started game(s)", file=__import__("sys").stderr)

    game_blocks = "\n\n".join(_serialize_game_for_ai(g) for g in unstarted)
    user_msg = (
        f"Today is {date_str}. Analyze these {len(unstarted)} MLB games and "
        f"identify any strong betting opportunities:\n\n{game_blocks}"
    )

    # Tool schema guarantees structured output — no JSON text parsing needed
    _tool = {
        "name": "report_betting_suggestions",
        "description": "Submit today's MLB betting suggestions after completing analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "picks": {
                    "type": "array",
                    "description": "All bets to recommend today. Can include multiple picks for the same game. Empty array if no bets.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "game":        {"type": "string", "description": "Exactly as shown in game header, e.g. 'TEX @ MIA'"},
                            "bet_type":    {"type": "string", "description": "e.g. Total, Spread, ML, F5_Total, F5_ML, F5_Spread, Team_Total, Pitcher_Ks, Pitcher_Outs"},
                            "bet":         {"type": "string", "description": "Full bet description, e.g. 'Game Total Under 8.5' or 'NYY -1.5'"},
                            "team_side":   {
                                "type": ["string", "null"],
                                "enum": ["away", "home", "over", "under", "away_over", "away_under", "home_over", "home_under", None],
                                "description": "Which side: 'over'/'under' for totals; 'away'/'home' for ML/spread; 'away_over' etc for team totals; null for props",
                            },
                            "line":        {"type": ["number", "null"], "description": "Numeric line: total line (e.g. 8.5), spread line (e.g. -1.5 for favorite), null for ML"},
                            "period":      {"type": "string", "enum": ["full_game", "f5", "props"], "description": "full_game, f5 (first 5 innings), or props"},
                            "odds":        {"type": "string", "description": "American odds string, e.g. '-110' or '+145'"},
                            "odds_num":    {"type": ["integer", "null"], "description": "Odds as integer, e.g. -110 or 145"},
                            "confidence":  {"type": "string", "enum": ["high", "medium"]},
                            "reason":      {"type": "string"},
                            "line_warning":   {"type": "boolean"},
                            "alt_suggestion": {"type": ["string", "null"]},
                        },
                        "required": ["game", "bet_type", "bet", "team_side", "line", "period", "odds", "confidence", "reason"],
                    },
                },
                "pass_reasons": {
                    "type": "object",
                    "description": "Key = game header exactly (e.g. 'TEX @ MIA'). Value = 1-sentence reason why no bet. Include every game not in picks.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["picks", "pass_reasons"],
        },
    }

    try:
        client = _ant.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=_AI_SYSTEM_PROMPT,
            tools=[_tool],
            tool_choice={"type": "tool", "name": "report_betting_suggestions"},
            messages=[{"role": "user", "content": user_msg}],
        )
        tool_block = next((b for b in response.content if getattr(b, "type", "") == "tool_use"), None)
        if not tool_block:
            print("[suggestions] No tool_use block in response", file=__import__("sys").stderr)
            return None
        result = tool_block.input
    except Exception as e:
        print(f"[suggestions] API error: {e}", file=__import__("sys").stderr)
        return None

    try:
        from datetime import timezone as _tz
        sugg_path.write_text(json.dumps(result, indent=2))
        sugg_meta.write_text(json.dumps({"generated_at": datetime.now(_tz.utc).isoformat()}))
    except Exception:
        pass

    return result


def _render_suggestions_html(all_picks: list, target_date: "date") -> str:
    """Render the global AI Picks section. Returns '' if no picks."""
    date_s = target_date.strftime(f"%b {target_date.day}")
    n_bets = len(all_picks)
    if not n_bets:
        return ""

    now = datetime.now(timezone.utc)

    def _game_dt(pick):
        gt = pick.get("game_time_utc", "")
        if not gt:
            return datetime.max.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(gt.replace("Z", "+00:00"))
        except Exception:
            return datetime.max.replace(tzinfo=timezone.utc)

    active_picks  = sorted([p for p in all_picks if _game_dt(p) > now],  key=_game_dt)
    started_picks = sorted([p for p in all_picks if _game_dt(p) <= now], key=_game_dt)

    def _pick_block(pick: dict) -> str:
        reason  = _h(pick.get("reason", ""))
        warn    = pick.get("line_warning")
        alt     = pick.get("alt_suggestion")
        warn_s  = (f'<div class="ai-line-warn">Line Warning: {_h(alt)}</div>'
                   if warn and alt else "")
        found   = pick.get("found_at", "")
        found_s = ""
        if found:
            try:
                _ft = datetime.fromisoformat(found).astimezone(_ET)
                _ft_s = f"{int(_ft.strftime('%I'))}:{_ft.strftime('%M %p')}"
                found_s = f'<div class="ai-found-at">Found at {_h(_ft_s)} ET</div>'
            except Exception:
                pass
        title   = _h(_pick_summary_title(pick))
        pid     = _pick_dom_id(pick)
        return (
            f'<details class="ai-pick-row" id="{pid}">'
            f'<summary class="ai-pick-sum">{title}</summary>'
            f'<div class="ai-pick-body">'
            f'<div class="ai-reason">{reason}</div>'
            f'{found_s}'
            f'{warn_s}'
            f'</div>'
            f'</details>'
        )

    inner = ""
    if active_picks:
        inner += (
            f'<div class="ai-active-wrap">'
            f'{"".join(_pick_block(p) for p in active_picks)}'
            f'</div>'
        )
    if started_picks:
        inner += (
            f'<div class="ai-started-wrap">'
            f'<div class="ai-started-label">Games In Progress / Completed</div>'
            f'{"".join(_pick_block(p) for p in started_picks)}'
            f'</div>'
        )

    bets_lbl = f"{n_bets} Bet{'s' if n_bets != 1 else ''}"
    disclaimer = (
        '<div class="ai-disclaimer">'
        'AI-generated · For entertainment only · Not financial advice'
        '</div>'
    )

    return (
        f'<details class="ai-picks" id="ai-picks-card">'
        f'<summary class="ai-picks-hd">AI Picks · {_h(bets_lbl)} · {_h(date_s)}</summary>'
        f'{inner}'
        f'{disclaimer}'
        f'</details>'
    )


def _ai_game_map(valid_picks: list, suggestions: Optional[dict]) -> dict:
    """
    Build per-game AI lookup: {"AWAY @ HOME": {"picks": [...], "pass_reason": str|None}}.
    valid_picks: all saved picks for the day (includes started games).
    suggestions: latest run result, used only for pass_reasons on games with no picks.
    """
    picks_by_game: dict[str, list] = {}
    for p in (valid_picks or []):
        game = p.get("game", "")
        if game:
            picks_by_game.setdefault(game, []).append(p)

    pass_reasons = (suggestions or {}).get("pass_reasons") or {}

    # Also handle old-schema suggestions fallback for pass_reasons
    if not pass_reasons and suggestions:
        old_best = suggestions.get("best_bet")
        old_others = suggestions.get("other_bets") or []
        bet_games = set()
        if old_best and old_best.get("game"):
            bet_games.add(old_best["game"])
        for o in old_others:
            if o.get("game"):
                bet_games.add(o["game"])
        pass_reasons = {
            k: v for k, v in (suggestions.get("pass_reasons") or {}).items()
        }

    result: dict = {}
    for game, picks in picks_by_game.items():
        result[game] = {"picks": picks, "pass_reason": None}
    for game, reason in pass_reasons.items():
        if game not in result:
            result[game] = {"picks": [], "pass_reason": reason}
    return result


def render_html_page(games: list[dict], target_date: date, generated_at: str,
                     odds_at: str = "", suggestions: Optional[dict] = None,
                     valid_picks: Optional[list] = None) -> str:
    date_long = target_date.strftime(f"%A, %B {target_date.day}, %Y")
    date_short = target_date.strftime(f"%b {target_date.day}")
    games = sorted(games, key=_time_sort_key)
    valid_picks = valid_picks or []
    ai_by_game = _ai_game_map(valid_picks, suggestions)
    cards = "".join(_html_game(g, ai_by_game.get(f"{g['away']} @ {g['home']}")) for g in games)
    gen_span  = _ts_span(generated_at)
    odds_sub  = f" · Odds Updated {_ts_span(odds_at)}" if odds_at else ""
    ai_html   = _render_suggestions_html(valid_picks, target_date)
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>MLB Game Overviews · {_h(date_short)}</title>\n'
        f'<style>{_CSS}</style>\n'
        f'</head>\n<body>\n'
        f'<header><h1>MLB Game Overviews</h1>'
        f'<p class="sub">{_h(date_long)}</p>'
        f'<p class="sub">Updated {gen_span}{odds_sub}</p></header>\n'
        f'<main>{ai_html}{cards}\n</main>'
        f'<footer style="text-align:center;padding:1.5rem 1rem;font-size:.75rem;color:#9ca3af">'
        f'Powered by <a href="https://handigraphs.com" target="_blank" rel="noopener" style="color:#9ca3af">Handigraphs</a>'
        f'</footer>'
        f'{_SPLIT_SCRIPT}\n</body>\n</html>'
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _use_color

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
    args = ap.parse_args()

    if args.no_color or args.html:
        _use_color = False

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
    starters = load_starters(data_dir, target_date)
    rhp, lhp = load_team_stats(data_dir, target_date)
    bp = load_bullpen(data_dir, target_date)
    ballpark_wx = {} if args.no_weather else load_ballpark_weather(data_dir, target_date)
    games = build_games(starters)

    if not games:
        sys.exit("No games found in starters CSV.")

    # Filter by team
    if args.game:
        team_filter = args.game.upper()
        games = [(p1, p2) for p1, p2 in games
                 if team_filter in (p1.get("Team", ""), p2.get("Team", ""))]
        if not games:
            sys.exit(f"No games found for '{team_filter}'.")

    # MLB schedule (home/away, venue)
    mlb_schedule: dict = {}
    if not args.no_mlb and HAS_REQUESTS:
        _log("Fetching MLB schedule...")
        mlb_schedule = get_mlb_schedule(target_date)
        _log(f"  {len(mlb_schedule)} games found")

    if not args.html:
        print(bold(f"\n{'━'*64}"))
        print(bold(f"  MLB Handicap — {target_date.strftime('%A, %B %d %Y')}"))
        print(bold(f"{'━'*64}"))

    odds_data = load_odds(data_dir, target_date)
    _log(f"Odds: {len(odds_data)} games loaded" if odds_data else "Odds: no file found")
    odds_at   = load_odds_meta(data_dir, target_date)
    props_data = load_pitcher_props(data_dir, target_date)
    _log(f"Props: {len(props_data)} games loaded" if props_data else "Props: no file found")

    game_data: list[dict] = []
    for p1, p2 in games:
        t1_mlb = to_mlb(p1.get("Team", ""))
        t2_mlb = to_mlb(p2.get("Team", ""))
        key = frozenset([t1_mlb, t2_mlb])

        mlb_info = mlb_schedule.get(key, {})

        # Skip games not on today's MLB schedule — guards against stale or multi-date starters data
        if mlb_schedule and not mlb_info:
            _log(f"  Skipping {p1.get('Team','')} @ {p2.get('Team','')}: not on today's schedule")
            continue

        if not args.no_mlb and HAS_REQUESTS:
            for p in (p1, p2):
                pid = p.get("mlbam_id")
                team = p.get("Team", "")
                if pid and team:
                    mlb_info[f"history_{team}"] = get_recent_starts(int(pid))
            away_id = mlb_info.get("away_mlb_id")
            home_id = mlb_info.get("home_mlb_id")
            if away_id:
                mlb_info["away_record"] = get_team_schedule(int(away_id), target_date.year)
            if home_id:
                mlb_info["home_record"] = get_team_schedule(int(home_id), target_date.year)

        # Ballpark weather — keyed by raw team codes (same as Handigraphs starters JSON)
        t1_raw = p1.get("Team", "")
        t2_raw = p2.get("Team", "")
        wx = ballpark_wx.get(frozenset([t1_raw, t2_raw]), {})
        # Fallback to Open-Meteo if Handigraphs weather file wasn't downloaded
        if not wx and not args.no_weather and HAS_REQUESTS:
            home_t = mlb_info.get("home", t2_raw)
            wx = get_weather(home_t, target_date)

        if args.html:
            g = analyze_game(p1, p2, rhp, lhp, bp, mlb_info, wx, target_date)
            g["odds"] = get_game_odds(odds_data, g["away"], g["home"],
                                       g["away_sp"]["name"], g["home_sp"]["name"],
                                       props_data)
            # Add commence_time from odds for AI filtering and picks display
            away_full = _ODDS_TEAM.get(g["away"], "")
            home_full = _ODDS_TEAM.get(g["home"], "")
            raw_game = odds_data.get((away_full, home_full)) or odds_data.get((home_full, away_full)) or {}
            g["game_time_utc"] = raw_game.get("commence_time", "")
            game_data.append(g)
        else:
            print_game(p1, p2, rhp, lhp, bp, mlb_info, wx)

    if args.html:
        from datetime import timezone as _tz
        generated_at = datetime.now(_tz.utc).isoformat()
        suggestions = generate_suggestions(game_data, data_dir, target_date)
        # Load all picks for the day (including started/completed games)
        try:
            from picks import load_all_picks as _lap
            picks_dir = Path("./picks")
            all_picks = _lap(picks_dir, target_date)
        except Exception:
            all_picks = []
        print(render_html_page(game_data, target_date, generated_at, odds_at,
                               suggestions, all_picks))
    else:
        print()


if __name__ == "__main__":
    main()
