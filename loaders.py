"""Data loaders — read Handigraphs JSON/CSV files from data/ directory."""

import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config
from teams import to_stats

from season import ET as _ET, season_start


# ── File finding ──────────────────────────────────────────────────────────────

def _find_file(data_dir: Path, prefix: str, target_date: date, ext: str) -> Optional[Path]:
    ds = target_date.strftime("%Y-%m-%d")
    matches = list(data_dir.glob(f"{prefix}*{ds}*.{ext}"))
    if not matches:
        return None
    # Prefer files without "meta" in the name, then shortest name
    matches.sort(key=lambda p: ("meta" in p.name, len(p.name)))
    return matches[0]


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


# ── JSON parsers ──────────────────────────────────────────────────────────────

def _load_starters_json(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    rows = raw.get("starters", raw) if isinstance(raw, dict) else raw
    result = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        s = p.get("stats") or {}
        result.append({
            "Name":          p.get("name", ""),
            "Throws":        p.get("throws", ""),
            "Team":          p.get("team", ""),
            "Opponent":      p.get("opponent", ""),
            "mlbam_id":      p.get("mlbam_id") or p.get("id"),
            "lineup_status": p.get("lineup_status", ""),
            "game_number":   p.get("game_number") or 1,
            "today_time":    p.get("today_time", ""),
            "IP":            s.get("ip"),
            "TBF":           s.get("tbf"),
            "ERA":           s.get("era"),
            "xERA":          s.get("xera"),
            "FIP":           s.get("fip"),
            "xFIP":          s.get("xfip"),
            "K-BB%":         s.get("k_bb_pct"),
            "CSW%":          s.get("csw_pct"),
            "K%":            s.get("k_pct"),
            "BB%":           s.get("bb_pct"),
            "SwStr%":        s.get("swstr_pct"),
            "Whiff%":        s.get("whiff_pct"),
            "O-Swing%":      s.get("o_swing_pct"),
            "Zone%":         s.get("zone_pct"),
            "FPS%":          s.get("fps_pct"),
            "Avg EV":        s.get("avg_ev"),
            "Hard-Hit%":     s.get("hard_hit_pct"),
            "Barrel%":       s.get("barrel_pct"),
            "HR/9":          s.get("hr_per_9"),
            "GB%":           s.get("gb_pct"),
            "FB%":           s.get("fb_pct"),
            "LD%":           s.get("ld_pct"),
            "xBA":           s.get("xba"),
            "xSLG":          s.get("xslg"),
            "wOBA":          s.get("woba"),
            "xwOBA":         s.get("xwoba"),
            "BABIP (ag)":    s.get("babip_ag"),
            "ISO (ag)":      s.get("iso_ag"),
            "SLG (ag)":      s.get("slg_ag"),
            "WHIP":          s.get("whip"),
            "LOB%":          s.get("lob_pct"),
            "Outs/GS":       s.get("outs_per_gs"),
            "Pitches/PA":    s.get("pitches_per_pa"),
            "H":             s.get("h") or s.get("hit_cnt"),
            "Games":         s.get("games"),
        })
    return [r for r in result if r.get("Name")]


def _load_team_stats_json(path: Path) -> dict:
    raw = json.loads(path.read_text())
    rows = raw if isinstance(raw, list) else raw.get("data", [])
    result = {}
    for r in rows:
        team = r.get("team", "")
        if not team:
            continue
        entry = {
            "Team":     team,
            "wRC+":     r.get("wrc_plus"),
            "wOBA":     r.get("woba"),
            "BABIP":    r.get("babip"),
            "OPS":      r.get("ops"),
            "ISO":      r.get("iso"),
            "GB/FB":    r.get("gb_fb"),
            "K%":       r.get("k_perc") or r.get("k_pct"),
            "BB%":      r.get("bb_perc") or r.get("bb_pct"),
            "HardHit%": r.get("hard_perc"),
            # Feed gives contact rate, not whiff rate — they're complements.
            "Whiff%":   (100 - r["contact_perc"]) if r.get("contact_perc") is not None else None,
            "FB%":      r.get("fb_perc"),
            "LD%":      r.get("ld_perc"),
            "GB%":      r.get("gb_perc"),
        }
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
            "Team":    team,
            "ERA":     r.get("era"),
            "xERA":    r.get("xera"),
            "FIP":     r.get("fip"),
            "xFIP":    r.get("xfip"),
            "K%":      r.get("k_perc") or r.get("k_pct"),
            "BB%":     r.get("bb_perc") or r.get("bb_pct"),
            "BABIP":   r.get("babip"),
            "wOBA":    r.get("woba"),
            "SwStr%":  r.get("swstr_pct"),
            "CSW%":    r.get("csw_pct"),
            "Hard%":   r.get("hard_hit_pct") or r.get("hard_contact_pct"),
            "Barrel%": r.get("barrel_pct"),
            "GB%":     r.get("gb_pct") or r.get("ground_ball_pct"),
            "FB%":     r.get("fb_pct") or r.get("fly_ball_pct"),
            "LD%":     r.get("ld_pct") or r.get("line_drive_pct"),
            "HR/9":    r.get("hr_per_9") or r.get("hr_per_nine"),
        }
        result[team] = entry
        norm = to_stats(team)
        if norm != team:
            result[norm] = entry
    return result


