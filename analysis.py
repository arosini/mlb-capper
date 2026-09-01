"""Core game analysis — analyze_game(), flags, trends, and helper utilities."""

from datetime import date, datetime
from typing import Optional

from teams import (
    to_stats, to_mlb, ODDS_TEAM, MLB_NAME_TO_CODE, division, normalize_name,
)

from season import ET as _ET


# ── Numeric helpers ───────────────────────────────────────────────────────────

def flt(val) -> Optional[float]:
    try:
        return float(str(val).rstrip("%"))
    except (TypeError, ValueError):
        return None


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


# ── Pitcher roles: openers, bulk arms, and bullpen games ──────────────────────
#
# MLB's probablePitcher and Handigraphs' starters feed disagree for two very different
# reasons, and the old guard collapsed both into "Handigraphs is stale":
#
#   * an OPENER — MLB lists the reliever who throws the first inning or two, while
#     Handigraphs lists the arm expected to carry the bulk of the game. Handigraphs is
#     the one worth believing here: the bulk arm's rate stats describe most of the game,
#     and it is HIS hand the opposing lineup mostly bats against.
#   * a genuinely STALE row — the same two clubs played yesterday and Handigraphs is
#     still holding yesterday's starter.
#
# Blanking the row on both meant an opener game lost the pitcher's hand entirely, and a
# pitcher with no hand means no platoon split, which is why the opposing club's offense
# card rendered empty. Recent workload from the MLB game log tells the two apart.

# At or under this many innings, an outing is not a conventional start. Two is the
# classic opener script; 2.5 leaves room for the opener who gets an extra out or two.
OPENER_IP = 2.5
# At or above this, an arm is carrying real innings — the bulk pitcher, whose last-3
# rate stats describe most of today's game.
BULK_IP = 3.0
# A start this long rules a pitcher out of starting again for several days.
REAL_START_IP = 4.0

_ROLE_LOOKBACK = 5   # appearances considered "recent usage"
_ROLE_MIN_APPS = 2   # below this there is no pattern to read, only noise
_REST_DAYS = 3       # a real start this recent means he cannot be starting today


def outing_ip(raw) -> Optional[float]:
    """Innings from a game log. MLB spells 5⅔ as '5.2' — a base-3 fraction that only
    looks decimal, so reading it as 5.2 quietly understates every partial inning."""
    v = flt(raw)
    if v is None:
        return None
    whole  = int(v)
    thirds = round((v - whole) * 10)
    return whole + (thirds / 3.0 if thirds in (1, 2) else 0.0)


def pitcher_workload(history: list[dict], before: Optional[date] = None,
                     n: int = _ROLE_LOOKBACK) -> dict:
    """Recent-usage profile from an MLB game log.

    `before` drops appearances on or after the target date. On a re-run of a day whose
    games have already been played the log contains today's outing, and letting an
    opener's completed 1.0 IP count toward his own profile is circular.
    """
    apps = []
    for sp in history:
        d = (sp.get("date") or "")[:10]
        if before and d and d >= before.isoformat():
            continue
        ip = outing_ip((sp.get("stat") or {}).get("inningsPitched"))
        if ip is None:
            continue
        apps.append((d, ip, int((sp.get("stat") or {}).get("gamesStarted") or 0)))
    apps.sort(key=lambda a: a[0])
    recent = apps[-n:]
    ips = [ip for _, ip, _ in recent]
    # The most recent REAL start, at full log depth rather than the recent window —
    # the rest check needs to see it however long ago the window ends.
    last_real_start = ""
    for d, ip, gs in apps:
        if gs and ip >= REAL_START_IP:
            last_real_start = d
    return {
        "apps":            len(recent),
        "avg_ip":          (sum(ips) / len(ips)) if ips else None,
        "max_ip":          max(ips) if ips else None,
        "last_real_start": last_real_start,
    }


def is_short_arm(w: dict) -> bool:
    """Recent usage says this pitcher does not go deep — an opener or a reliever."""
    return bool(
        w.get("apps", 0) >= _ROLE_MIN_APPS
        and w.get("avg_ip") is not None
        and w["avg_ip"] <= OPENER_IP
        and (w.get("max_ip") or 0) < REAL_START_IP
    )


def is_bulk_arm(w: dict) -> bool:
    """This pitcher has recently carried bulk innings, started or not."""
    return bool(w.get("apps", 0) >= 1 and (w.get("max_ip") or 0) >= BULK_IP)


def pitched_too_recently(w: dict, today: Optional[date]) -> bool:
    """A real start inside the rest window means he cannot be today's starter.

    This is the stale-row test the old id comparison was standing in for, and it is
    the direct one: it asks whether the Handigraphs pitcher is physically available
    rather than inferring it from two sources disagreeing.
    """
    last = w.get("last_real_start")
    if not last or not today:
        return False
    try:
        return (today - date.fromisoformat(last)).days < _REST_DAYS
    except ValueError:
        return False


def _hand_of(row: dict) -> str:
    return (row.get("Throws") or "?")[0].upper()


