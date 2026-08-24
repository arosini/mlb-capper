"""Terminal renderer — print_game() and ANSI color helpers."""


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
    rhp_ctx: dict | None = None,
    lhp_ctx: dict | None = None,
    all_pool: dict | None = None,
    all_ctx: dict | None = None,
) -> None:
    g       = analyze_game(p1, p2, rhp, lhp, bullpen, mlb_info, wx,
                           rhp_ctx=rhp_ctx, lhp_ctx=lhp_ctx,
                           all_pool=all_pool, all_ctx=all_ctx)
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
        op   = sp.get("opener") or {}
        role = ""
        if sp.get("mode") == "opener":
            role = f"  [BULK — opener {op.get('name', '?')} ({op.get('hand', '?')}HP)]"
        elif sp.get("mode") == "bullpen":
            role = "  [BULLPEN GAME]"
        return (
            f"  {team:<5} {sp['name']} ({sp['hand']}HP)   "
            f"xERA {sp['xera_s']}  {lbl:<12}  K-BB% {sp['kbb_s']}  {sp['depth']}{role}"
        )

    def _print_edge(category, stat, edge, away_v, home_v, prec, gap_unit=""):
        """The '→ Pitching edge: NYY (xERA 3.10 vs 3.94)' line under a section.

        The edge-holder's number is printed first whichever club it belongs to, so the
        comparison always reads in the direction of the edge being claimed. Nothing is
        printed at all when either side has no number to compare.
        """
        if away_v is None or home_v is None:
            return
        if not edge:
            print(f"  → {category}: EVEN  (gap {abs(away_v - home_v):.{prec}f}{gap_unit})")
            return
        win_v, lose_v = (away_v, home_v) if edge == away else (home_v, away_v)
        print(f"  → {category} edge: {edge}  ({stat} {win_v:.{prec}f} vs {lose_v:.{prec}f})")

    print(cyan("\nSTARTERS"))
    print(_sp_line(away, away_sp))
    print(_sp_line(home, home_sp))
    _print_edge("Pitching", "xERA", g["pitch_edge"], away_sp["xera"], home_sp["xera"], 2)

    def _off_line(team, off):
        if off is None:
            return f"  {team:<5} vs ???: no data"
        lbl = f"({off['label']:<10})" if off["label"] else ""
        return (
            f"  {team:<5} {off.get('hand_lbl') or 'vs ' + off['vs_hand']}: "
            f"wRC+ L6 {off['wrc_s']} {lbl:<12}  "
            f"wRC+ L12 {off.get('wrc_ctx_s', 'N/A')}  "
            f"wOBA {off['woba']}  K% {off['k']}  Hard% {off['hard']}"
        )

    print(cyan("\nOFFENSE vs STARTER HAND  (last 6g; L12 wRC+ for comparison)"))
    print(_off_line(away, away_off))
    print(_off_line(home, home_off))
    _print_edge("Offense", "wRC+", g["off_edge"],
                away_off["wrc"] if away_off else None,
                home_off["wrc"] if home_off else None, 0, " wRC+")

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
    _print_edge("Bullpen", "xERA", g["bp_edge"], away_bp["xera"], home_bp["xera"], 2)

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