# ── Public loaders ────────────────────────────────────────────────────────────

def load_starters(data_dir: Path, target_date: date) -> list[dict]:
    """Load starters from JSON (primary) or CSV (fallback)."""
    p = _find_file(data_dir, "starters_last3g", target_date, "json")
    if p:
        return _load_starters_json(p)
    p = _find_file(data_dir, "starters_last3g", target_date, "csv")
    if p:
        return [r for r in _load_csv(p) if r.get("Name", "").strip()]
    return []


def _load_team_split_pair(data_dir: Path, target_date: date, window: str) -> tuple[dict, dict]:
    """Load one team-offense window (e.g. 'L6' or 'L12') as (rhp_stats, lhp_stats)."""
    rj = _find_file(data_dir, f"team_stats_{window}RHP", target_date, "json")
    lj = _find_file(data_dir, f"team_stats_{window}LHP", target_date, "json")
    if rj and lj:
        return _load_team_stats_json(rj), _load_team_stats_json(lj)
    rp = _find_file(data_dir, f"team_stats_{window}RHP", target_date, "csv")
    lp = _find_file(data_dir, f"team_stats_{window}LHP", target_date, "csv")
    if rp and lp:
        return (
            {r["Team"]: r for r in _load_csv(rp)},
            {r["Team"]: r for r in _load_csv(lp)},
        )
    return {}, {}


def load_team_stats(data_dir: Path, target_date: date) -> tuple[dict, dict, dict, dict]:
    """Returns (rhp6, lhp6, rhp12, lhp12) dicts keyed by team code.

    The first pair is the PRIMARY window every offense number on the card comes from;
    the second is the longer window kept purely so wRC+ can be compared across the two.
    Either pair degrades independently to {} — a missing L12 file costs the context
    column, not the card.
    """
    rhp6, lhp6   = _load_team_split_pair(data_dir, target_date, config.TEAM_SPLIT_PRIMARY)
    rhp12, lhp12 = _load_team_split_pair(data_dir, target_date, config.TEAM_SPLIT_CONTEXT)
    if not rhp6 and not lhp6:
        print(
            f"WARNING: Missing {config.TEAM_SPLIT_PRIMARY} team stats data in {data_dir} "
            f"for {target_date} — cards will show no offense stats",
            file=sys.stderr,
        )
    elif not rhp12 and not lhp12:
        print(
            f"WARNING: Missing {config.TEAM_SPLIT_CONTEXT} team stats data in {data_dir} "
            f"for {target_date} — cards will show no comparison wRC+",
            file=sys.stderr,
        )
    return rhp6, lhp6, rhp12, lhp12