def has_sp_stats(row: dict) -> bool:
    """Does this Handigraphs row carry rate stats, or only a name and a hand?"""
    return any(flt(row.get(k)) is not None for k in ("xERA", "ERA", "K%"))


def _blank_row(row: dict, name: str, hand: str, pid="") -> dict:
    """A pitcher we can name but have no Handigraphs rate stats for."""
    return {
        "Name":     name or "TBD",
        "Team":     row.get("Team", ""),
        "Opponent": row.get("Opponent", ""),
        "Throws":   hand or "?",
        "mlbam_id": pid or "",
        "game_number": row.get("game_number") or 1,
    }


def _why_short(w: dict) -> str:
    if not w.get("apps"):
        return "no recent MLB appearances on file"
    return (f"last {w['apps']} outings avg {w['avg_ip']:.1f} IP "
            f"(longest {w['max_ip']:.1f})")


def _resolve_side(hg: dict, pid, pname: str, logs: dict, hands: dict,
                  today: Optional[date]) -> dict:
    """Decide who to show for one club, and under what role.

    Returns the row to display, stamped with `Mode` ("starter" | "opener" | "bullpen"),
    `Opener` (the arm MLB expects first, when that isn't the pitcher being shown), and
    `RoleWhy` (the evidence, for the flags section).
    """
    pid_s  = str(pid or "")
    hg_id  = str(hg.get("mlbam_id") or "")
    mlb_hand = hands.get(pid_s, "")

    def _stamp(row, mode, opener=None, why=""):
        row = dict(row)
        row["Mode"]    = mode
        row["Opener"]  = opener
        row["RoleWhy"] = why
        # `mlb_hand` belongs to MLB's probable, so it can only fill in for a row that IS
        # him. On an opener game the row being returned is the BULK arm — a different
        # pitcher — and stamping the opener's hand on him would split the opposing
        # lineup against the wrong side.
        if (not _hand_of(row).strip("?") and mlb_hand
                and str(row.get("mlbam_id") or "") == pid_s):
            row["Throws"] = mlb_hand
        return row

    # No MLB probable to compare against — nothing to resolve.
    if not pid_s:
        return _stamp(hg, "starter")

    # Handigraphs sends no id for a pitcher it has no recent data on. Matching on name
    # recovers the id (so the game log can be fetched) while keeping the row's `throws`,
    # which is the one field such a row does carry.
    if not hg_id and normalize_name(hg.get("Name", "")) == normalize_name(pname):
        hg = {**hg, "mlbam_id": pid_s}
        hg_id = pid_s

    w_prob = pitcher_workload(logs.get(pid_s, []), before=today)

    if hg_id == pid_s:
        # Both sources agree. A club whose only listed arm does not go deep is running a
        # bullpen game regardless of who is credited with the start.
        if is_short_arm(w_prob):
            return _stamp(hg, "bullpen", why=_why_short(w_prob))
        return _stamp(hg, "starter")

    # The two sources disagree.
    w_hg = pitcher_workload(logs.get(hg_id, []), before=today)
    opener = {"name": pname, "hand": mlb_hand or "?", "id": pid_s}

    if pitched_too_recently(w_hg, today):
        # Handigraphs is holding a starter who threw too recently to go again — the
        # back-to-back rematch case. MLB's probable is the live one.
        row = _blank_row(hg, pname, mlb_hand, pid_s)
        mode = "bullpen" if is_short_arm(w_prob) else "starter"
        return _stamp(row, mode, why=_why_short(w_prob) if mode == "bullpen" else "")

    if is_short_arm(w_prob):
        if is_bulk_arm(w_hg):
            # An opener in front of a bulk arm. Show the bulk arm — his stats and his
            # hand describe the innings that decide the game.
            return _stamp(hg, "opener", opener=opener, why=_why_short(w_prob))
        # Nobody on this side profiles as carrying a game. Handigraphs' pick is worth
        # showing only if it comes with stats; a bare name from a feed that has no data
        # on him is less use than the arm MLB actually has taking the ball first.
        if has_sp_stats(hg):
            return _stamp(hg, "bullpen", opener=opener, why=_why_short(w_prob))
        row = _blank_row(hg, pname, mlb_hand, pid_s)
        return _stamp(row, "bullpen", why=_why_short(w_prob))

    # MLB's probable profiles as a real starter (or has too short a log to say) and
    # Handigraphs names someone else. Believe MLB — it is the more current source, and
    # nothing about the usage pattern says this is an opener script.
    return _stamp(_blank_row(hg, pname, mlb_hand, pid_s), "starter")


def resolve_pitchers(p1: dict, p2: dict, mlb_info: dict,
                     logs: Optional[dict] = None, hands: Optional[dict] = None,
                     today: Optional[date] = None) -> tuple[dict, dict]:
    """Reconcile the Handigraphs starters feed against MLB's probables, per club.

    `logs` maps an MLB player id (as a string) to that pitcher's game-log splits, and
    `hands` maps the same ids to 'L'/'R'. Both are gathered by the caller — this module
    never makes network calls — and both are optional: with neither, every side resolves
    to plain "starter" and the behaviour is the pre-role one.
    """
    logs  = logs or {}
    hands = hands or {}
    away_mlb   = mlb_info.get("away", "")
    t1_is_away = (to_mlb(p1.get("Team", "")) == away_mlb)
    away_p, home_p = (p1, p2) if t1_is_away else (p2, p1)

    away_p = _resolve_side(away_p, mlb_info.get("away_pid"),
                           mlb_info.get("away_pname", ""), logs, hands, today)
    home_p = _resolve_side(home_p, mlb_info.get("home_pid"),
                           mlb_info.get("home_pname", ""), logs, hands, today)

    return (away_p, home_p) if t1_is_away else (home_p, away_p)


