"""AI betting suggestions — generate, cache, and render Claude-powered picks."""

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from analysis import flt
from odds import fmt_k_line, fmt_outs_line

_ET = timezone(timedelta(hours=-4))


def _h(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── DOM / title helpers ───────────────────────────────────────────────────────

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

    if "total" in bet_type and game and line is not None and team_side:
        if team_side in ("over", "under"):
            ou = "u" if team_side == "under" else "o"
            bet_text = f"{game} {f5_tag}{ou}{line}"
        elif "_" in team_side:
            parts = game.split(" @ ", 1)
            away_team = parts[0].strip() if len(parts) == 2 else game
            home_team = parts[1].strip() if len(parts) == 2 else game
            team = away_team if team_side.startswith("away") else home_team
            ou = "u" if "under" in team_side else "o"
            bet_text = f"{team} {f5_tag}Team Total {ou}{line}"
        else:
            bet_text = bet.replace("Over ", "o").replace("Under ", "u")
            if game:
                bet_text = bet_text.replace("Game Total", game)
    else:
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


# ── AI system prompt ──────────────────────────────────────────────────────────

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


# ── Game serialization for AI prompt ─────────────────────────────────────────

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

    time_s = ""
    if g.get("game_date"):
        try:
            dt = datetime.fromisoformat(g["game_date"].replace("Z", "+00:00")).astimezone(_ET)
            h12 = dt.hour % 12 or 12
            time_s = f" | {h12}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'} ET"
        except Exception:
            pass

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

    hand_h = (sp_h.get("hand") or "?")[0]
    hand_a = (sp_a.get("hand") or "?")[0]

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
    k_a  = fmt_k_line(od.get("away_k"))
    k_h  = fmt_k_line(od.get("home_k"))
    ou_a = fmt_outs_line(od.get("away_outs"))
    ou_h = fmt_outs_line(od.get("home_outs"))
    prop_parts = []
    if k_a or ou_a:
        prop_parts.append(f"{sp_a['name']}: {', '.join(p for p in [k_a, ou_a] if p)}")
    if k_h or ou_h:
        prop_parts.append(f"{sp_h['name']}: {', '.join(p for p in [k_h, ou_h] if p)}")
    if prop_parts:
        odds_lines.append("  Props: " + " | ".join(prop_parts))

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


# ── AI call + caching ─────────────────────────────────────────────────────────

def generate_suggestions(games: list[dict], data_dir: Path, target_date: date) -> Optional[dict]:
    """
    Call Claude to generate betting suggestions. Caches to data/suggestions_{date}.json
    and regenerates whenever odds are updated. Returns parsed dict or None on failure.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    sugg_path = data_dir / f"suggestions_{date_str}.json"
    sugg_meta = data_dir / f"suggestions_meta_{date_str}.json"
    odds_meta  = data_dir / f"odds_meta_{date_str}.json"

    if sugg_path.exists() and sugg_meta.exists():
        try:
            s_ts = datetime.fromisoformat(json.loads(sugg_meta.read_text())["generated_at"])
            if odds_meta.exists():
                o_ts = datetime.fromisoformat(json.loads(odds_meta.read_text())["fetched_at"])
                if s_ts >= o_ts:
                    return json.loads(sugg_path.read_text())
            else:
                if (datetime.now(timezone.utc) - s_ts).total_seconds() < 14400:
                    return json.loads(sugg_path.read_text())
        except Exception:
            pass

    try:
        import anthropic as _ant
    except ImportError:
        print("[suggestions] anthropic package not installed — skipping", file=sys.stderr)
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
        print("[suggestions] ANTHROPIC_API_KEY not set — skipping", file=sys.stderr)
        return None

    if not games:
        return None

    _now = datetime.now(timezone.utc)
    unstarted = []
    for _g in games:
        _gt = _g.get("game_time_utc", "")
        if not _gt:
            unstarted.append(_g)
            continue
        try:
            if datetime.fromisoformat(_gt.replace("Z", "+00:00")) > _now:
                unstarted.append(_g)
        except Exception:
            unstarted.append(_g)
    if not unstarted:
        print("[suggestions] All games have started — skipping AI call", file=sys.stderr)
        return json.loads(sugg_path.read_text()) if sugg_path.exists() else None

    n_skipped = len(games) - len(unstarted)
    if n_skipped:
        print(f"[suggestions] Skipping {n_skipped} already-started game(s)", file=sys.stderr)

    game_blocks = "\n\n".join(_serialize_game_for_ai(g) for g in unstarted)
    user_msg = (
        f"Today is {date_str}. Analyze these {len(unstarted)} MLB games and "
        f"identify any strong betting opportunities:\n\n{game_blocks}"
    )

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
            print("[suggestions] No tool_use block in response", file=sys.stderr)
            return None
        result = tool_block.input
    except Exception as e:
        print(f"[suggestions] API error: {e}", file=sys.stderr)
        return None

    try:
        sugg_path.write_text(json.dumps(result, indent=2))
        sugg_meta.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat()}))
    except Exception:
        pass

    return result


# ── HTML rendering ────────────────────────────────────────────────────────────

def _render_suggestions_html(all_picks: list, target_date: date) -> str:
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
        gt      = _h(pick.get("game_time_utc", ""))
        return (
            f'<details class="ai-pick-row" id="{pid}" data-game-time="{gt}">'
            f'<summary class="ai-pick-sum">{title}</summary>'
            f'<div class="ai-pick-body">'
            f'<div class="ai-reason">{reason}</div>'
            f'{found_s}'
            f'{warn_s}'
            f'</div>'
            f'</details>'
        )

    # active_picks/started_picks below is only the split as of render time (last
    # cron run) — a static page can go hours before the next regeneration, so
    # games that started since then would otherwise stay stuck in "active" until
    # the next run. Both wraps always render (started-wrap hidden if empty) so
    # splitPicks() in the page JS can move newly-started picks over client-side,
    # using the viewer's actual current time — mirrors split() for game cards.
    inner = (
        f'<div class="ai-active-wrap" id="ai-active-wrap">'
        f'{"".join(_pick_block(p) for p in active_picks)}'
        f'</div>'
        f'<div class="ai-started-wrap" id="ai-started-wrap"{"" if started_picks else " hidden"}>'
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
    Build per-game AI lookup: {(away @ home, game_time_utc): {"picks": [...], "pass_reason": str|None}}.
    Keyed by (game, game_time_utc) rather than just "AWAY @ HOME" so doubleheader legs
    (same matchup string, different start times) don't merge their picks together.
    valid_picks: all saved picks for the day (includes started games).
    suggestions: latest run result, used only for pass_reasons on games with no picks.
    """
    picks_by_game: dict[tuple, list] = {}
    for p in (valid_picks or []):
        game = p.get("game", "")
        if game:
            picks_by_game.setdefault((game, p.get("game_time_utc", "")), []).append(p)

    pass_reasons = (suggestions or {}).get("pass_reasons") or {}

    result: dict = {}
    for key, picks in picks_by_game.items():
        result[key] = {"picks": picks, "pass_reason": None}
    for game, reason in pass_reasons.items():
        key = (game, "")
        if key not in result:
            result[key] = {"picks": [], "pass_reason": reason}
    return result


def _lookup_ai_for_game(ai_by_game: dict, away: str, home: str, game_time_utc: str) -> Optional[dict]:
    """Look up ai_by_game for a rendered game card, matching on time first, then
    falling back to any entry for the matchup (covers games with no recorded time)."""
    game = f"{away} @ {home}"
    hit = ai_by_game.get((game, game_time_utc))
    if hit is not None:
        return hit
    for (g, _t), val in ai_by_game.items():
        if g == game:
            return val
    return None
