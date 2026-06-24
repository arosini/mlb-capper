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
import sys
from datetime import date, timedelta, datetime
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

def load_odds(data_dir: Path, target_date: date) -> dict:
    """Load Odds API JSON; returns {(away_name, home_name): game_dict}."""
    p = _find_file(data_dir, "odds", target_date, "json")
    if not p:
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

def _find_pitcher_kline(bookmakers: list, pitcher_name: str) -> Optional[dict]:
    """Find best-priced K over/under line for a named pitcher across bookmakers."""
    if not pitcher_name or pitcher_name in ("TBD", ""):
        return None
    last = pitcher_name.strip().split()[-1].lower()
    best_over: Optional[dict] = None
    best_under: Optional[dict] = None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != "pitcher_strikeouts":
                continue
            for oc in mkt.get("outcomes", []):
                if last not in oc.get("name", "").lower():
                    continue
                desc = (oc.get("description") or "").lower()
                p, pt = oc.get("price"), oc.get("point")
                if "over" in desc:
                    if p is not None and (best_over is None or p > best_over["price"]):
                        best_over = {"point": pt, "price": p}
                elif "under" in desc:
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
    pt = f"+{point}" if point > 0 else str(point)
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

def get_game_odds(odds_data: dict, away_code: str, home_code: str,
                  away_sp_name: str = "", home_sp_name: str = "") -> Optional[dict]:
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

    # F5
    away_f5_sp_pt, away_f5_sp_pr = _best_spread(bks, away_name, "spreads_1st_5_innings")
    home_f5_sp_pt, home_f5_sp_pr = _best_spread(bks, home_name, "spreads_1st_5_innings")
    f5_over_pt, f5_over_pr   = _best_total(bks, "Over",  "totals_1st_5_innings")
    f5_under_pt, f5_under_pr = _best_total(bks, "Under", "totals_1st_5_innings")
    has_f5 = any(v is not None for v in (away_f5_sp_pt, f5_over_pt,
                                          _best_price(bks, "h2h_1st_5_innings", away_name)))

    # Pitcher K lines
    away_k = _find_pitcher_kline(bks, away_sp_name)
    home_k = _find_pitcher_kline(bks, home_sp_name)

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
        "away_f5_ml":    _fmt_ml(_best_price(bks, "h2h_1st_5_innings", away_name)),
        "home_f5_ml":    _fmt_ml(_best_price(bks, "h2h_1st_5_innings", home_name)),
        "away_f5_spread":_fmt_spread(away_f5_sp_pt, away_f5_sp_pr),
        "home_f5_spread":_fmt_spread(home_f5_sp_pt, home_f5_sp_pr),
        "f5_over":       _fmt_total("O", f5_over_pt, f5_over_pr),
        "f5_under":      _fmt_total("U", f5_under_pt, f5_under_pr),
        # Pitcher K lines
        "away_k":        away_k,
        "home_k":        home_k,
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
        "Hard-Hit%": r.get("hard_perc"),
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
    """Return odds fetch time as 'H:MM AM/PM ET', or '' if not found."""
    p = data_dir / f"odds_meta_{target_date.strftime('%Y-%m-%d')}.json"
    if not p.exists():
        return ""
    try:
        meta = json.loads(p.read_text())
        fetched_at = meta.get("fetched_at", "")
        if fetched_at:
            dt = datetime.fromisoformat(fetched_at)
            try:
                from zoneinfo import ZoneInfo
                et = dt.astimezone(ZoneInfo("America/New_York"))
                return et.strftime("%-I:%M %p ET")
            except Exception:
                return dt.strftime("%H:%M UTC")
    except Exception:
        pass
    return ""

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