def pitcher_ids_to_check(p1: dict, p2: dict, mlb_info: dict) -> set:
    """Every MLB player id resolve_pitchers() may need a game log for.

    Kept here, beside the resolver, so the caller doing the fetching never has to
    re-derive which ids matter.
    """
    ids = set()
    for row in (p1, p2):
        if row.get("mlbam_id"):
            ids.add(str(row["mlbam_id"]))
    for key in ("away_pid", "home_pid"):
        if mlb_info.get(key):
            ids.add(str(mlb_info[key]))
    return ids


# ── Flag generators ───────────────────────────────────────────────────────────

def _pct_tier(v: Optional[float], cuts: tuple) -> Optional[int]:
    """Bucket a percentage into 5 tiers (0-4) using 4 ascending cut points.

    Tier only encodes "how far along the scale" — whether a high tier is good or bad
    depends on the caller's cut points, not on this function. Team K%/Whiff% cuts run
    low-to-high with a HIGH tier meaning worse for the offense; pitcher K%/Whiff% cuts
    are the mirror image, with a HIGH tier meaning better stuff. The team cuts mirror
    render_html.py's _k_bat_cls/_whiff_bat_cls boundaries; the pitcher cuts mirror
    _k_sp_cls/_whiff_sp_cls — kept in sync by hand since analysis.py cannot import
    render_html.py (wrong direction per the module order in CLAUDE.md).
    """
    if v is None:
        return None
    c0, c1, c2, c3 = cuts
    if v < c0: return 0
    if v < c1: return 1
    if v < c2: return 2
    if v < c3: return 3
    return 4


def team_whiff_k_flag(off: Optional[dict]) -> Optional[str]:
    """L6 K% vs Whiff% disagreement for a lineup — last-6 only, deliberately ignoring
    the last-12 window (that one is context, not the number a live alert should react
    to). Team K% and Whiff% are both "higher is worse for the offense" rates on the
    same card, so their tiers are directly comparable. A 2+ tier gap either way is the
    alert; see suggestions.py Section 1 (WHIFF% VS K%) for how the prompt uses it.
    """
    if not off:
        return None
    k     = flt(off.get("k"))
    whiff = flt(off.get("whiff"))
    if k is None or whiff is None:
        return None
    k_tier     = _pct_tier(k,     (16, 20, 24, 28))
    whiff_tier = _pct_tier(whiff, (18, 21, 25, 28))
    gap = k_tier - whiff_tier
    if gap >= 2:
        return f"K% {k:.1f} (last 6) runs well above what Whiff% {whiff:.1f} supports"
    if gap <= -2:
        return f"Whiff% {whiff:.1f} (last 6) runs well above what K% {k:.1f} shows"
    return None


def pitcher_whiff_k_flag(sp: dict) -> Optional[str]:
    """Last-3-start K% vs Whiff% disagreement for a starter. Both are "higher is
    better" rates for a pitcher, so a wide tier gap flags a K rate his swing-and-miss
    stuff does not support in either direction. A 2+ tier gap either way is the alert.
    """
    k     = flt(sp.get("k"))
    whiff = flt(sp.get("whiff"))
    if k is None or whiff is None:
        return None
    k_tier     = _pct_tier(k,     (12, 17, 23, 28))
    whiff_tier = _pct_tier(whiff, (12, 17, 23, 28))
    gap = k_tier - whiff_tier
    if gap >= 2:
        return (f"K% {k:.1f} (last 3 starts) runs well above what Whiff% "
                f"{whiff:.1f} supports")
    if gap <= -2:
        return (f"Whiff% {whiff:.1f} (last 3 starts) runs well above what K% "
                f"{k:.1f} shows")
    return None


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
        flags.append(f"small sample: {ip:.1f} IP over 3 starts")

    ogs    = flt(row.get("Outs/GS"))

    if ogs    is not None and (ogs / 3) < 4.0:
        flags.append(f"avg {ogs/3:.1f} IP/gs over his last 3")

    return flags


def bullpen_flags(row: dict) -> list[str]:
    flags = []
    xera = flt(row.get("xERA"))
    if xera is not None and xera > 5.0:
        flags.append(f"bullpen xERA {xera:.2f} over its last 12")
    return flags


