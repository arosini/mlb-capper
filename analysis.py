"""Core game analysis — analyze_game(), flags, trends, and helper utilities."""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from teams import to_stats, to_mlb, ODDS_TEAM, MLB_NAME_TO_CODE

_ET = timezone(timedelta(hours=-4))


# ── Numeric helpers ───────────────────────────────────────────────────────────

def flt(val) -> Optional[float]:
    try:
        return float(str(val).rstrip("%"))
    except (TypeError, ValueError):
        return None


def pct_val(s: str) -> Optional[float]:
    return flt(s.rstrip("%")) if s else None


def fp1(val) -> str:
    """Format a percentage/rate to 1 decimal place."""
    v = flt(val)
    return f"{v:.1f}" if v is not None else "?"


def fp3(val) -> str:
    """Format a rate to 3 decimal places."""
    v = flt(val)
    return f"{v:.3f}" if v is not None else "?"


# ── Qualitative labels ────────────────────────────────────────────────────────

def wrc_label(v: Optional[float]) -> str:
    if v is None: return ""
    if v >= 130:  return "elite"
    if v >= 115:  return "above avg"
    if v >= 95:   return "avg"
    if v >= 80:   return "below avg"
    return "poor"


def xera_label(v: Optional[float]) -> str:
    if v is None: return ""
    if v < 3.00:  return "elite"
    if v < 3.75:  return "good"
    if v < 4.50:  return "avg"
    if v < 5.25:  return "below avg"
    return "poor"


# ── Game pairing ──────────────────────────────────────────────────────────────