# ── Pitcher flags (from CSV data) ─────────────────────────────────────────────
def pitcher_csv_flags(row: dict) -> list[str]:
    flags = []
    status = row.get("lineup_status", "")
    if status and status not in ("confirmed", "expected", ""):
        flags.append(f"lineup status '{status}' — confirm this pitcher is actually starting")
    ip    = flt(row.get("IP"))
    era   = flt(row.get("ERA"))
    xera  = flt(row.get("xERA"))
    hh    = flt(row.get("Hard-Hit%", ""))
    barrel= flt(row.get("Barrel%", ""))
    kbb   = flt(row.get("K-BB%", ""))
    lob   = flt(row.get("LOB%", ""))
    ogs   = flt(row.get("Outs/GS"))

    if ip is None and xera is None:
        flags.append("no stats — likely TBD or skipping this start")
        return flags

    if ip is not None:
        if ip < 9:
            flags.append(f"tiny sample ({ip:.1f} IP over 3 starts) — xERA is unreliable")
        elif ip < 13:
            flags.append(f"small sample ({ip:.1f} IP over 3 starts)")

    if era is not None and xera is not None:
        gap = era - xera
        if gap > 2.5:
            flags.append(
                f"ERA {era:.2f} >> xERA {xera:.2f} — likely unlucky; "
                "may be better than ERA shows (check LOB%/BABIP)"
            )
        elif gap < -2.5:
            flags.append(
                f"ERA {era:.2f} << xERA {xera:.2f} — overperforming; "
                "regression risk"
            )

    if hh is not None and hh > 45:
        flags.append(f"getting squared up (Hard-Hit% {hh:.0f}%)")
    if barrel is not None and barrel > 15:
        flags.append(f"elevated Barrel% {barrel:.0f}%")
    if kbb is not None and kbb < 5:
        flags.append(f"poor K-BB% {kbb:.0f}% — limited command/stuff separation")
    if lob is not None and lob > 85:
        flags.append(f"high LOB% {lob:.0f}% — ERA flattered by strand luck")
    if lob is not None and lob < 48:
        flags.append(f"low LOB% {lob:.0f}% — ERA penalized by bad sequencing")
    if ogs is not None and (ogs / 3) < 4.0:
        flags.append(f"averaging only {ogs/3:.1f} IP/start — heavy bullpen usage")

    return flags


def bullpen_flags(row: dict) -> list[str]:
    flags = []
    era  = flt(row.get("ERA"))
    xera = flt(row.get("xERA"))
    if xera is not None and xera > 5.0:
        flags.append(f"bullpen is a liability (xERA {xera:.2f})")
    if era is not None and xera is not None:
        gap = era - xera
        if gap > 2.0:
            flags.append(f"bullpen ERA {era:.2f} >> xERA {xera:.2f} — may be getting unlucky")
        elif gap < -2.0:
            flags.append(f"bullpen ERA {era:.2f} << xERA {xera:.2f} — ERA flatters the pen")
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
                "venue": g.get("venue", {}).get("name", ""),
                "home_pid": hp.get("id"), "home_pname": hp.get("fullName", ""),
                "away_pid": ap.get("id"), "away_pname": ap.get("fullName", ""),
            }
    return games


def get_recent_starts(player_id: int) -> list[dict]:
    if not HAS_REQUESTS or not player_id:
        return []
    try:
        r = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "season": 2026, "group": "pitching"},
            timeout=10,
        )
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        return [s for s in splits if flt(s.get("stat", {}).get("inningsPitched")) is not None][-6:]
    except Exception:
        return []