def team_situational_flags(
    team_record: list[dict],
    opp_abbr_today: str,
    series_game_number: Optional[int],
    games_in_series: Optional[int],
    today_s: str,
) -> list[str]:
    """Bounce-back / letdown flags derived from a team's completed game log.

    `team_record` entries come from mlb_api.get_team_schedule() (oldest-first, one dict
    per completed game with won/runs_scored/runs_allowed/opp_abbr). `opp_abbr_today` is
    today's opponent in MLB-API abbreviation form (i.e. to_mlb() of the other club), used
    to tell whether an in-progress series streak is against the team on today's card.
    """
    flags: list[str] = []
    completed = [g for g in team_record if g["date"] != today_s]
    if not completed:
        return flags

    # These state the result and stop. "due for some offensive regression" put
    # gambler's-fallacy phrasing in the data layer, and worse, double-counted: a
    # lineup's pull back toward its true level is exactly what the last-6/last-12 pair
    # on the offense line already measures. §11 assigns the weight.
    last = completed[-1]
    if not last["won"] and last["runs_scored"] == 0:
        if last["runs_allowed"] == 1:
            flags.append(f"lost the last game 1-0 ({last['date']})")
        else:
            flags.append(f"was shut out {last['runs_allowed']}-0 last time out ({last['date']})")

    # Trailing run of consecutive completed games against one opponent, ending at `last`.
    # This is the current (or just-finished) series, series length not being a field the
    # schedule reliably reports for completed games.
    block: list[dict] = []
    for g in reversed(completed):
        if block and g.get("opp_abbr") != block[-1].get("opp_abbr"):
            break
        block.append(g)
    block.reverse()
    opp = block[0].get("opp_abbr") if block else ""
    all_losses = bool(block) and all(not g["won"] for g in block)

    # Is the trailing block the series IN PROGRESS, or one that finished? The schedule
    # numbers a series' games, so game N means N-1 of it are already played. Getting this
    # wrong is not a missing flag but a false one: these branches used to be ordered with
    # the general "just got swept" case FIRST, which made the in-progress case unreachable
    # for any team that had lost two or more — exactly the case it was written for — and
    # put "just got swept by TOR (2 games)" on the card of a club sitting 0-2 in a set
    # still being played. Past tense, and it says the series is over.
    in_current_series = bool(
        opp and opp == opp_abbr_today
        and series_game_number and len(block) == series_game_number - 1
    )

    if opp and all_losses:
        n = len(block)
        dropped = (f"lost the opener of this series vs {opp}" if n == 1
                   else f"has lost all {n} games of this series vs {opp}")
        if in_current_series:
            # State the fact and stop. No "must-win", no "extra motivation to avoid the
            # sweep", no lean toward the moneyline — those are the tails the 2026-08-24
            # adversarial review stripped out of this very flag. §11 assigns the weight.
            if games_in_series and series_game_number == games_in_series:
                flags.append(f"{dropped}; today is the finale, so a loss completes the sweep")
            else:
                left = (games_in_series - series_game_number + 1
                        if games_in_series and series_game_number else None)
                flags.append(f"{dropped}, with {left} left to play" if left
                             else f"{dropped} so far")
        elif opp == opp_abbr_today and not series_game_number:
            # Series numbers missing. The streak is against today's club, so this may well
            # be a set in progress — do not call it finished.
            flags.append(f"has lost the last {n} straight to {opp}, today's opponent")
        elif n >= 2:
            flags.append(f"just got swept by {opp} ({n} games)")

    return flags