def build_games(starters: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each starter with their game's opponent starter.

    Rows are paired by (team, game_number) rather than team alone so doubleheaders
    (two rows per team, same opponent, different game_number) don't collide.
    """
    by_team_game = {
        (r["Team"], r.get("game_number") or 1): r for r in starters if r.get("Team")
    }
    seen: set[tuple] = set()
    games = []
    for row in starters:
        team = (row.get("Team") or "").strip()
        opp  = (row.get("Opponent") or "").strip()
        if not team or not opp:
            continue
        gn  = row.get("game_number") or 1
        key = (tuple(sorted([team, opp])), gn)
        if key in seen:
            continue
        seen.add(key)
        opp_row = by_team_game.get((opp, gn), {"Name": "TBD", "Team": opp, "Throws": "?"})
        games.append((row, opp_row))
    return games


def validate_pitchers(p1: dict, p2: dict, mlb_info: dict) -> tuple[dict, dict]:
    """Guard against Handigraphs carrying yesterday's starter in a back-to-back series.

    Compares mlbam_id (Handigraphs) against probablePitcher.id (MLB API).
    If they differ, the row is stale — replace with a TBD placeholder.
    """
    away_mlb   = mlb_info.get("away", "")
    t1_is_away = (to_mlb(p1.get("Team", "")) == away_mlb)
    away_p, home_p = (p1, p2) if t1_is_away else (p2, p1)

    def _tbd(p: dict, probable_name: str) -> dict:
        return {"Name": probable_name or "TBD", "Team": p.get("Team", ""),
                "Opponent": p.get("Opponent", ""), "Throws": "?"}

    away_pid = str(mlb_info.get("away_pid") or "")
    home_pid = str(mlb_info.get("home_pid") or "")
    ap_id    = str(away_p.get("mlbam_id") or "")
    hp_id    = str(home_p.get("mlbam_id") or "")

    if away_pid and ap_id and ap_id != away_pid:
        away_p = _tbd(away_p, mlb_info.get("away_pname", ""))
    if home_pid and hp_id and hp_id != home_pid:
        home_p = _tbd(home_p, mlb_info.get("home_pname", ""))

    return (away_p, home_p) if t1_is_away else (home_p, away_p)


# ── Flag generators ───────────────────────────────────────────────────────────

def pitcher_csv_flags(row: dict) -> list[str]:
    """Generate flags from Handigraphs aggregate stats (last 3 starts)."""
    flags  = []
    status = row.get("lineup_status", "")
    if status and status not in ("confirmed", "expected", ""):
        flags.append(f"lineup: {status}")

    ip   = flt(row.get("IP"))
    xera = flt(row.get("xERA"))

    if ip is None and xera is None:
        flags.append("first start of the season — no stats available yet")
        return flags

    if ip is not None and ip < 9:
        flags.append(f"small sample ({ip:.1f} IP over 3 starts) — stats may not reflect true ability")

    hh     = flt(row.get("Hard-Hit%", ""))
    barrel = flt(row.get("Barrel%", ""))
    bb     = flt(row.get("BB%"))
    ogs    = flt(row.get("Outs/GS"))

    if hh     is not None and hh > 44:
        flags.append(f"HH% {hh:.0f}% — batters are squaring up the ball at an elevated rate")
    if barrel is not None and barrel > 12:
        flags.append(f"Barrel% {barrel:.0f}% — high hard contact rate, elevated home run risk")
    if bb     is not None and bb > 12:
        flags.append(f"BB% {bb:.0f}% — command concerns, elevated walk rate")
    if ogs    is not None and (ogs / 3) < 4.0:
        flags.append(f"avg {ogs/3:.1f} IP/gs — short outings, bullpen likely needed early")

    return flags


def bullpen_flags(row: dict) -> list[str]:
    flags = []
    xera = flt(row.get("xERA"))
    if xera is not None and xera > 5.0:
        flags.append(
            f"bullpen xERA {xera:.2f} — bullpen performing well below average by expected ERA"
        )
    return flags


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


def pitcher_history_flags(
    starts: list[dict],
    hand: str,
    rhp_pool: dict,
    lhp_pool: dict,
    today: date,
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

    today_s        = today.isoformat() if today else ""
    all_appearances = [s for s in starts if _raw_date(s) != today_s]
    start_entries   = [s for s in all_appearances if int(s.get("stat", {}).get("gamesStarted", 0)) > 0]
    recent_3        = start_entries[-3:]

    # Days since last start
    if start_entries:
        last_dt = _parse_date(start_entries[-1])
        if last_dt:
            days = (today - last_dt).days
            if days > 10:
                flags.append(
                    f"{days} days since last start ({_raw_date(start_entries[-1])}) "
                    "— may not be fully stretched out"
                )

    # Recent relief appearances
    relief_dates = []
    for s in all_appearances[-6:]:
        if int(s.get("stat", {}).get("gamesStarted", 0)) == 0:
            relief_dates.append(_raw_date(s))
    if relief_dates:
        flags.append(
            "recent bullpen appearance: "
            + ", ".join(sorted(relief_dates, reverse=True)[:2])
            + " — may affect pitch count or availability"
        )

    # Pitch count on last start
    if start_entries:
        last_stat = start_entries[-1].get("stat", {})
        pc = last_stat.get("numberOfPitches")
        if pc is not None:
            pc = int(pc)
            if pc < 80:
                flags.append(f"last start: {pc} pitches — short outing, possible injury concern or early hook")
            elif pc > 100:
                flags.append(f"last start: {pc} pitches — high pitch count, may be on shorter leash today")

    # One rough outing skewing the 3-game ERA
    if len(recent_3) >= 2:
        outings = []
        for s in recent_3:
            stat = s.get("stat", {})
            oip  = flt(stat.get("inningsPitched"))
            oer  = int(stat.get("earnedRuns") or 0)
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
                flags.append(
                    f"{worst['date']}: {outing_str} skewing 3-game ERA "
                    "— other starts look better, don't overweight the ERA"
                )

    # K outlier in last 3 starts
    k_pairs = []
    for s in recent_3:
        k_val = s.get("stat", {}).get("strikeOuts")
        if k_val is not None:
            k_pairs.append((int(k_val), _raw_date(s)))
    if len(k_pairs) >= 2:
        avg_k = sum(k for k, _ in k_pairs) / len(k_pairs)
        for k, d in k_pairs:
            if k >= max(avg_k * 1.75, 9) and k >= avg_k + 3:
                flags.append(f"high-K outing {d} ({k} Ks vs avg {avg_k:.1f}) — stuff can dominate; may not repeat")
            elif avg_k >= 5 and k <= avg_k * 0.4 and k <= avg_k - 3:
                flags.append(f"low-K outing {d} ({k} Ks vs avg {avg_k:.1f}) — stuff was flat that day")

    # Opponent K-rate context
    opp_pool = lhp_pool if hand == "L" else rhp_pool
    opp_ks: list[tuple[str, float]] = []
    for s in recent_3:
        opp_full = (s.get("opponent") or {}).get("name", "")
        opp_code = MLB_NAME_TO_CODE.get(opp_full, "")
        if not opp_code:
            continue
        row = opp_pool.get(opp_code) or opp_pool.get(to_stats(opp_code), {})
        k   = flt(row.get("K%")) if row else None
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


# ── Situational splits ────────────────────────────────────────────────────────

def _situational_avg(entries: list[dict]) -> Optional[dict]:
    """Average pitching stats (starts only) over a list of game log splits."""
    starts = [
        s for s in entries
        if int(s.get("stat", {}).get("gamesStarted", 0)) > 0
        and flt(s.get("stat", {}).get("inningsPitched")) is not None
    ]
    if not starts:
        return None
    n        = len(starts)
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

    last10 = completed[-10:]
    side10 = [g for g in completed if g["is_home"] == is_home_today][-10:]

    start_pks = {
        s.get("game", {}).get("gamePk")
        for s in pitcher_hist_cur
        if int(s.get("stat", {}).get("gamesStarted", 0)) > 0
        and s.get("game", {}).get("gamePk")
    }
    in_starts  = [g for g in completed if g["game_pk"] in start_pks]
    last5      = in_starts[-5:]
    last5_side = [g for g in in_starts if g["is_home"] == is_home_today][-5:]

    # Win/loss streak
    streak_count = 0
    streak_type: Optional[str] = None
    for g in reversed(completed):
        if streak_type is None:
            streak_type  = "W" if g["won"] else "L"
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


def extract_outings(history: list[dict], n: int = 5) -> list[dict]:
    """Return the n most-recent outings from a game-log list, newest first."""
    result = []
    for s in reversed(history):
        stat = s.get("stat", {})
        if not stat:
            continue
        raw_date = s.get("date") or s.get("game", {}).get("officialDate", "")
        try:
            dt     = datetime.strptime(raw_date[:10], "%Y-%m-%d")
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

        opp_full  = (s.get("opponent") or {}).get("name", "")
        opp_code  = MLB_NAME_TO_CODE.get(opp_full, opp_full[:3].upper() if opp_full else "?")
        is_home   = s.get("isHome")
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


# ── Core analysis ─────────────────────────────────────────────────────────────

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
    t1, t2    = p1.get("Team", "?"), p2.get("Team", "?")
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
        era       = flt(p.get("ERA"))
        has_stats = xera is not None or era is not None or flt(p.get("K%")) is not None
        return {
            "name":      p.get("Name", "TBD"),
            "hand":      hand,
            "has_stats": has_stats,
            "xera":      xera,
            "xera_s":    f"{xera:.2f}" if xera is not None else "?",
            "era":       era,
            "era_s":     f"{era:.2f}" if era is not None else "?",
            "label":     xera_label(xera) if xera is not None else "",
            "kbb":       kbb,
            "kbb_s":     f"{kbb:.1f}%" if kbb is not None else "?",
            "depth":     depth,
            "k":         fp1(p.get("K%")),
            "bb":        fp1(p.get("BB%")),
            "hard":      fp1(p.get("Hard-Hit%")),
            "barrel":    fp1(p.get("Barrel%")),
            "h_per_gs":  (lambda h, g: f"{h/g:.1f}" if h and g else "?")(
                flt(p.get("H")), flt(p.get("Games"))),
        }

    def _off(batting: str, pitcher: dict) -> Optional[dict]:
        hand = (pitcher.get("Throws") or "?")[0]
        if hand not in ("R", "L"):
            return None
        pool = rhp if hand == "R" else lhp
        s    = pool.get(to_stats(batting), {})
        if not s:
            return None
        wrc = flt(s.get("wRC+"))
        return {
            "wrc":     wrc,
            "wrc_s":   f"{wrc:.0f}" if wrc is not None else "N/A",
            "label":   wrc_label(wrc) if wrc is not None else "",
            "woba":    fp3(s.get("wOBA")),
            "k":       fp1(s.get("K%")),
            "hard":    fp1(s.get("HardHit%")),
            "vs_hand": "RHP" if hand == "R" else "LHP",
        }

    def _bp(team: str) -> dict:
        b    = bullpen.get(team, bullpen.get(to_stats(team), {}))
        xera = flt(b.get("xERA"))
        era  = flt(b.get("ERA"))
        stress_key = "away_bp_stress" if team == away_team else "home_bp_stress"
        stress = mlb_info.get(stress_key, {})
        return {
            "xera":         xera,
            "xera_s":       f"{xera:.2f}" if xera is not None else "N/A",
            "era_s":        f"{era:.2f}" if era is not None else "N/A",
            "label":        xera_label(xera) if xera is not None else "",
            "k":            fp1(b.get("K%") or b.get("k_pct") or b.get("k_perc")),
            "bb":           fp1(b.get("BB%") or b.get("bb_pct") or b.get("bb_perc")),
            "hard":         fp1(b.get("Hard%")),
            "barrel":       fp1(b.get("Barrel%")),
            "raw":          b,
            "stress_ip":    stress.get("ip"),
            "stress_games": stress.get("games", 0),
            "stress_label": stress.get("label", ""),
            "stress_css":   stress.get("css", "era-na"),
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
    wrc_a    = away_off["wrc"] if away_off else None
    wrc_h    = home_off["wrc"] if home_off else None
    off_edge = (
        None if wrc_a is None or wrc_h is None or abs(wrc_a - wrc_h) < 10
        else (away_team if wrc_a > wrc_h else home_team)
    )

    away_bp  = _bp(away_team)
    home_bp  = _bp(home_team)
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

    best    = max(tally.values())
    leaders = [t for t, v in tally.items() if v == best]
    if best == 0:
        verdict, verdict_team = "TOSS-UP / no clear edge", None
    elif best == 1:
        verdict, verdict_team = f"Lean {leaders[0]}  (1 of 3)", leaders[0]
    else:
        verdict, verdict_team = f"{leaders[0]}  ({best} of 3 categories)", leaders[0]

    # SP situational splits (2 seasons)
    away_full = ODDS_TEAM.get(away_team, "")
    home_full = ODDS_TEAM.get(home_team, "")
    away_hist = mlb_info.get(f"history_{away_team}", [])
    home_hist = mlb_info.get(f"history_{home_team}", [])
    cur_year  = str(today.year) if today else str(datetime.now(_ET).year)
    away_hist_cur = [s for s in away_hist if s.get("date", "").startswith(cur_year)]
    home_hist_cur = [s for s in home_hist if s.get("date", "").startswith(cur_year)]

    away_sp_splits = {
        "vs": _situational_avg(
            [s for s in away_hist if (s.get("opponent") or {}).get("name", "") == home_full][-3:]
        ),
        "at": _situational_avg(
            [s for s in away_hist
             if s.get("isHome") is False
             and (s.get("opponent") or {}).get("name", "") == home_full][-3:]
        ),
    }
    home_sp_splits = {
        "vs": _situational_avg(
            [s for s in home_hist if (s.get("opponent") or {}).get("name", "") == away_full][-3:]
        ),
        "at": _situational_avg(
            [s for s in home_hist if s.get("isHome") is True][-3:]
        ),
    }

    today_s     = today.isoformat() if today else ""
    away_trends = _team_trends(mlb_info.get("away_record", []), away_hist_cur, False, today_s)
    home_trends = _team_trends(mlb_info.get("home_record", []), home_hist_cur, True,  today_s)

    # H2H record this season
    away_rec = mlb_info.get("away_record", [])
    home_rec = mlb_info.get("home_record", [])
    away_pks = {g["game_pk"] for g in away_rec if g.get("game_pk")}
    home_pks = {g["game_pk"] for g in home_rec if g.get("game_pk")}
    h2h_pks  = away_pks & home_pks
    h2h_games = [g for g in away_rec if g.get("game_pk") in h2h_pks]
    away_h2h_w = sum(1 for g in h2h_games if g["won"])
    h2h = {
        "away_wins": away_h2h_w,
        "home_wins": len(h2h_games) - away_h2h_w,
        "total":     len(h2h_games),
    }

    # Aggregate flags
    flags: list[str] = []
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        name = p.get("Name", "?")
        for f in pitcher_csv_flags(p):
            flags.append(f"{team} — {name}: {f}")
    for team in [away_team, home_team]:
        b = bullpen.get(team, bullpen.get(to_stats(team), {}))
        for f in bullpen_flags(b):
            flags.append(f"{team} bullpen: {f}")
    for team, bp_d in [(away_team, away_bp), (home_team, home_bp)]:
        slabel = bp_d.get("stress_label", "")
        if slabel in ("Elevated", "Stressed"):
            ip    = bp_d.get("stress_ip", 0)
            g_cnt = bp_d.get("stress_games", 0)
            flags.append(
                f"{team} bullpen {slabel.lower()} ({ip:.1f} IP over {g_cnt}g) "
                "— manager likely leaves starter in longer; lean SP K/outs OVER"
            )
        elif slabel == "Fresh":
            ip    = bp_d.get("stress_ip", 0)
            g_cnt = bp_d.get("stress_games", 0)
            flags.append(
                f"{team} bullpen fresh ({ip:.1f} IP over {g_cnt}g) "
                "— manager may hook starter early; lean SP K/outs UNDER"
            )
    hist_cur_map = {away_team: away_hist_cur, home_team: home_hist_cur}
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        hand = (p.get("Throws") or "?")[0]
        for f in pitcher_history_flags(hist_cur_map[team], hand, rhp, lhp, today):
            flags.append(f"{team} — {p.get('Name', '?')}: {f}")
    for f in weather_flags(wx):
        flags.append(f"WEATHER: {f}")

    return {
        "away":          away_team,
        "home":          home_team,
        "venue":         mlb_info.get("venue", ""),
        "game_date":     mlb_info.get("game_date", ""),
        "game_number":   mlb_info.get("game_number") or 1,
        "away_sp":       away_sp,
        "home_sp":       home_sp,
        "pitch_edge":    pitch_edge,
        "away_off":      away_off,
        "home_off":      home_off,
        "off_edge":      off_edge,
        "away_bp":       away_bp,
        "home_bp":       home_bp,
        "bp_edge":       bp_edge,
        "cat_edges":     cat_edges,
        "verdict":       verdict,
        "verdict_team":  verdict_team,
        "verdict_count": best,
        "wx":            wx,
        "flags":         flags,
        "away_sp_outings":  extract_outings(away_hist_cur),
        "home_sp_outings":  extract_outings(home_hist_cur),
        "away_sp_splits":   away_sp_splits,
        "home_sp_splits":   home_sp_splits,
        "away_trends":      away_trends,
        "home_trends":      home_trends,
        "h2h":              h2h,
    }