def pitcher_history_flags(starts: list[dict]) -> list[str]:
    """Derive context flags from MLB game log entries."""
    flags = []
    if not starts:
        return flags

    # Check for missed turn (gap > 10 days between consecutive starts)
    dates = []
    for s in starts:
        d = s.get("date") or s.get("game", {}).get("officialDate", "")
        try:
            dates.append(datetime.strptime(d[:10], "%Y-%m-%d").date())
        except (ValueError, TypeError):
            pass
    if len(dates) >= 2:
        for a, b in zip(sorted(dates), sorted(dates)[1:]):
            if (b - a).days > 10:
                flags.append(f"missed a turn ({(b-a).days}-day gap between starts)")
                break

    # Check last start
    last = starts[-1].get("stat", {})
    ip = flt(last.get("inningsPitched"))
    er = flt(last.get("earnedRuns"))
    pitches = last.get("numberOfPitches")

    if pitches is not None:
        pitches = int(pitches)
        if pitches < 80:
            flags.append(f"low pitch count last start ({pitches} pitches) — may have been managing something")
        elif pitches > 100:
            flags.append(f"high pitch count last start ({pitches} pitches) — monitor workload today")

    # xERA outlier check: look at all starts in the 3-game window and flag if one outing
    # is dramatically inflating the aggregate xERA
    game_starts = [s for s in starts if int(s.get("stat", {}).get("gamesStarted", 0)) > 0]
    recent_3 = game_starts[-3:]
    if len(recent_3) >= 2:
        outings = []
        for s in recent_3:
            stat = s.get("stat", {})
            oip = flt(stat.get("inningsPitched"))
            oer = int(stat.get("earnedRuns") or 0)
            d = (s.get("date") or s.get("game", {}).get("officialDate", ""))[:10]
            if oip and oip > 0:
                outings.append({"ip": oip, "er": oer, "era_eq": (oer / oip) * 9, "date": d})

        if len(outings) >= 2:
            worst = max(outings, key=lambda x: x["era_eq"])
            others = [o for o in outings if o is not worst]
            avg_other_era = sum(o["era_eq"] for o in others) / len(others)

            # Flag if worst start was a disaster AND the other starts were genuinely clean
            # (avg_other_era <= 3.75 avoids false positives when all starts were mediocre)
            if worst["era_eq"] >= 9.0 and avg_other_era <= 3.75:
                other_label = "other start" if len(others) == 1 else f"other {len(others)} starts"
                # Skip ERA equiv display when IP is too small to be meaningful
                if worst["ip"] >= 2.0:
                    outing_str = (
                        f"{worst['er']} ER in {worst['ip']:.1f} IP "
                        f"(ERA equiv {worst['era_eq']:.0f})"
                    )
                else:
                    outing_str = f"{worst['er']} ER in just {worst['ip']:.1f} IP"
                flags.append(
                    f"{worst['date']}: {outing_str} is inflating 3-game xERA — "
                    f"{other_label} averaged {avg_other_era:.2f} ERA equiv"
                )

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
        return {
            "city": city,
            "rain_pct": (daily.get("precipitation_probability_max") or [None])[0],
            "temp_f":   (daily.get("temperature_2m_max") or [None])[0],
            "wind_mph": (daily.get("windspeed_10m_max") or [None])[0],
        }
    except Exception:
        return {}

def weather_flags(wx: dict) -> list[str]:
    """Generate flags from Handigraphs ballpark-weather data."""
    flags = []
    if not wx:
        return flags

    apf         = wx.get("adjusted_park_factor")
    pitch_cond  = wx.get("pitching_conditions", "Neutral")
    hit_cond    = wx.get("hitting_conditions", "Average")
    precip_risk = wx.get("precip_risk_during_game", False)
    precip_prob = wx.get("precip_probability")
    wind_lbl    = wx.get("wind_effect_label", "")
    wind_speed  = wx.get("wind_speed")
    roof        = wx.get("roof_status", "")
    is_outdoor  = roof not in ("Dome", "Roof Closed")

    # Park factor — flag when meaningfully off neutral
    if apf is not None:
        if apf >= 108:
            flags.append(
                f"extreme hitter's park (adj factor {apf:.0f}) — "
                f"xERAs will play higher than usual"
            )
        elif apf >= 104 and hit_cond not in ("Average",):
            flags.append(f"hitter-friendly park today (adj factor {apf:.0f})")
        elif apf <= 92:
            flags.append(
                f"extreme pitcher's park (adj factor {apf:.0f}) — "
                f"xERAs will play lower than usual"
            )

    # Pitching conditions label (combines park + weather)
    if pitch_cond == "Hitter Friendly":
        apf_s = f" (adj factor {apf:.0f})" if apf is not None else ""
        flags.append(
            f"hitter-friendly conditions{apf_s} — "
            f"adjust pitcher xERA expectations up"
        )

    # Weather — only relevant for outdoor/open-roof parks
    if is_outdoor:
        if precip_risk:
            prob_s = f" ({precip_prob:.0f}%)" if precip_prob is not None else ""
            flags.append(f"rain risk{prob_s} — possible delay or postponement")
        elif precip_prob is not None and precip_prob >= 25:
            flags.append(f"rain chance {precip_prob:.0f}%")

        if wind_lbl and wind_lbl not in ("Calm", "Indoor", ""):
            speed_s = f" {wind_speed:.0f} mph" if wind_speed is not None else ""
            flags.append(f"wind: {wind_lbl}{speed_s} — check direction for park impact")

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