def load_bullpen(data_dir: Path, target_date: date) -> dict:
    """Returns bullpen stats dict keyed by team code."""
    p = _find_file(data_dir, "bullpen_stats_last12g", target_date, "json")
    if p:
        return _load_bullpen_json(p)
    p = _find_file(data_dir, "bullpen_stats_last12g", target_date, "csv")
    if p:
        return {r["Team"]: r for r in _load_csv(p)}
    print(
        f"WARNING: No bullpen data in {data_dir} for {target_date} — "
        "cards will show no bullpen stats",
        file=sys.stderr,
    )
    return {}


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
    """Load per-event pitcher props; returns {(away_team, home_team): [event_dict, ...]}.

    Each event_dict keeps commence_time (used to disambiguate doubleheader legs)
    alongside its bookmakers list — see odds.pick_odds_by_time().
    """
    p = _find_file(data_dir, "props", target_date, "json")
    if not p:
        return {}
    try:
        raw = json.loads(p.read_text())
        result: dict = {}
        for event_data in raw.values():
            away = event_data.get("away_team", "")
            home = event_data.get("home_team", "")
            if away and home:
                result.setdefault((away, home), []).append(event_data)
        return result
    except Exception:
        return {}


# How far back the over/under trend lines can possibly look: the widest slice is a
# team's last 10 games, and a team plays ~10 games in two weeks. 45 days is generous
# headroom for off-days and postponements while keeping the read bounded.
_OU_WINDOW_DAYS = 45


def load_history_games(history_dir: Path, target_date: Optional[date] = None,
                       days: int = _OU_WINDOW_DAYS) -> list[dict]:
    """Load graded, de-duplicated games from history/*.json, oldest first.

    Each slate file records every game the Odds API was serving that day, which
    includes games one to three days out. The same game therefore appears in
    several files with a different line each time, and its over_hit flips as the
    line moves — counting raw rows would badly skew a trend record (1052 rows
    covering only ~512 games).

    Two passes fix it: keep only the row whose slate date matches the game's own
    ET date (drops forward-looking snapshots), then keep the latest-recorded row
    per game (the closing line). Doubleheaders collapse to a single game, since
    history rows carry no game number.

    Pass target_date to read only the files that can affect its trend lines. This
    is not just a speed cap — it is a correctness one. history/ is append-only and
    git-tracked, so it grows by a file a day forever (~70KB/day, ~13MB a season).
    Without a floor at the season boundary, an April 2027 page would compute "last
    10 games" over/under records from games played in September 2026, silently
    presenting last year's roster as this year's trend. Filenames are the slate
    date, so the window is applied before any file is opened.
    """
    paths = sorted(history_dir.glob("*.json"))
    if target_date is not None:
        floor = max(target_date - timedelta(days=days), season_start(target_date))
        lo, hi = floor.isoformat(), target_date.isoformat()
        paths = [p for p in paths if lo <= p.stem <= hi]

    rows: list[dict] = []
    for p in paths:
        try:
            parsed = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        rows.extend(
            r for r in parsed
            if isinstance(r, dict) and r.get("over_hit") is not None
        )

    def et_date(r: dict) -> str:
        t = r.get("game_time_utc") or ""
        try:
            return (datetime.fromisoformat(t.replace("Z", "+00:00"))
                    .astimezone(_ET).strftime("%Y-%m-%d"))
        except Exception:
            return ""

    best: dict[tuple, dict] = {}
    for r in rows:
        if et_date(r) != r.get("date"):
            continue
        key = (r.get("away_code"), r.get("home_code"), r.get("date"))
        prev = best.get(key)
        if prev is None or (r.get("odds_recorded_at") or "") > (prev.get("odds_recorded_at") or ""):
            best[key] = r

    games = list(best.values())
    games.sort(key=lambda r: (r.get("date", ""), r.get("game_time_utc", "")))
    return games


def load_ballpark_weather(data_dir: Path, target_date: date) -> dict:
    """Returns dict keyed by (frozenset({away_team, home_team}), game_number) → game weather dict.

    game_number defaults to 1 when the source data doesn't distinguish doubleheader legs.
    """
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
            result[(frozenset([away, home]), g.get("game_number") or 1)] = g
    return result
