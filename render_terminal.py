"""Terminal renderer — print_game() and ANSI color helpers."""

import sys
from analysis import analyze_game

# Set to False via --no-color flag before calling print_game()
use_color = True


class C:
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
    CYAN   = "\033[36m"
    YELLOW = "\033[33m"
    DIM    = "\033[2m"


def bold(s):   return f"{C.BOLD}{s}{C.RESET}"   if use_color else s
def cyan(s):   return f"{C.CYAN}{s}{C.RESET}"   if use_color else s
def yellow(s): return f"{C.YELLOW}{s}{C.RESET}" if use_color else s
def dim(s):    return f"{C.DIM}{s}{C.RESET}"    if use_color else s


def print_game(
    p1: dict, p2: dict,
    rhp: dict, lhp: dict,
    bullpen: dict,
    mlb_info: dict,
    wx: dict,
) -> None:
    g       = analyze_game(p1, p2, rhp, lhp, bullpen, mlb_info, wx)
    away    = g["away"]
    home    = g["home"]
    away_sp = g["away_sp"]
    home_sp = g["home_sp"]
    away_off = g["away_off"]
    home_off = g["home_off"]
    away_bp  = g["away_bp"]
    home_bp  = g["home_bp"]
    venue    = g["venue"]
    W = 64

    title = f"{away} @ {home}" if mlb_info.get("home") else f"{away} vs {home}"
    print()
    print(bold("═" * W))
    print(bold(f" {title}" + (f"  ·  {venue}" if venue else "")))
    print(bold("═" * W))

    def _sp_line(team, sp):
        lbl = f"({sp['label']:<10})" if sp["label"] else ""
        return (
            f"  {team:<5} {sp['name']} ({sp['hand']}HP)   "
            f"xERA {sp['xera_s']}  {lbl:<12}  K-BB% {sp['kbb_s']}  {sp['depth']}"
        )

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
        return (
            f"  {team:<5} vs {off['vs_hand']}: wRC+ {off['wrc_s']} {lbl:<12}  "
            f"wOBA {off['woba']}  K% {off['k']}  Hard% {off['hard']}"
        )

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
        stress_s = ""
        if bp.get("stress_label") and bp["stress_label"] != "No recent games":
            ip    = bp.get("stress_ip", 0)
            games = bp.get("stress_games", 0)
            stress_s = f"  2d: {bp['stress_label']} ({ip:.1f} IP/{games}g)"
        return (
            f"  {team:<5} xERA {bp['xera_s']} {lbl:<12}  ERA {bp['era_s']}  "
            f"K% {bp['k']}  BB% {bp['bb']}  Hard% {bp['hard']}{stress_s}"
        )

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
        w     = g["wx"]
        venue = w.get("venue_name") or w.get("city", "?")
        roof  = w.get("roof_status", "")
        roof_s = f" ({roof})" if roof and roof not in ("Open Air", "N/A") else ""
        time_s = f"  ·  {w['game_time_local']}" if w.get("game_time_local") else ""
        print(f"  {venue}{roof_s}{time_s}")
        parts = []
        if w.get("temperature") is not None:
            parts.append(f"{w['temperature']:.0f}°F")
        if w.get("weather_description"):
            parts.append(w["weather_description"])
        if w.get("wind_speed") is not None:
            wd = w.get("wind_direction_label", "")
            parts.append(f"Wind {w['wind_speed']:.0f} mph {wd}".strip())
        if w.get("precip_probability") is not None:
            parts.append(f"Rain {w['precip_probability']:.0f}%")
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