# ── Per-game output ───────────────────────────────────────────────────────────
def analyze_game(
    p1: dict, p2: dict,
    rhp: dict, lhp: dict,
    bullpen: dict,
    mlb_info: dict,
    wx: dict,
) -> dict:
    """Return structured analysis dict — used by both terminal and HTML renderers."""
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
        return {
            "name":   p.get("Name", "TBD"),
            "hand":   hand,
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
        }

    def _off(batting: str, pitcher: dict) -> Optional[dict]:
        hand = (pitcher.get("Throws") or "?")[0]
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
            "hard":     fp1(s.get("HardHit%", s.get("Hard-Hit%"))),
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

    flags: list[str] = []
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        name = p.get("Name", "?")
        for f in pitcher_csv_flags(p):
            flags.append(f"{team} — {name}: {f}")
    for team in [away_team, home_team]:
        b = bullpen.get(team, bullpen.get(to_stats(team), {}))
        for f in bullpen_flags(b):
            flags.append(f"{team} bullpen: {f}")
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        for f in pitcher_history_flags(mlb_info.get(f"history_{team}", [])):
            flags.append(f"{team} — {p.get('Name', '?')}: {f}")
    for f in weather_flags(wx):
        flags.append(f"WEATHER: {f}")

    return {
        "away":         away_team,
        "home":         home_team,
        "venue":        mlb_info.get("venue", ""),
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
.gd{padding:.7rem .875rem .875rem;display:flex;flex-direction:column;gap:.7rem}
.sec-hd{font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af;margin-bottom:.28rem}
.hb{background:#e5e7eb;color:#374151;font-size:.63rem;font-weight:700;padding:.04rem .26rem;border-radius:3px}
.xr{font-weight:600}
.era-elite{color:#16a34a}.era-good{color:#2563eb}.era-avg{color:#6b7280}.era-below{color:#d97706}.era-poor{color:#dc2626}.era-na{color:#9ca3af}
.wrc-elite{color:#16a34a}.wrc-above{color:#2563eb}.wrc-avg{color:#6b7280}.wrc-below{color:#d97706}.wrc-poor{color:#dc2626}
.dim{color:#9ca3af;font-size:.795rem}
.mu-wrap{margin:.05rem 0 .55rem}
.mu-hdr{display:flex;align-items:center;gap:.35rem;margin-bottom:.22rem;font-size:.84rem}
.mu-sp-nm{font-weight:700}
.mu-sep{color:#9ca3af;font-size:.78rem}
.mu-bat-nm{font-weight:600;color:#6b7280}
.mu-tbl{display:grid;grid-template-columns:5.4rem 1fr 1fr;gap:.1rem .45rem;font-size:.8rem;align-items:baseline}
.mu-col-hd{font-size:.65rem;text-transform:uppercase;letter-spacing:.04em;color:#9ca3af;font-weight:700;text-align:center}
.mu-lbl{color:#9ca3af;font-size:.77rem}
.mu-v{font-weight:600}
.mu-misc{font-size:.74rem;color:#9ca3af;margin-top:.22rem;padding-left:5.85rem}
.bp-row{display:flex;align-items:flex-start;gap:.4rem;font-size:.845rem;padding:.18rem 0}
.tm{font-weight:700;font-size:.77rem;min-width:2.3rem;padding-top:.1rem}
.bp-body{flex:1;min-width:0}
.stats{display:flex;flex-wrap:wrap;gap:.15rem .5rem;font-size:.8rem;color:#6b7280}
.stats b{color:#374151;font-weight:600}
.odds-grid{display:grid;grid-template-columns:2.4rem 1fr 1fr 1fr;gap:.18rem .4rem;font-size:.82rem;align-items:center}
.odds-hd{font-size:.6rem;font-weight:700;color:#9ca3af;text-align:center;text-transform:uppercase;letter-spacing:.04em}
.odds-val{text-align:center;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.odds-sub{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af;margin-top:.45rem;margin-bottom:.1rem}
.flags{list-style:none}
.flags li{font-size:.78rem;color:#92400e;background:#fffbeb;border-left:3px solid #f59e0b;padding:.18rem .45rem;margin-top:.2rem;border-radius:0 4px 4px 0}
@media(prefers-color-scheme:dark){
body{background:#0f0f0f;color:#e5e5e5}
header{background:#030712}
.game{background:#1a1a1a;border-color:#2a2a2a}
.game[open]>summary{border-bottom-color:#2a2a2a}
.gs-venue{color:#6b7280}
.sec-hd{color:#6b7280}
.mu-bat-nm{color:#9ca3af}
.mu-lbl{color:#6b7280}
.mu-col-hd{color:#6b7280}
.mu-misc{color:#6b7280}
.stats b{color:#d1d5db}
.hb{background:#374151;color:#d1d5db}
.flags li{background:#1c1400;border-left-color:#b45309;color:#fbbf24}
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

def _html_game(g: dict) -> str:
    away, home = g["away"], g["home"]
    sp_a, sp_h = g["away_sp"], g["home_sp"]
    of_a, of_h = g["away_off"], g["home_off"]
    bp_a, bp_h = g["away_bp"], g["home_bp"]

    roof = (g["wx"] or {}).get("roof_status", "")
    roof_paren = f" ({roof})" if roof and roof not in ("Open Air", "N/A") else ""
    venue_str = (g["venue"] or "") + roof_paren
    time_str = (g["wx"] or {}).get("game_time_local", "").replace(" ET", "").strip()
    venue_parts = [p for p in [time_str, venue_str] if p.strip()]
    venue_html = (f'<span class="gs-venue">{_h("  ·  ".join(venue_parts))}</span>'
                  if venue_parts else "")

    def _mu_table(sp, bat_team, off, k_line=None):
        """Comparison table: SP stats vs batting lineup stats."""
        def _cell(val_s, cls, lbl):
            if val_s == "?": return '<span class="mu-v dim">?</span>'
            lbl_s = f' <span class="dim">({lbl})</span>' if lbl else ""
            return f'<span class="mu-v {cls}">{_h(val_s)}{lbl_s}</span>'

        ec = _era_cls(sp["label"])
        xera_cell = f'<span class="mu-v {ec}">{_h(sp["xera_s"])}</span>'

        if off:
            wc = _wrc_cls(off["label"])
            wrc_lbl = f' <span class="dim">({_h(off["label"])})</span>' if off["label"] else ""
            wrc_cell  = f'<span class="mu-v {wc}">{_h(off["wrc_s"])}{wrc_lbl}</span>'
            k_bat_v   = flt(off["k"])
            k_bat     = _cell(off["k"],   _k_bat_cls(k_bat_v),  _k_bat_lbl(k_bat_v))
            hh_bat_v  = flt(off["hard"])
            hh_bat    = _cell(off["hard"], _hh_bat_cls(hh_bat_v), _hh_bat_lbl(hh_bat_v))
            col_hd    = _h(off["vs_hand"])
        else:
            wrc_cell = '<span class="mu-v dim">no data</span>'
            k_bat = hh_bat = '<span class="mu-v dim">—</span>'
            col_hd = "lineup"

        k_sp_v  = flt(sp["k"])
        k_sp    = _cell(sp["k"],    _k_sp_cls(k_sp_v),      _k_sp_lbl(k_sp_v))
        hh_sp_v = flt(sp["hard"])
        hh_sp   = _cell(sp["hard"], _hh_sp_cls(hh_sp_v),    _hh_sp_lbl(hh_sp_v))

        barrel_row = ""
        if sp["barrel"] != "?":
            bv = flt(sp["barrel"])
            barrel_row = (
                f'<span class="mu-lbl">Barrel%</span>'
                + _cell(sp["barrel"], _barrel_sp_cls(bv), _barrel_sp_lbl(bv))
                + '<span class="mu-v dim">—</span>'
            )

        misc_parts = []
        if sp["era_s"] != "?":
            misc_parts.append(f'ERA {sp["era_s"]}')
        misc_parts.append(sp["depth"])
        if sp["bb"] != "?":
            misc_parts.append(f'BB% {sp["bb"]}')
        k_s = _fmt_k_line(k_line)
        if k_s:
            misc_parts.append(k_s)
        hand_badge = f'<span class="hb">{_h(sp["hand"])}</span>' if sp["hand"] != "?" else ""
        return (
            f'<div class="mu-wrap">'
            f'<div class="mu-hdr">'
            f'<span class="mu-sp-nm">{_h(sp["name"])} {hand_badge}</span>'
            f'<span class="mu-sep">vs</span>'
            f'<span class="mu-bat-nm">{_h(bat_team)}</span>'
            f'</div>'
            f'<div class="mu-tbl">'
            f'<span></span><span class="mu-col-hd">SP</span><span class="mu-col-hd">{col_hd}</span>'
            f'<span class="mu-lbl">xERA / wRC+</span>{xera_cell}{wrc_cell}'
            f'<span class="mu-lbl">K%</span>{k_sp}{k_bat}'
            f'<span class="mu-lbl">HH%</span>{hh_sp}{hh_bat}'
            f'{barrel_row}'
            f'</div>'
            + (f'<div class="mu-misc">{_h(" · ".join(misc_parts))}</div>' if misc_parts else "")
            + f'</div>'
        )

    def _bp_row(team, bp):
        ec = _era_cls(bp["label"])
        lbl = f' <span class="dim">({_h(bp["label"])})</span>' if bp["label"] else ""
        barrel_s = f'<span><b>Brrl%</b> {_h(bp["barrel"])}</span>' if bp["barrel"] != "?" else ""
        return (f'<div class="bp-row">'
                f'<span class="tm">{_h(team)}</span>'
                f'<div class="bp-body stats">'
                f'<span class="xr {ec}"><b>xERA</b> {_h(bp["xera_s"])}{lbl}</span>'
                f'<span><b>ERA</b> {_h(bp["era_s"])}</span>'
                f'<span><b>K%</b> {_h(bp["k"])}</span>'
                f'<span><b>BB%</b> {_h(bp["bb"])}</span>'
                f'<span><b>HH%</b> {_h(bp["hard"])}</span>'
                f'{barrel_s}'
                f'</div></div>')

    wx = g["wx"]
    wx_html = ""
    if wx:
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
        wx_html = (
            f'<div><div class="sec-hd">Weather</div>'
            + (f'<div class="dim">{_h(", ".join(parts))}</div>' if parts else "")
            + cond_line
            + f'</div>'
        )

    flags_html = ""
    if g["flags"]:
        items = "".join(f'<li>{_h(f)}</li>' for f in g["flags"])
        flags_html = f'<div><div class="sec-hd">Flags</div><ul class="flags">{items}</ul></div>'

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
        odds_html = (
            f'<div><div class="sec-hd">Odds <span class="dim"{_sub}>· best of DK / FanDuel / Fanatics</span></div>'
            f'<div class="odds-sub">Full Game</div>'
            f'<div class="odds-grid">'
            + _odds_rows(od["away_ml"], od["home_ml"],
                         od["away_spread"], od["home_spread"],
                         od["over"], od["under"])
            + f'</div>{f5_html}</div>'
        )

    matchup_html = (
        f'<div><div class="sec-hd">Matchup <span class="dim"{_sub}>· SP last 3 starts / lineup last 12</span></div>'
        + _mu_table(sp_a, home, of_h, od.get("away_k") if od else None)
        + _mu_table(sp_h, away, of_a, od.get("home_k") if od else None)
        + f'</div>'
    )

    return (
        f'\n<details class="game" open id="{_h(away)}-{_h(home)}">'
        f'\n  <summary>'
        f'\n    <div class="gs-matchup"><div class="gs-teams">{_logo_img(away)}{_h(away)} @ {_logo_img(home)}{_h(home)}</div>{venue_html}</div>'
        f'\n  </summary>'
        f'\n  <div class="gd">'
        f'\n    {odds_html}'
        f'\n    {matchup_html}'
        f'\n    <div><div class="sec-hd">Bullpens <span class="dim"{_sub}>· last 12</span></div>'
        f'{_bp_row(away,bp_a)}{_bp_row(home,bp_h)}</div>'
        f'\n    {wx_html}'
        f'\n    {flags_html}'
        f'\n  </div>'
        f'\n</details>'
    )


def _time_sort_key(g: dict) -> int:
    t = (g.get("wx") or {}).get("game_time_local", "")
    import re as _re
    m = _re.match(r'(\d+):(\d+)\s*(AM|PM)', t)
    if not m:
        return 9999
    h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "PM" and h != 12: h += 12
    elif ampm == "AM" and h == 12: h = 0
    return h * 60 + mn


def render_html_page(games: list[dict], target_date: date, generated_at: str,
                     odds_at: str = "") -> str:
    date_long = target_date.strftime(f"%A, %B {target_date.day}, %Y")
    date_short = target_date.strftime(f"%b {target_date.day}")
    games = sorted(games, key=_time_sort_key)
    cards = "".join(_html_game(g) for g in games)
    odds_sub = f" · Odds {_h(odds_at)}" if odds_at else ""
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>MLB Game Overviews · {_h(date_short)}</title>\n'
        f'<style>{_CSS}</style>\n'
        f'</head>\n<body>\n'
        f'<header><h1>MLB Game Overviews</h1>'
        f'<p class="sub">{_h(date_long)} · Updated {_h(generated_at)}{odds_sub}</p></header>\n'
        f'<main>{cards}\n</main>\n</body>\n</html>'
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
    today_d = date.today()
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
    odds_at = load_odds_meta(data_dir, target_date)

    game_data: list[dict] = []
    for p1, p2 in games:
        t1_mlb = to_mlb(p1.get("Team", ""))
        t2_mlb = to_mlb(p2.get("Team", ""))
        key = frozenset([t1_mlb, t2_mlb])

        mlb_info = mlb_schedule.get(key, {})

        if not args.no_mlb and HAS_REQUESTS:
            for p in (p1, p2):
                pid = p.get("mlbam_id")
                team = p.get("Team", "")
                if pid and team:
                    mlb_info[f"history_{team}"] = get_recent_starts(int(pid))

        # Ballpark weather — keyed by raw team codes (same as Handigraphs starters JSON)
        t1_raw = p1.get("Team", "")
        t2_raw = p2.get("Team", "")
        wx = ballpark_wx.get(frozenset([t1_raw, t2_raw]), {})
        # Fallback to Open-Meteo if Handigraphs weather file wasn't downloaded
        if not wx and not args.no_weather and HAS_REQUESTS:
            home_t = mlb_info.get("home", t2_raw)
            wx = get_weather(home_t, target_date)

        if args.html:
            g = analyze_game(p1, p2, rhp, lhp, bp, mlb_info, wx)
            g["odds"] = get_game_odds(odds_data, t1_raw, t2_raw,
                                       g["away_sp"]["name"], g["home_sp"]["name"])
            game_data.append(g)
        else:
            print_game(p1, p2, rhp, lhp, bp, mlb_info, wx)

    if args.html:
        generated_at = datetime.utcnow().strftime("%H:%M UTC")
        print(render_html_page(game_data, target_date, generated_at, odds_at))
    else:
        print()


if __name__ == "__main__":
    main()