def weather_flags(wx: dict, neutral_site: bool = False) -> list[str]:
    """Generate flags from Handigraphs ballpark-weather data.

    neutral_site suppresses the park-factor flag: APF is keyed to the home club's
    usual park, so at Field of Dreams or Mexico City it describes a stadium the game
    is not being played in. A park factor for the wrong park is worse than none —
    the prompt uses APF as a tiebreaker on totals.
    """
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

    # Handigraphs reports the actual state ("Roof Closed"), but the Open-Meteo fallback
    # only knows the park HAS a retractable roof — which is the normal case on the
    # tomorrow page. Say so rather than presenting the forecast as settled conditions.
    roof_caveat = (" (retractable roof — conditions apply only if open)"
                   if roof.strip().lower() == "retractable" else "")

    precip_risk = wx.get("precip_risk_during_game", False)
    precip_prob = wx.get("precip_probability")
    wind_lbl    = wx.get("wind_effect_label", "")
    wind_speed  = wx.get("wind_speed")
    apf         = wx.get("adjusted_park_factor")

    if precip_risk:
        prob_s = f" {precip_prob:.0f}%" if precip_prob is not None else ""
        flags.append(f"rain risk{prob_s}{roof_caveat} — game delay or conditions may affect performance")
    elif precip_prob is not None and precip_prob >= 30:
        flags.append(f"rain chance {precip_prob:.0f}%{roof_caveat}")

    if wind_lbl and wind_lbl not in ("Calm", "Indoor", ""):
        speed_s = f" {wind_speed:.0f} mph" if wind_speed is not None else ""
        dir_s   = wx.get("wind_direction_label") or ""
        from_s  = f" from the {dir_s}" if dir_s else ""
        # Direction is now real (measured against the park's azimuth). The flag states
        # the direction and stops there: a card line that names a side ("supports the
        # over") is a betting verdict sitting in the data layer, and it outweighs the
        # prompt's own weighting rules because it is specific to this game. §9 decides
        # what a wind reading is worth; this only reports it. The previous code labelled
        # any wind over 15 mph as "Out" without consulting direction at all.
        phrase = {
            "Out":   "blowing OUT",
            "In":    "blowing IN",
            "Cross": "blowing ACROSS the field",
        }.get(wind_lbl, wind_lbl)
        flags.append(f"wind {phrase}{speed_s}{from_s}{roof_caveat}")

    if apf is not None and not neutral_site:
        # No "favor the over" tail. §9 of the prompt says in as many words that an
        # ordinary hitter-friendly/pitcher-friendly label "is not a reason for
        # anything" — a card line telling the model to favor a side contradicted the
        # rule it was supposed to be governed by, and the card wins that argument.
        if apf >= 108:
            flags.append(f"hitter-friendly park (APF {apf:.0f})")
        elif apf <= 92:
            flags.append(f"pitcher-friendly park (APF {apf:.0f})")

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
                    f"{days} days since last start ({_raw_date(start_entries[-1])})"
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
        )

    # Pitch count on last start
    if start_entries:
        last_stat = start_entries[-1].get("stat", {})
        pc = last_stat.get("numberOfPitches")
        if pc is not None:
            pc = int(pc)
            # Reported at both ends without a reading attached. "high pitch count, may be
            # on shorter leash today" and "possible injury concern or early hook" were
            # both conclusions, and §8 is where a pitch count gets turned into one.
            if pc < 80 or pc > 100:
                ip = flt(last_stat.get("inningsPitched")) or 0
                flags.append(f"last start: {pc} pitches over {ip:.1f} IP")

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
                    f"{worst['date']}: {outing_str}; the other "
                    f"{len(others)} start(s) in the 3-game window average "
                    f"{avg_other:.2f} ERA equivalent"
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
                flags.append(f"high-K outing {d} ({k} Ks vs avg {avg_k:.1f} over his last 3)")
            elif avg_k >= 5 and k <= avg_k * 0.4 and k <= avg_k - 3:
                flags.append(f"low-K outing {d} ({k} Ks vs avg {avg_k:.1f} over his last 3)")

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
            flags.append(f"recent opponents high-K: {detail}")
        elif len(low_k) >= 2:
            detail = ", ".join(f"{t} {k:.0f}%" for t, k in low_k)
            flags.append(f"recent opponents low-K: {detail}")

    return flags


# ── Situational splits ────────────────────────────────────────────────────────

def _qualifying_starts(entries: list[dict]) -> list[dict]:
    """Game-log entries that count as a start with usable innings, in input order."""
    return [
        s for s in entries
        if int(s.get("stat", {}).get("gamesStarted", 0)) > 0
        and flt(s.get("stat", {}).get("inningsPitched")) is not None
    ]


def _merge_outings(*lists: list[dict]) -> list[dict]:
    """Union of situational outing lists — one row per game, newest first.

    The vs-opponent and at-park sets overlap heavily (an away starter's meetings at this
    park are by definition meetings with this club), so they are merged on the game date
    rather than rendered as two tables that repeat each other.
    """
    seen: set = set()
    merged: list[dict] = []
    for o in sorted((o for lst in lists for o in lst),
                    key=lambda o: o.get("date_iso") or "", reverse=True):
        key = o.get("date_iso") or o.get("date")
        if key in seen:
            continue
        seen.add(key)
        merged.append(o)
    return merged


# A lineup that has faced this arm inside two weeks has current looks at his stuff and
# his sequencing. That is the one part of head-to-head that points somewhere on its own;
# _AI_SYSTEM_PROMPT section 2 says which way, this file only measures it.
FAMILIAR_DAYS = 14


def meeting_recency(outings: list[dict], today: Optional[date] = None) -> dict:
    """Season mix and recency for a starter's meetings with today's opponent.

    Marks each outing with `prior_season` and `days_ago` in place and returns the
    summary the cards print. The game log reaches back two seasons, so a meetings block
    can mix them — and a start against this club last September describes a roster, an
    arsenal and a coaching staff that have all moved since. The card states the split;
    the prompt decides what it is worth.
    """
    ref       = today or datetime.now(_ET).date()
    this_year = prior = 0
    last_days = None
    for o in outings:
        yr = o.get("year")
        o["prior_season"] = bool(yr and yr < ref.year)
        if yr == ref.year:
            this_year += 1
        elif yr:
            prior += 1
        try:
            days = (ref - datetime.strptime(o["date_iso"], "%Y-%m-%d").date()).days
        except Exception:
            days = None
        o["days_ago"] = days
        if days is not None and days >= 0 and (last_days is None or days < last_days):
            last_days = days
    return {
        "n":          len(outings),
        "this_year":  this_year,
        "prior_year": prior,
        "last_days":  last_days,
        "familiar":   last_days is not None and last_days <= FAMILIAR_DAYS,
    }


def _situational_split(entries: list[dict], n: int = 3) -> tuple[Optional[dict], list[dict]]:
    """The averaged line for a situational split, plus the outings behind it.

    Both halves come off the same n-game slice, so the "(n)" printed on the average is
    always the number of outing rows rendered under it. Returns (avg, outings) with the
    outings newest-first, matching extract_outings().
    """
    starts = _qualifying_starts(entries[-n:])
    return _situational_avg(starts), extract_outings(starts, n)


def _situational_avg(entries: list[dict]) -> Optional[dict]:
    """Average pitching stats (starts only) over a list of game log splits."""
    starts = _qualifying_starts(entries)
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


# Below these sample sizes a record is noise rather than a trend, so the line
# is dropped instead of shown.
_OU_MIN_TEAM    = 3
_OU_MIN_PITCHER = 2


def ou_trends(
    hist_games: list[dict],
    team: str,
    pitcher_id: str,
    is_home_today: bool,
    today_s: str,
) -> Optional[dict]:
    """Over/under records for a team across four slices of graded history.

    Returns (over, under) tuples plus sample sizes for: the team's last 10 games,
    their last 10 on today's side, their last 5 behind today's starter, and their
    last 3 behind that starter on today's side. Slices below the minimum sample
    are omitted. hist_games must be graded and oldest-first (load_history_games).
    """
    if not hist_games or not team:
        return None

    played = [
        g for g in hist_games
        if g.get("date", "") < today_s
        and team in (g.get("away_code"), g.get("home_code"))
    ]
    if not played:
        return None

    def is_home(g: dict) -> bool:
        return g.get("home_code") == team

    def rec(games: list[dict]) -> tuple[int, int]:
        over = sum(1 for g in games if g["over_hit"])
        return over, len(games) - over

    side = [g for g in played if is_home(g) == is_home_today]

    pid = str(pitcher_id or "")
    starts = [
        g for g in played
        if pid and pid in (str(g.get("away_pitcher_id") or ""),
                           str(g.get("home_pitcher_id") or ""))
    ] if pid else []
    starts_side = [g for g in starts if is_home(g) == is_home_today]

    out: dict = {"is_home": is_home_today}
    for key, games, n, floor in (
        ("last10",       played,      10, _OU_MIN_TEAM),
        ("last10_side",  side,        10, _OU_MIN_TEAM),
        ("sp_last5",     starts,       5, _OU_MIN_PITCHER),
        ("sp_last3_side", starts_side, 3, _OU_MIN_PITCHER),
    ):
        window = games[-n:]
        if len(window) >= floor:
            out[key] = rec(window)
            out[f"n_{key}"] = len(window)
    return out if len(out) > 1 else None


def extract_outings(history: list[dict], n: int = 5) -> list[dict]:
    """Return the n most-recent outings from a game-log list, newest first."""
    result = []
    for s in reversed(history):
        stat = s.get("stat", {})
        if not stat:
            continue
        raw_date = s.get("date") or s.get("game", {}).get("officialDate", "")
        # The game log spans TWO seasons (get_recent_starts fetches the prior year as
        # well), so a bare "Sep 24" on a vs-opponent meeting could be this September or
        # last one — and those are not equal evidence. Every outing carries its year:
        # the compact form for the HTML tables, the long one for the AI card.
        try:
            dt     = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            date_s = dt.strftime("%b %-d '%y")
            date_l = dt.strftime("%b %-d, %Y")
            year   = dt.year
        except Exception:
            date_s = date_l = raw_date[:10]
            year   = None

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
            "date_long": date_l,
            "year":      year,
            # Kept alongside the display strings because those are formatted for people
            # — this is the one that sorts and subtracts.
            "date_iso":  raw_date[:10],
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
    rhp_ctx: Optional[dict] = None,
    lhp_ctx: Optional[dict] = None,
    all_pool: Optional[dict] = None,
    all_ctx: Optional[dict] = None,
) -> dict:
    """Return structured analysis dict — used by both terminal and HTML renderers.

    `rhp`/`lhp` are the PRIMARY team-offense pools (last 6 games vs that hand) and drive
    every offense number and the offense edge. `rhp_ctx`/`lhp_ctx` are the longer window
    (last 12) and contribute exactly one thing: a comparison wRC+ alongside the primary
    one. They are optional so a caller with only the primary window still works.

    `all_pool`/`all_ctx` are those same two windows with no platoon split, and are read
    only when the opposing club is running a bullpen game. There is no starter's hand to
    split on then, so a "vs RHP" line would be describing a matchup that isn't happening.
    """
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
        mode = p.get("Mode") or "starter"
        opener = p.get("Opener") or None
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
            # "starter" | "opener" (this is the bulk arm behind one) | "bullpen"
            "mode":      mode,
            "opener":    opener,
            "role_why":  p.get("RoleWhy", ""),
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
            "whiff":     fp1(p.get("Whiff%")),
            "barrel":    fp1(p.get("Barrel%")),
            "h_per_gs":  (lambda h, g: f"{h/g:.1f}" if h and g else "?")(
                flt(p.get("H")), flt(p.get("Games"))),
        }

    def _off(batting: str, pitcher: dict) -> Optional[dict]:
        # On a bullpen game nobody's hand governs enough of the game to split on, so the
        # lineup is read against its unsplit form instead. That is also the one path that
        # still produces a card when the opposing arm's hand is unknown.
        bullpen_game = (pitcher.get("Mode") == "bullpen")
        hand = (pitcher.get("Throws") or "?")[0]
        # A page rendered for a date before the unsplit windows were being downloaded
        # has no overall pool to read. Falling back to the first arm's hand is a worse
        # answer than "all hands" but a much better one than an empty card.
        if bullpen_game and not (all_pool or {}).get(to_stats(batting)):
            bullpen_game = False
        if bullpen_game:
            pool, ctx_pool, hand_lbl = (all_pool or {}), (all_ctx or {}), "all hands"
        elif hand in ("R", "L"):
            pool     = rhp if hand == "R" else lhp
            ctx_pool = (rhp_ctx if hand == "R" else lhp_ctx) or {}
            hand_lbl = "vs RHP" if hand == "R" else "vs LHP"
        else:
            return None
        s = pool.get(to_stats(batting), {})
        if not s:
            return None
        wrc = flt(s.get("wRC+"))
        # Comparison window. Absent data (no L12 file, a club with no qualifying row, or
        # a null wRC+) degrades to N/A exactly the way the primary number does — the
        # column disappears rather than the card breaking.
        ctx_row = ctx_pool.get(to_stats(batting), {})
        wrc_ctx  = flt(ctx_row.get("wRC+")) if ctx_row else None
        return {
            "wrc":       wrc,
            "wrc_s":     f"{wrc:.0f}" if wrc is not None else "N/A",
            "label":     wrc_label(wrc) if wrc is not None else "",
            "wrc_ctx":   wrc_ctx,
            "wrc_ctx_s": f"{wrc_ctx:.0f}" if wrc_ctx is not None else "N/A",
            # Signed L6 − L12: positive means the lineup is hotter than its longer form.
            "wrc_gap":   (wrc - wrc_ctx) if (wrc is not None and wrc_ctx is not None) else None,
            "woba":      fp3(s.get("wOBA")),
            "k":         fp1(s.get("K%")),
            "k_ctx":     fp1(ctx_row.get("K%")),
            "bb":        fp1(s.get("BB%")),
            "bb_ctx":    fp1(ctx_row.get("BB%")),
            "hard":      fp1(s.get("HardHit%")),
            "hard_ctx":  fp1(ctx_row.get("HardHit%")),
            "whiff":     fp1(s.get("Whiff%")),
            "whiff_ctx": fp1(ctx_row.get("Whiff%")),
            "vs_hand":   "ALL" if bullpen_game else ("RHP" if hand == "R" else "LHP"),
            "hand_lbl":  hand_lbl,
            "bullpen_game": bullpen_game,
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
            "era":          era,
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
    # 15, not 10: the offense pools are a 6-game window now, and six games of wRC+ is a
    # noisier estimate than twelve. Keeping the old gap would call an "edge" on what is
    # often sampling noise — and would disagree with the prompt, which tells the model a
    # modest last-6 gap is not by itself decisive.
    off_edge = (
        None if wrc_a is None or wrc_h is None or abs(wrc_a - wrc_h) < 15
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

    away_vs_avg, away_vs_ot = _situational_split(
        [s for s in away_hist if (s.get("opponent") or {}).get("name", "") == home_full]
    )
    away_at_avg, away_at_ot = _situational_split(
        [s for s in away_hist
         if s.get("isHome") is False
         and (s.get("opponent") or {}).get("name", "") == home_full]
    )
    away_sp_splits = {
        "vs": away_vs_avg, "vs_outings": away_vs_ot,
        "vs_meta": meeting_recency(away_vs_ot, today),
        "at": away_at_avg, "at_outings": away_at_ot,
        "outings": _merge_outings(away_vs_ot, away_at_ot),
    }

    home_vs_avg, home_vs_ot = _situational_split(
        [s for s in home_hist if (s.get("opponent") or {}).get("name", "") == away_full]
    )
    home_at_avg, home_at_ot = _situational_split(
        [s for s in home_hist if s.get("isHome") is True]
    )
    home_sp_splits = {
        "vs": home_vs_avg, "vs_outings": home_vs_ot,
        "vs_meta": meeting_recency(home_vs_ot, today),
        "at": home_at_avg, "at_outings": home_at_ot,
        "outings": _merge_outings(home_vs_ot, home_at_ot),
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
    # Opener and bullpen games first — they change how every other line on the card
    # should be read, so they belong above the stat-derived flags rather than buried
    # among them.
    for team, sp, opp in [(away_team, away_sp, home_team), (home_team, home_sp, away_team)]:
        why = f" ({sp['role_why']})" if sp.get("role_why") else ""
        op  = sp.get("opener") or {}
        if sp["mode"] == "opener":
            flags.append(
                f"OPENER: {team} start {op.get('name', '?')} ({op.get('hand', '?')}HP)"
                f"{why}, with {sp['name']} ({sp['hand']}HP) expected to carry the bulk "
                f"of the innings. The stats and the hand shown are the bulk arm's — "
                f"{opp}'s offense line is split against {sp['hand']}HP, not the opener's."
            )
        elif sp["mode"] == "bullpen":
            first = (f"{op.get('name')} ({op.get('hand', '?')}HP) is listed first"
                     if op.get("name") else f"{sp['name']} is listed")
            flags.append(
                f"BULLPEN GAME: {team} have no conventional starter — {first}{why}. "
                f"Expect a parade of relievers; {opp}'s offense is shown against all "
                f"hands rather than a platoon split, and starter-based reads "
                f"(SP K/outs props, F5 lines) do not apply."
            )
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        name = p.get("Name", "?")
        for f in pitcher_csv_flags(p):
            flags.append(f"{team} — {name}: {f}")
    for team, sp in [(away_team, away_sp), (home_team, home_sp)]:
        f = pitcher_whiff_k_flag(sp)
        if f:
            flags.append(f"{team} — {sp.get('name', '?')}: {f}")
    for team, off in [(away_team, away_off), (home_team, home_off)]:
        f = team_whiff_k_flag(off)
        if f:
            flags.append(f"{team} offense: {f}")
    for team in [away_team, home_team]:
        b = bullpen.get(team, bullpen.get(to_stats(team), {}))
        for f in bullpen_flags(b):
            flags.append(f"{team} bullpen: {f}")
    for team, bp_d in [(away_team, away_bp), (home_team, home_bp)]:
        # Stress used to arrive with a verdict attached ("lean SP K/outs OVER" /
        # "...UNDER"). That was the lever behind the outs skew: §8 was rewritten to say
        # both sides of an outs prop are live, and the card went on telling the model
        # which one to take, in imperative form, next to the numbers. Outs picks over
        # the following weeks ran 9 unders to 1 over. The innings are the fact; §8
        # decides what they mean. The number itself now also rides on the bullpen line
        # for every team (see _bp_line), so this flag is only the exceptional cases.
        slabel = bp_d.get("stress_label", "")
        if slabel in ("Elevated", "Stressed", "Fresh"):
            ip    = bp_d.get("stress_ip", 0)
            g_cnt = bp_d.get("stress_games", 0)
            flags.append(
                f"{team} bullpen {slabel.lower()} ({ip:.1f} IP over {g_cnt}g)"
            )
    hist_cur_map = {away_team: away_hist_cur, home_team: home_hist_cur}
    for team, p in [(away_team, p_away), (home_team, p_home)]:
        hand = (p.get("Throws") or "?")[0]
        for f in pitcher_history_flags(hist_cur_map[team], hand, rhp, lhp, today):
            flags.append(f"{team} — {p.get('Name', '?')}: {f}")
    for f in weather_flags(wx, neutral_site=bool(mlb_info.get("neutral_site"))):
        flags.append(f"WEATHER: {f}")

    sgn  = mlb_info.get("series_game_number")
    gis  = mlb_info.get("games_in_series")
    for team, rec, opp_today in [
        (away_team, away_rec, to_mlb(home_team)),
        (home_team, home_rec, to_mlb(away_team)),
    ]:
        for f in team_situational_flags(rec, opp_today, sgn, gis, today_s):
            flags.append(f"TREND: {team} — {f}")

    away_div, home_div = division(away_team), division(home_team)
    if away_div and away_div == home_div:
        # The reasoning tail lived here AND in §11, so a weak-to-nonexistent effect was
        # asserted twice per game — once as a rule, once as a fact about this game — on
        # roughly two of every five games on a slate. §11 carries it once now.
        flags.append(f"TREND: DIVISIONAL GAME ({away_div})")

    # A neutral site invalidates anything derived from the home club's usual park —
    # most importantly the park factor, which the prompt uses as a total tiebreaker.
    # Say so explicitly rather than letting the model read Target Field's APF for a
    # game in Dyersville.
    if mlb_info.get("neutral_site"):
        where = mlb_info.get("venue", "") or "a neutral site"
        city  = mlb_info.get("venue_city", "")
        flags.append(
            f"NEUTRAL SITE: played at {where}"
            + (f" ({city})" if city else "")
            + f" — not {home_team}'s home park. Park factor and any home-park "
              "assumption do not apply here."
        )

    return {
        "away":          away_team,
        "home":          home_team,
        "venue":         mlb_info.get("venue", ""),
        "venue_city":    mlb_info.get("venue_city", ""),
        "neutral_site":  bool(mlb_info.get("neutral_site")),
        "game_date":     mlb_info.get("game_date", ""),
        "game_number":   mlb_info.get("game_number") or 1,
        "games_today":   mlb_info.get("games_today") or 1,
        "away_sp":       away_sp,
        "home_sp":       home_sp,
        "away_sp_id":    str(p_away.get("mlbam_id") or ""),
        "home_sp_id":    str(p_home.get("mlbam_id") or ""),
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
        # Posted an hour or two before first pitch; absent on the early runs and on the
        # tomorrow page. Passed straight through — nothing here derives from it, it only
        # reaches the AI card.
        "lineups":          mlb_info.get("lineups"),
        "bat_sides":        mlb_info.get("bat_sides") or {},
    }
