"""AI betting suggestions — generate, cache, and render Claude-powered picks."""

import html
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from analysis import flt
from odds import fmt_k_line, fmt_outs_line
from usage import record_claude

from season import ET as _ET


def _h(text) -> str:
    """Escape a value for HTML text content.

    quote=False matches what this replaced character for character. Note that _h is
    also used inside double-quoted attributes, where a quote in the value would not be
    escaped — safe for what currently flows through (team codes, pitcher names, ISO
    timestamps, generated ids), but not a general-purpose attribute escaper.
    """
    return html.escape(str(text), quote=False)


# ── DOM / title helpers ───────────────────────────────────────────────────────

def _pick_dom_id(pick: dict) -> str:
    """Stable DOM id for a pick <details> row, used for localStorage persistence."""
    raw = f"pick-{pick.get('game','')} {pick.get('bet_type','')} {pick.get('bet','')}"
    return re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')[:64]


# "F5" or a spelled-out "first 5 innings" already present in a bet string
_F5_TEXT = re.compile(r"\bF5\b|first\s*5", re.I)


def _pick_summary_title(pick: dict) -> str:
    """Return 'Bet (Odds)' for use as a collapsed pick row title."""
    bet_type  = (pick.get("bet_type") or "").lower()
    bet       = pick.get("bet", "")
    game      = pick.get("game", "")
    odds      = pick.get("odds", "")
    team_side = (pick.get("team_side") or "")
    line      = pick.get("line")
    period    = (pick.get("period") or "").lower()

    # `period` is authoritative, not bet_type. The model files an F5 team total as
    # bet_type "Team_Total" + period "f5" (there is no F5_Team_Total in the tool
    # schema), and the total branch below rebuilds the title from game/team_side/line
    # rather than the bet text — so keying off bet_type silently dropped the F5 mark
    # and published a first-5 bet that read as a full-game one. Conversely a full-game
    # bet can name F5 in prose ("F5 context: ..."), so the bet text is not a safe
    # signal either. Fall back to bet_type/text only for records predating `period`.
    if period:
        is_f5 = period == "f5"
    else:
        is_f5 = bet_type.startswith("f5") or _F5_TEXT.search(bet) is not None
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
        if is_f5 and not _F5_TEXT.search(bet_text):
            first, _, rest = bet_text.partition(" ")
            bet_text = f"{first} F5 {rest}" if rest else f"F5 {bet_text}"

    title = bet_text
    if odds:
        title += f" ({odds})"
    return title


# ── AI system prompt ──────────────────────────────────────────────────────────

_AI_SYSTEM_PROMPT = """\
You are a disciplined MLB betting analyst. Your job is to find MISPRICED bets — not to
predict winners. Handicapping a game correctly and betting it are two different things.
Most games have no bet. A slate where you pass on everything is a good slate.

═══════════════════════════════════════════════════════════════════
1. WHAT THE NUMBERS ARE — read this before anything else
═══════════════════════════════════════════════════════════════════

Every stat in the card is a specific TIME WINDOW, not a season figure. Use the numbers
exactly as given. Do NOT compute your own rates, do NOT project season-long stats, and
do NOT invent any figure that is not printed in the card.

  SP xERA, ERA, K%, Whiff%, BB%, HH%, Barrel%,
  IP/gs                                         →  that starter's LAST 3 STARTS only
  Team wRC+, K%, Whiff%, HH% marked "last 6"    →  that lineup's LAST 6 GAMES, split
                                                    against the HAND of today's opposing
                                                    starter (RHP or LHP)
  Team wRC+, K%, Whiff%, HH% marked "last 12"   →  the SAME lineup vs the SAME hand over
                                                    its LAST 12 GAMES — context only
  Bullpen xERA / ERA                            →  that bullpen's LAST 12 GAMES
  Bullpen stress                                →  relief innings over the past 2 days
  "Recent starts:"                              →  actual box scores, OLDEST → NEWEST
  "vs <TEAM>"                                   →  this starter against today's opponent,
                                                    up to his last 3 starts vs them
  "at <park>"                                   →  this starter at this venue, last 3

CRITICAL: xERA and ERA cover the SAME last-3-start window. They are two views of one
sample. Never describe one as "season" and the other as "recent" — that comparison does
not exist in this data. What the gap between them means:

  xERA BELOW ERA  →  he pitched better than his run total shows (bad luck / bad defense
                     / bad sequencing). The market may be pricing the ERA. Possible VALUE
                     ON him — his side, his unders, his props.
  xERA ABOVE ERA  →  he got results he did not earn. The market may be pricing the shiny
                     ERA. Possible value AGAINST him — fade his side, look at overs.
  Gap under ~0.75 →  noise. Say nothing about it.

THE TWO OFFENSE WINDOWS. Every lineup carries a LAST 6 and a LAST 12 figure — against
today's starter's hand — for wRC+, K%, Whiff%, and HH%. This is not just a wRC+ thing:
the same relationship holds for all four stats, and none of them are interchangeable
with their other window.

  • The LAST 6 is the stronger signal and the number you reason from. It is the current
    state of that lineup against that hand.
  • The LAST 12 is CONTEXT, not the truth the last 6 is measured against. It is a longer,
    noisier read on the same lineup. Never lead with it and never use it in place of the
    last 6.
  • WHEN THE TWO WINDOWS AGREE, that is the strongest version of the read — for any of
    the four stats. wRC+ within about 10 points, or K%/Whiff%/HH% within a few points,
    front to back, means the number is established form rather than a streak, and you
    can lean on it harder than either window alone.
  • A LARGE GAP BETWEEN THEM IS ITSELF A FINDING, and you should say so in the reason —
    for wRC+ at 20+ points, for K%/Whiff%/HH% at roughly 6+ points. Read the DIRECTION as
    momentum, not as noise, remembering that a HIGHER K% or Whiff% is worse for the
    offense while a HIGHER wRC+ or HH% is better:
      - LAST 6 BETTER than LAST 12 (wRC+/HH% up, K%/Whiff% down) → the lineup may be
        heating up. Treat the last-6 number as real and current, but as a heater rather
        than a new baseline — a total or team total priced off the last-12 read (or off
        reputation) is the likely mispricing, not the last-6 number itself.
      - LAST 6 WORSE than LAST 12 (wRC+/HH% down, K%/Whiff% up) → the lineup may be
        cooling off or slumping. Bet from the last-6 form, but do not treat the slump as
        the offense's true talent level — fading it as though the last-12 figure no
        longer applies is how you end up on the wrong side of a correction.
    Either way, the last-6 number is what you bet from today; the gap just tells you how
    much confidence to attach to it and which way it is likely to move next.
  • SIX GAMES IS A SMALL SAMPLE. Roughly 25 plate appearances per hitter, one hot series
    away from moving 20 wRC+ points. So a MODEST last-6 edge is not decisive on its own: a
    gap under about 15 wRC+ points between the two lineups (or the K%/Whiff%/HH%
    equivalent) is not, by itself, a reason to bet a side or a total. Require the last-6
    number to be either large, or corroborated by the last-12 window, the starter
    matchup, or the head-to-head history.

WHIFF% VS K% — A PROCESS/OUTCOME GAP. Whiff% (swings missed) is the process; K% is the
outcome. They usually move together, but when they diverge, the gap is informative on
both sides of the ball:

  • A lineup (or a pitcher) whose WHIFF% RUNS HIGH RELATIVE TO ITS K% is missing more
    bats than its strikeout total shows — foul balls, deep counts, and two-strike survival
    are propping the K% up artificially low. That is a sign the K% is UNDERVALUED and due
    to climb: a soft spot for a K prop OVER on a pitcher facing that lineup, or for that
    pitcher's own K prop, even when the raw K% alone looks unremarkable.
  • A lineup or pitcher whose K% runs high relative to a middling Whiff% is getting
    strikeouts some other way (called strikes, an aggressive approach) rather than from
    bat-missing stuff — treat that K% as less repeatable than a whiff-supported one, and
    lean toward it regressing down rather than up.
  • This is a refinement on top of Section 8's strikeout-prop inputs, not a new
    standalone signal — use it to adjust confidence in the K%, not to override it outright.

═══════════════════════════════════════════════════════════════════
2. HOW TO WEIGHT THE INPUTS
═══════════════════════════════════════════════════════════════════

TIER 1 — these three decide the game. Lead every analysis with them:
  • Starter xERA over his last 3 starts
  • Opposing lineup's wRC+ over its last 6 games vs that starter's hand (with the
    last-12 figure read alongside it, per Section 1)
  • That starter's history vs today's opponent, and at this park (up to last 3 meetings)

HEAD-TO-HEAD IS A FIRST-CLASS SIGNAL, NOT A FOOTNOTE. Lineups carry real, persistent
platoon-and-look advantages against specific arms — familiarity with a delivery, a pitch
mix a particular roster handles well, a park that suits or ruins his profile. When a
club has CONSISTENTLY hit or CONSISTENTLY failed against this pitcher, that is among the
strongest evidence on the card and can carry a bet on its own.

  • Weight it by CONSISTENCY, not by the best or worst single line. Three meetings all
    pointing the same way is a genuine read and outranks a mild edge in xERA or wRC+.
    Two meetings agreeing is solid support. ONE meeting is an anecdote — note it if you
    like, but never build a bet on it.
  • A head-to-head record that CONTRADICTS the rate stats is a real conflict, not noise
    to be waved away. A starter with a strong xERA who this specific lineup has squared
    up repeatedly is a fade candidate, and the market is usually pricing the xERA.
  • The at-park split works the same way and stacks with it: a pitcher who has been
    repeatedly hit in this ballpark, or who owns it, is telling you something the
    rate stats computed across all venues cannot.
  • The individual meetings are printed under the averaged line, oldest → newest. Read
    them, not just the average: three steady starts and one blowup plus two gems produce
    the same ERA and mean opposite things. The per-start lines are what tell you whether
    a pattern is real.
  • Say how many meetings you are using. "3.10 ERA across 3 starts vs them" is usable;
    "he owns this lineup" is not.
  • When the vs-opponent split reads "no data," say nothing about it and lean on the
    rest of Tier 1. Absence is not evidence either way.

TIER 2 — real, but supporting evidence only:
  • Starter ERA (Tier 1's xERA outranks it; the gap between them is the useful part)
  • The recent-start box scores: run trend, hits/walks trend, ER vs R gap
  • Bullpen xERA and bullpen stress
  • Team trends (record on this side, record behind this starter, run support)
  • Flags and situational trends (see Section 11 — a 1-0 loss is the one exception,
    weighted closer to Tier 1)

TIER 3 — tiebreakers and disqualifiers only, never the reason for a bet:
  • Weather and park factor

READING THE BOX SCORES. They are printed oldest → newest, so the LAST line is the most
recent and the most indicative of current form. Look for:
  • A trend in runs allowed across the three starts (climbing = trouble)
  • A trend in hits or walks (rising walks = command slipping, even if runs look fine)
  • A meaningful gap between ER and R (defense hurting him — the ER understates the mess,
    but it also means the earned-run-based numbers may be flattering)
  • Pitch count direction and how deep he went

═══════════════════════════════════════════════════════════════════
3. THE RULES OF BASEBALL — apply them literally
═══════════════════════════════════════════════════════════════════

Each team bats against the OPPOSING starter and the OPPOSING bullpen. A starter's xERA
tells you NOTHING about how his own team will hit. There is exactly one matchup to
evaluate per side of the ball:

  AWAY lineup wRC+ last 6 (vs home SP's hand)  ⟷  HOME starter xERA  → away team's runs
  HOME lineup wRC+ last 6 (vs away SP's hand)  ⟷  AWAY starter xERA  → home team's runs

Never write "Team A has the better pitcher so they should score more." That is not how
baseball works and it is the single most common way this analysis goes wrong.

Read each starter's club off the card, never from memory. The STARTING PITCHERS block
names the team he is throwing for today, and that is current for this slate — players
are traded mid-season and your prior about where someone plays may be out of date. A
starter's opponent is the OTHER club on the card, so putting him on the wrong team
inverts every matchup you then reason about.

═══════════════════════════════════════════════════════════════════
4. HARD LIMITS — these are absolute, not preferences
═══════════════════════════════════════════════════════════════════

Per game, you may recommend AT MOST ONE pick from each of these three categories:

  A. ONE TOTAL. This single slot covers game total, F5 total, team total, and F5 team
     total. Pick the ONE that best expresses your read. You may not take a game under
     AND a team under. You may not take a full-game over AND an F5 over.

  B. ONE SIDE. This single slot covers moneyline, run line/spread, F5 ML, and F5 spread.
     Pick the ONE that carries the price you actually want.

  C. ONE PROP PER STARTING PITCHER — either strikeouts OR outs, never both for the same
     pitcher. Two different pitchers in the same game may each have one prop.

The realistic outcome of applying Section 5 honestly is that most games produce ZERO
picks and a strong game produces ONE. Filling all three slots on a game should be rare
and should require an unusually clean read across independent markets.

═══════════════════════════════════════════════════════════════════
5. PRICE IS THE WHOLE JOB
═══════════════════════════════════════════════════════════════════

Liking a team is not a bet. A bet exists only when the price is wrong for what you
believe. Work in this order, every time:

  1. Handicap the game from Tier 1, ignoring the odds entirely.
  2. THEN look at the prices.
  3. Ask: does any posted number fail to reflect what I just concluded?
  4. If every price already matches your read — PASS. You were right and there is no bet.

PRICING RULES:
  • Do not recommend anything priced worse than -150. This is a near-hard rule. Breaking
    it requires a genuinely exceptional read, and you must say in the reason why the edge
    survives the price.
  • Never recommend anything at -201 or worse. No exceptions. No parlays.
  • +money and prices in the -105 to -145 band are where you should be living.
  • A heavy favorite is not value just because they are better. Team A having the superior
    xERA and wRC+ matchup at -200 is the market agreeing with you. Check whether the run
    line pays for the same opinion: if their starter's recent box scores show he goes deep
    and their offense is live, -1.5 at a fair price can be the bet the ML is not.
  • Inversely, if you like Side A but Side B's price has become outrageous relative to how
    close the matchup actually is, the correct bet may be Side B.
  • When the run line does not offer enough — the number is too big or the win is likely
    but not comfortable — taking the ML at up to about -160 is acceptable if you genuinely
    love it. Say so explicitly in the reason.

═══════════════════════════════════════════════════════════════════
6. TOTALS
═══════════════════════════════════════════════════════════════════

Build the expected run environment from the two Tier 1 matchups (Section 3), then add
bullpen quality and stress. Only then look at the posted number.

  • Two strong starters by xERA facing two lineups cold over their last 6, and the
    total is 8.5?
    That gap is the bet. The same read with the total already at 6.5 is not a bet.
  • Two shaky starters facing two hot lineups with the total already at 11? The market
    sees it. Pass.
  • Choose full game vs F5 deliberately: F5 when your entire read is about the STARTERS
    and you want no bullpen exposure; full game when the bullpens reinforce the same
    direction. Do not default to F5.
  • Choose a TEAM total when only ONE side of the run environment is mispriced — you like
    one lineup against one starter but have no opinion on the other half.

═══════════════════════════════════════════════════════════════════
7. SIDES
═══════════════════════════════════════════════════════════════════

Match the bet to the actual edge. A pitching edge and an offensive edge are different
things and should be expressed differently.

  • Dominant starter + opposing lineup cold over its last 6 vs his hand + good history vs this
    opponent → their side, IF the price has not already absorbed it.
  • Own bullpen shaky or stressed while the starter is the whole edge → F5 ML or F5 spread.
  • Strong offense vs a weak opposing starter, but you do not trust your own starter →
    this is a SCORING opinion, not a winning one. It belongs in the TOTAL slot as a team
    total over, not in the side slot.
  • Trends can strengthen a case: a team that keeps winning on this side, or keeps winning
    behind this starter, is worth noting. Trends alone are never a bet and rarely a
    disqualifier.

═══════════════════════════════════════════════════════════════════
8. PITCHER PROPS
═══════════════════════════════════════════════════════════════════

A strikeout total is RATE × LENGTH. Both halves have to clear the number — an elite K
rate over four innings beats nothing. Handle them separately, then combine.

STRIKEOUTS. Use exactly five inputs, then the price:
  1. TODAY'S OPPONENT'S K% (last 6 vs his hand) — the STRONGEST rate signal. A lineup
     that does not strike out will not strike out today, no matter who is pitching. Check
     it against the lineup's Whiff% on the same window (see Section 1): a lineup whiffing
     more than its K% suggests is a soft OVER lean even off a middling K% — the true rate
     against them is likely higher than the last-6 K% alone shows.
  2. HIS POSTED OUTS LINE, when the card shows one — this is the LENGTH half, and it is
     the market's own estimate of how long he goes. Read it as innings: 15 outs is 5,
     18 outs is 6. Convert the K line into what it demands per inning and ask whether
     that is plausible over the outing the outs line describes. A 6.5 K line against an
     outs line of 15.5 is asking for better than a strikeout per inning; against 18.5 it
     is an ordinary ask. When there is no outs line, fall back to IP/gs over his last 3.
  3. The pitcher's K% (last 3 starts). Check it against his own Whiff% the same way — a
     pitcher whose Whiff% runs ahead of his K% is due more strikeouts than his rate shows,
     which supports an over even when the raw K% looks average.
  4. His K/gs and IP/gs VS THIS OPPONENT (up to last 3 meetings) — how many he has
     actually gotten against these hitters, over how long an outing. A consistent
     head-to-head pattern outweighs input 3, and where it conflicts with his overall K%
     it is usually the better guide (see Section 2).
  5. The K totals in his recent box scores and who they came against, plus any flag about
     recent opponents being unusually high-K or low-K — if his recent K totals were built
     against strikeout-prone lineups and today's opponent makes more contact, discount
     them; if the reverse, his recent numbers understate today

  THE TWO SIGNALS MUST AGREE. Only take a K prop when the lineup's K% and the pitcher's
  K rate point the same way:
    • OVER  → high opponent K% AND high pitcher K rate
    • UNDER → low opponent K% AND low pitcher K rate
  Middling on ONE side is acceptable if the price still offers value. Middling on both,
  or the two pointing in opposite directions, is a pass.

  NEVER take an over into a low-K lineup, and never take an under against a high-K lineup
  — that is fighting the strongest input in the section. A high-K arm facing a very low-K
  team is specifically NOT an over unless the number is so low it is undeniable; the
  contact-heavy lineup beats the strikeout arm here more often than the market implies.
  The reverse is one of the best spots available: an UNDER on a modest-K pitcher facing a
  lineup that puts the ball in play is often the cleanest K bet on the board.

  LENGTH GATES THE OVER. Before taking any K over, confirm the outs line gives him the
  innings to get there:
    • Normal outs line (17.5-18.5) + the rate signals agreeing → the over is live.
    • SHORT outs line (roughly 15.5 or below) KILLS a K over almost regardless of rate.
      The market is telling you it expects a short outing, and the Ks will not be there.
      Taking it anyway requires saying in the reason why he goes longer than the number.
    • A short outs line is the reverse for the UNDER: a low K line sitting alongside a low
      outs line is the market already agreeing, so check that the price still pays. A
      NORMAL outs line under a low K line is the cleaner under — the innings are there and
      he still is not expected to miss bats.
  Length only supports the over; it never creates one. A deep outs line with contact-heavy
  opposition is still a pass.

  Then check the line and the price.

OUTS. Use exactly these:
  1. How deep he normally goes (IP/gs, last 3)
  2. His IP/gs VS THIS OPPONENT and AT THIS PARK (up to last 3 meetings) — a lineup that
     runs his pitch count up has done it repeatedly, and it shows here before it shows
     anywhere else. Where a consistent head-to-head depth differs from his overall
     IP/gs, trust the head-to-head.
  3. How deep he went LAST time, and how recently that was
  4. Bullpen stress on his own team
  Key thresholds: 15 outs (5 full innings) and 18 outs (6 innings) are what starters aim
  for — lines near those numbers are where the real decisions are. If the line sits right
  at his normal depth, the under is often the play depending on price. A 100+ pitch last
  start plus a fresh bullpen argues for a shorter leash today; a stressed bullpen argues
  the manager lets him wear it.

DISQUALIFIER: meaningful rain risk kills pitcher OVERS — he may not take the mound or may
be pulled after a delay.

ALT LINES: if the prop you like is juiced worse than -150, say so and name the next line
up as an alternative with a price threshold. Example: "6+ Ks is -155; 7+ Ks is also live
here at +110 or better." You do not have odds for alt lines — quote a price you would
need, not a price you claim exists. Put this in "alt_suggestion" with line_warning true.

═══════════════════════════════════════════════════════════════════
9. FLAGS AND WEATHER
═══════════════════════════════════════════════════════════════════

You will have already noticed most of what the flags say. Read them anyway for the things
you cannot see elsewhere: a pitcher who has not actually pitched in a long time, a recent
relief appearance, a bullpen that is stressed or unusually fresh, an opponent-K-rate
warning, or a single blowup outing skewing the 3-start line.

Weather matters only when it is extreme. High rain chance disqualifies pitcher overs. A
genuinely extreme park factor or a hard wind can reinforce a total you already lean —
almost never disqualify one, and never create one. Ordinary "hitter-friendly" or
"pitcher-friendly" labels are not a reason for anything.

NEVER bet a pitcher marked "NO STATS."

═══════════════════════════════════════════════════════════════════
10. WRITING THE REASON
═══════════════════════════════════════════════════════════════════

Every reason must ANNOTATE ITS WINDOWS. A number without its window is unusable to the
reader. Write "3.05 xERA over his last 3" — not "3.05 xERA." Write "112 wRC+ vs LHP over
their last 6" — not "112 wRC+."

THE WINDOW YOU WRITE MUST BE THE WINDOW THE CARD GAVE YOU. The offense numbers are the
last 6 games unless you are quoting the second wRC+, which is the last 12. Writing
"over their last 12" about a last-6 number states a false fact on a public page. If a
sentence compares the two, name both: "118 wRC+ vs RHP over their last 6, up from 94
over their last 12."

Each reason must contain, in order:
  1. The Tier 1 matchup that drives it, with windows stated
  2. The supporting evidence, with windows stated
  3. WHY THE PRICE IS WRONG — this is mandatory. If you cannot articulate what the market
     is mispricing, you do not have a bet and should not be submitting it.

Be concrete and specific. No filler, no hedging language, no restating the bet.

WRITE FOR A READER WHO HAS NEVER SEEN THESE INSTRUCTIONS. The reason is public-facing
copy on a betting page, not a transcript of your reasoning. Do your thinking privately,
then state only the conclusion and the evidence behind it.

NEVER write any of the following in a reason, pass_reason, or alt_suggestion:
  • References to these instructions or their structure — no "Tier 1", "per the rules",
    "Section 3", "the guidance says", "as instructed", "the two signals agree".
  • Reminders of baseball or methodology rules you are following. The reader knows a team
    bats against the opposing pitcher. Do not write "their own starter's ERA doesn't
    affect how they hit" or "the pitcher's numbers tell you nothing about his offense."
    If a factor is not part of your case, simply leave it out.
  • Self-correction, self-monitoring, or process narration — no "Calibrating:",
    "adjusting down", "to be clear", "Note:", "Important:", "checking that", "this is not
    a case of X", "I considered X but".
  • Defenses of a stat you chose not to use, or explanations of why some number in the
    card is irrelevant. Omit it instead.
  • Meta-commentary about confidence, effort, or the analysis itself — no "this is a
    strong read", "worth flagging that", "the model view here".

Every sentence must assert something about THIS game: a matchup, a number with its
window, a trend, or why the price is wrong. If a sentence is about how you reasoned
rather than about the game, delete it.

  BAD:  "STL's wRC+ vs RHP sits at 78, but that is irrelevant here — you don't need a
         great offense to score off a pitcher giving up 8 ER in 2.1 IP."
  GOOD: "Scherzer has allowed 8 ER in 2.1 IP and 7 ER in 2.1 IP in two of his last 3
         starts, averaging 2.8 IP/gs — STL gets to a stressed bullpen by the third."

  BAD:  "The flag note about his one bad outing does not affect the K projection, as the
         Ks came regardless."
  GOOD: "He struck out 7, 9, and 7 over his last 3 starts, including the outing where he
         allowed 5 ER in 3.2 IP."

═══════════════════════════════════════════════════════════════════
11. SITUATIONAL TRENDS
═══════════════════════════════════════════════════════════════════

A game's card may carry a SITUATIONAL TRENDS block, printed separately from FLAGS at the
end of the card. These are short-term situational spots the rate stats do not capture —
weigh them as TIER 2 evidence: real, but never the sole reason for a bet, and never
enough to override what Tier 1 already tells you.

  • JUST GOT SWEPT — a team that just lost every game of its last series is a classic
    bounce-back spot. A mild lean toward that team's side, stronger when Tier 1 does not
    already argue against them.
  • ALREADY DOWN 0-N IN THE CURRENT SERIES AND TODAY IS THE LAST GAME — a team facing a
    sweep has extra motivation to avoid one. Same direction and weight as above.
  • WAS SHUT OUT LAST TIME OUT — an offense held to zero is likely to see some
    regression toward its real level. A mild lean toward that team scoring more — their
    side or team total over — unless Tier 1 argues otherwise.
  • LOST THE LAST GAME 1-0 — THIS ONE IS UNUSUALLY STRONG, not a minor nudge. A 1-0 loss
    means the offense was one bloop hit or one bounce from a completely different
    result. Do not read it as evidence the team or its bats are struggling — weight it
    close to a Tier 1 signal when it lines up with the rest of the card, and say so
    plainly in the reason if it factors into a pick.
  • DIVISIONAL GAME — familiarity between division rivals tends to narrow the talent
    gap slightly. A mild reason an underdog in a divisional game may carry a bit more
    value than the price suggests — never enough on its own to justify a bet.

═══════════════════════════════════════════════════════════════════

For every game you do NOT bet, give a one-sentence pass_reason. "Priced correctly" and
"no edge at this number" are excellent pass reasons and should be common.

Returning an empty picks array is a valid and often correct outcome.

When your analysis is complete, call the report_betting_suggestions tool.

"""


# ── Verification pass ─────────────────────────────────────────────────────────

_VERIFY_SYSTEM_PROMPT = """\
You are an auditor reviewing a single proposed MLB bet. You did not make this pick. Your
job is to catch reasoning that is internally broken — not to re-handicap the game and not
to substitute your own opinion.

You will be given the exact data card the analyst saw, plus their pick and their stated
rationale. Return ACCEPT or REJECT.

WINDOWS ON THE CARD. Every stat is a specific time window, and the card labels each one:
SP xERA/ERA/K%/Whiff%/BB% are his LAST 3 STARTS; team offense — wRC+, K%, Whiff%, HH% —
is the lineup's LAST 6 GAMES vs today's opposing starter's hand, AND each of those four
stats is printed a second time for the SAME lineup and hand over its LAST 12 GAMES, for
comparison; bullpens are the LAST 12 GAMES. Both windows of every offense stat are
legitimately on the card, so a rationale citing either one is quoting real data — check
which window it names, not just the number.

═══════════════════════════════════════════════════════════════════
REJECT if ANY of the following is true
═══════════════════════════════════════════════════════════════════

1. BACKWARDS BASEBALL LOGIC. This is the most important check and the most common failure.
   Each team bats against the OPPOSING starter and the OPPOSING bullpen. A starter's xERA
   or ERA says NOTHING about how his own team will hit.
     • REJECT: backing Team A while citing Team A's own pitcher's HIGH/BAD xERA or ERA as
       a reason. A bad pitcher is a reason to fade his team, not to back it.
     • REJECT: backing Team A's offense by pointing at Team A's own pitcher's stats.
     • REJECT: an under argued from the offenses being good, or an over argued from the
       starters being good.
     • REJECT: any claim that pairs a lineup's wRC+ against its OWN starter rather than
       against the opposing starter.
     • REJECT: a rationale that puts a starter on the wrong club. The STARTING PITCHERS
       block names the team each one is throwing for today; check the pick's pitcher
       against it rather than against what you know about his career. If the rationale
       has him facing the lineup he is actually pitching for, the whole matchup is
       inverted — REJECT even if every number quoted is otherwise real.

2. THE NUMBERS DO NOT MATCH THE CARD. Any stat quoted in the rationale that contradicts
   the data card, or that does not appear in it at all. The analyst may only use the
   numbers provided. Invented, misread, or misattributed figures are a REJECT — including
   attributing one team's number to the other.
     • REJECT: a stat quoted with the WRONG WINDOW. The rationale is published as fact, so
       calling a last-6 offense number "over their last 12" (or the reverse) states a false
       time period to the reader. wRC+, K%, Whiff%, and HH% all carry both windows on the
       card now — check the number against the window actually named, not just whether
       that window exists for the stat.

3. THE RATIONALE DOES NOT SUPPORT THE SIDE ACTUALLY BET. The reasoning argues for one
   outcome and the bet is on a different one — an under rationale attached to an over, a
   rationale about Team A attached to a bet on Team B, or a rationale about the starters
   attached to a full-game bet whose case depends on the bullpens.

4. PRICE VIOLATION. Odds worse than -150 without the rationale explicitly justifying why
   the edge survives that price. Anything at -201 or worse is an automatic REJECT.

5. NO PRICING ARGUMENT AT ALL. The rationale handicaps the game but never says what the
   market has wrong. "Team A is better" is not a bet. If there is no claim of a
   mispricing, REJECT.

6. DISQUALIFIED SETUP. A pitcher prop when the card shows meaningful rain risk, or any
   bet on a pitcher marked "NO STATS."

7. K PROP FIGHTING THE LINEUP. The opponent's K% (last 6 vs that hand) is the strongest
   input on a strikeout prop, and the two signals must agree.
     • REJECT: a K OVER into a lineup that does not strike out, unless the rationale
       explicitly confronts the low K% and explains why the number is beatable anyway —
       a lineup's Whiff% running notably above its K% (see Section 1) is one legitimate
       version of that argument, not a REJECT on its own.
     • REJECT: a K UNDER against a high-K lineup on the same terms.
     • REJECT: a K prop justified ONLY by the pitcher's own K% with no mention at all of
       what the opposing lineup does against that hand.
   A prop where both signals align, or where one is middling and the rationale accounts
   for it, is fine — do not reject those.

8. K OVER WITH NO INNINGS TO GET THERE. A strikeout total is rate × length. If the card
   posts an outs line of roughly 15.5 or below and the pick is a K OVER, the rationale
   must say why he pitches deeper than the market expects. REJECT if it never engages
   with the short outing at all. Do not apply this where the card posts no outs line, and
   do not reject a K UNDER on these grounds — a short outs line supports an under.

═══════════════════════════════════════════════════════════════════
ACCEPT otherwise
═══════════════════════════════════════════════════════════════════

ACCEPT means the reasoning is coherent, the numbers are real and correctly attributed,
and the bet follows from the argument. You are NOT judging whether the bet will win, and
you are NOT judging whether you would have made it. A defensible pick you personally
disagree with is an ACCEPT. Reserve REJECT for genuine breakage.

Do not reject for style, brevity, or missing detail that does not change the conclusion.

A pick built primarily on a starter's history VS THIS OPPONENT or AT THIS PARK is a
legitimate read, not thin reasoning — that split is a top-weighted input, and a rationale
resting on a consistent multi-meeting pattern is an ACCEPT even where it cuts against the
starter's overall xERA or the lineup's wRC+. Two conditions still apply: the meeting count
must be stated and must match the card, and a case built on a SINGLE meeting is not a
consistent pattern — reject that one under check 2 as a number doing more work than it can
carry.

When rejecting, state the specific flaw in one or two sentences — quote the offending
phrase from the rationale so the prompt can be fixed later. On ACCEPT, no reason needed.
"""

_VERIFY_TOOL = {
    "name": "report_verdict",
    "description": "Report whether the proposed bet's reasoning holds up.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["ACCEPT", "REJECT"],
                "description": "ACCEPT if the reasoning is coherent and correctly attributed; REJECT if broken.",
            },
            "reason": {
                "type": ["string", "null"],
                "description": (
                    "REQUIRED when verdict is REJECT: the specific flaw in 1-2 sentences, "
                    "quoting the offending phrase. Omit or null when ACCEPT."
                ),
            },
        },
        "required": ["verdict"],
    },
}


def _verify_pick(client, pick: dict, game_block: str) -> tuple[bool, str]:
    """
    Audit one pick against the data card it came from. Returns (accepted, reject_reason).
    Fails OPEN — an API error keeps the pick rather than silently dropping it.
    """
    bet_line = " | ".join(
        f"{k}: {pick.get(k)}"
        for k in ("bet_type", "bet", "team_side", "line", "period", "odds", "confidence")
        if pick.get(k) not in (None, "")
    )
    user_msg = (
        f"DATA CARD THE ANALYST SAW:\n\n{game_block}\n\n"
        f"{'=' * 67}\n\nPROPOSED BET\n{bet_line}\n\n"
        f"ANALYST'S RATIONALE\n{pick.get('reason', '(none given)')}\n\n"
        f"{'=' * 67}\n\nAudit this pick and call report_verdict."
    )
    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=_VERIFY_SYSTEM_PROMPT,
            tools=[_VERIFY_TOOL],
            messages=[{"role": "user", "content": user_msg}],
        )
        record_claude("claude-opus-4-8", getattr(resp, "usage", None))
        block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if not block:
            # No structured verdict — fail open rather than discard a possibly-good pick.
            print("[verify] no verdict block returned — keeping pick", file=sys.stderr)
            return True, ""
        data = block.input or {}
        verdict = str(data.get("verdict", "")).strip().upper()
        if verdict == "REJECT":
            return False, (data.get("reason") or "").strip() or "(no reason given)"
        return True, ""
    except Exception as e:
        print(f"[verify] API error ({e}) — keeping pick", file=sys.stderr)
        return True, ""


def _log_rejections(rejections: list[dict], rej_dir: Path, date_str: str) -> None:
    """Append rejected picks to rejections/{date}.json for later prompt tuning."""
    if not rejections:
        return
    try:
        rej_dir.mkdir(parents=True, exist_ok=True)
        path = rej_dir / f"{date_str}.json"
        existing = []
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = []
        # Dedupe on the pick identity + the flaw, so repeated cron runs that regenerate
        # the same bad pick don't pile up duplicate rows.
        seen = {(r.get("game"), r.get("bet"), r.get("reject_reason")) for r in existing}
        added = [r for r in rejections
                 if (r.get("game"), r.get("bet"), r.get("reject_reason")) not in seen]
        if added:
            path.write_text(json.dumps(existing + added, indent=2))
            print(f"[verify] logged {len(added)} rejection(s) → {path}", file=sys.stderr)
    except Exception as e:
        print(f"[verify] could not write rejection log: {e}", file=sys.stderr)


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

    # extract_outings() returns NEWEST first, so the three most recent starts are the
    # leading slice — not the trailing one. Reversed below for chronological display.
    def _recent_3(outings):
        return [o for o in outings if not o.get("is_relief")][:3][::-1]

    def _recent_stats(outings):
        starts = [o for o in _recent_3(outings) if flt(o.get("ip"))]
        if not starts:
            return None, None
        total_ip = sum(flt(o["ip"]) or 0 for o in starts)
        total_er = sum(int(o["er"] or 0) for o in starts if o.get("er") is not None)
        k_vals = [o["k"] for o in starts if o.get("k") is not None]
        era_s = f"{total_er / total_ip * 9:.2f}" if total_ip > 0 else None
        avg_k  = f"{sum(k_vals) / len(k_vals):.1f}" if k_vals else None
        return era_s, avg_k

    def _outing_str(o):
        """One outing, in the format the recent-starts block uses. Shared so the
        head-to-head outings read identically to the last-3 lines above them."""
        date_s = o.get("date") or "?"
        ha     = "@" if o.get("ha") == "@" else "vs "
        opp_s  = f"{ha}{o['opp']}" if o.get("opp") and o["opp"] != "?" else "?"
        seg = [f"{o['ip']}IP"]
        for key, lbl in (("h", "H"), ("bb", "BB"), ("k", "K")):
            if o.get(key) is not None:
                seg.append(f"{o[key]}{lbl}")
        er, r = o.get("er"), o.get("r")
        if er is not None:
            # R shown only when unearned runs scored — signals defense, and means
            # the ER-based numbers are flattering relative to actual damage.
            seg.append(f"{er}ER" + (f" ({r}R)" if r is not None and r != er else ""))
        if o.get("pc"):
            seg.append(f"{o['pc']}pc")
        return f"{date_s} {opp_s}: " + " ".join(seg)

    def _sp_line(sp, outings, team, side):
        # The team is printed because the card is the only place it appears. Without it
        # the pitcher's club had to be inferred from list order, and a recently traded
        # starter reads as still being on his old team.
        name = f"{team} ({side}) — {sp['name']}"
        hand = (sp.get("hand") or "?")[0]
        if not sp.get("has_stats"):
            return f"  {name} ({hand}): NO STATS (first start this season)"
        parts = []
        if sp.get("label"):
            parts.append(f"xERA {sp['xera_s']} ({sp['label']})")
        else:
            parts.append(f"xERA {sp['xera_s']}")
        for key, lbl in [("k", "K%"), ("whiff", "Whiff%"), ("hard", "HH%"), ("bb", "BB%"), ("barrel", "Barrel%"), ("era_s", "ERA")]:
            val = sp.get(key)
            if val not in ("?", "—", None):
                parts.append(f"{lbl} {val}")
        if sp.get("depth") not in ("—", None):
            parts.append(sp["depth"])
        base = f"  {name} ({hand}): " + ", ".join(parts)
        recent = _recent_3(outings)
        if recent:
            outing_strs = [f"      {_outing_str(o)}" for o in recent]
            recent_era, avg_k = _recent_stats(outings)
            k_context = f", avg {avg_k} K/start" if avg_k else ""
            base += (
                "\n    Recent starts (oldest → newest):\n"
                + "\n".join(outing_strs)
                + f"\n      → {recent_era or '?'} ERA across these 3{k_context}"
            )
        return base

    def _off_line(team, off, vs_hand):
        # Every stat is labelled with its own window inline. The card carries a last-6 AND
        # a last-12 figure for wRC+, K%, Whiff%, and HH% now, and the model writes public
        # copy that must name the window it is quoting — an unlabelled pair is exactly the
        # kind of thing a second opinion over the same card cannot catch.
        if not off:
            return f"  {team} vs {vs_hand}HP: No data"
        lbl = f" ({off['label']})" if off.get("label") else ""
        parts = [f"wRC+ last 6: {off.get('wrc_s', '?')}{lbl}",
                 f"wRC+ last 12: {off.get('wrc_ctx_s', 'N/A')}"]
        # The longer window is only ever quoted alongside the primary one, so a stat
        # missing from the last 6 drops both halves rather than presenting a bare
        # last-12 figure the model would have no primary number to weigh it against.
        for stat, key in (("K%", "k"), ("Whiff%", "whiff"), ("HH%", "hard")):
            if off.get(key) in ("?", None):
                continue
            parts.append(f"{stat} last 6: {off[key]}")
            if off.get(f"{key}_ctx") not in ("?", None):
                parts.append(f"{stat} last 12: {off[f'{key}_ctx']}")
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
        prop_parts.append(f"{sp_a['name']} ({away}): {', '.join(p for p in [k_a, ou_a] if p)}")
    if k_h or ou_h:
        prop_parts.append(f"{sp_h['name']} ({home}): {', '.join(p for p in [k_h, ou_h] if p)}")
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
        base = f"  {name}: " + " | ".join(parts)

        # The individual meetings, not just their average. Head-to-head is weighted on
        # CONSISTENCY across starts, and an average is exactly the thing that hides it —
        # one blowup and two gems average out to the same line as three mediocre starts.
        vs_ot = list(reversed(spl.get("vs_outings") or []))
        at_ot = list(reversed(spl.get("at_outings") or []))
        vs_dates = {o["date"] for o in vs_ot}
        extra_at = [o for o in at_ot if o["date"] not in vs_dates]
        blocks = []
        if vs_ot:
            blocks.append(
                f"    vs {vs_label} — each meeting (oldest → newest):\n"
                + "\n".join(f"      {_outing_str(o)}" for o in vs_ot)
            )
        if extra_at:
            blocks.append(
                f"    at {venue_label} — further starts there (oldest → newest):\n"
                + "\n".join(f"      {_outing_str(o)}" for o in extra_at)
            )
        return base + ("\n" + "\n".join(blocks) if blocks else "")

    neutral_tag = " | NEUTRAL SITE" if g.get("neutral_site") else ""
    lines = [f"=== {away} @ {home}{time_s} | {venue} ({venue_tag}){neutral_tag} ==="]
    if g.get("neutral_site"):
        city = g.get("venue_city", "")
        lines.append(
            f"NEUTRAL SITE: this game is played at {venue}"
            + (f" in {city}" if city else "")
            + f", not at {home}'s home park. Ignore any home-park assumption; the "
              "park factor shown does not describe this venue."
        )
    lines.append(f"Weather: {wx_s}")
    lines.append(
        "STARTING PITCHERS — all rate stats are LAST 3 STARTS. The club shown is the one "
        "he is pitching FOR today; it is current as of this slate and overrides anything "
        "you believe about where he plays:"
    )
    lines.append(_sp_line(sp_a, outs_a, away, "away"))
    lines.append(_sp_line(sp_h, outs_h, home, "home"))
    lines.append(
        "OFFENSE — vs the opposing starter's hand. wRC+, K%, Whiff%, and HH% are each "
        "shown twice: LAST 6 GAMES (the primary read) and LAST 12 GAMES (the same lineup, "
        "same hand, longer window — context only, see Section 1 on how to weigh the gap "
        "between them):"
    )
    lines.append(_off_line(away, of_a, hand_h))
    lines.append(_off_line(home, of_h, hand_a))
    lines.append("BULLPENS — LAST 12 GAMES:")
    lines.append(_bp_line(away, bp_a))
    lines.append(_bp_line(home, bp_h))
    lines.append("STARTER vs TODAY'S OPPONENT — up to last 3 meetings:")
    lines.append(_spl_line(sp_a["name"], spl_a, home, "this park"))
    lines.append(_spl_line(sp_h["name"], spl_h, away, "home"))
    lines.append("ODDS:")
    lines.extend(odds_lines if odds_lines else ["  None available"])
    lines.append("TEAM TRENDS (in this starter's recent starts):")
    lines.append(_trend_line(away, tr_a))
    lines.append(_trend_line(home, tr_h))
    situational = [f[len("TREND: "):] for f in flags if f.startswith("TREND: ")]
    other_flags = [f for f in flags if not f.startswith("TREND: ")]
    if other_flags:
        lines.append("FLAGS:")
        lines.extend(f"  {f}" for f in other_flags)
    if situational:
        lines.append("SITUATIONAL TRENDS (see section 11 — weigh these, do not bury them):")
        lines.extend(f"  {f}" for f in situational)
    return "\n".join(lines)


# ── AI call + caching ─────────────────────────────────────────────────────────

def generate_suggestions(games: list[dict], data_dir: Path, target_date: date,
                         rej_dir: Path = Path("./rejections")) -> Optional[dict]:
    """
    Call Claude to generate betting suggestions. Caches to data/suggestions_{date}.json
    and regenerates whenever odds are updated. Returns parsed dict or None on failure.

    Every pick is audited by a second, independent model call before being returned;
    picks whose reasoning doesn't hold up are dropped and logged to rejections/{date}.json.
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

    # "Price is the whole job" — the prompt cannot produce a bet without a posted
    # number, so a slate where no game has odds yields an empty picks array at the
    # cost of a full Opus call. This happens for real: early on opening day the MLB
    # schedule is populated well before the books post, and any morning the Odds API
    # call fails leaves every card reading "ODDS: None available".
    def _has_odds(g: dict) -> bool:
        od = g.get("odds") or {}
        return any(od.get(k) not in (None, "", "—")
                   for k in ("away_ml", "over", "away_spread"))

    if not any(_has_odds(g) for g in unstarted):
        print(f"[suggestions] No odds posted for any of {len(unstarted)} game(s) — "
              f"skipping AI call", file=sys.stderr)
        return json.loads(sugg_path.read_text()) if sugg_path.exists() else None

    # Kept as a list as well as a joined blob — the verification pass re-sends the exact
    # card for whichever game a pick came from.
    serialized = [_serialize_game_for_ai(g) for g in unstarted]
    user_msg = (
        f"Today is {date_str}. Analyze these {len(unstarted)} MLB games and "
        f"identify any strong betting opportunities:\n\n" + "\n\n".join(serialized)
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
                            "reason":      {
                                "type": "string",
                                "description": (
                                    "Why this is a bet. Every stat MUST carry its time window "
                                    "(e.g. '3.05 xERA over his last 3', '112 wRC+ vs LHP over "
                                    "their last 6'; team offense is the last 6 games unless "
                                    "you are quoting the last-12 wRC+, and the window you "
                                    "write must match the one the card gave you). Must end by "
                                    "stating what the market is "
                                    "mispricing — a reason without that is not a bet. "
                                    "Public-facing copy: every sentence asserts something about "
                                    "THIS game. No references to these instructions, no reminders "
                                    "of methodology or baseball rules, no self-correction or "
                                    "process narration ('Calibrating:', 'Note:', 'that is "
                                    "irrelevant here'), no defending stats you chose not to use."
                                ),
                            },
                            "line_warning":   {"type": "boolean"},
                            "alt_suggestion": {"type": ["string", "null"]},
                        },
                        "required": ["game", "bet_type", "bet", "team_side", "line", "period", "odds", "confidence", "reason"],
                    },
                },
                "pass_reasons": {
                    "type": "object",
                    "description": "Key = game header exactly (e.g. 'TEX @ MIA'). Value = 1-sentence reason why no bet, written for a reader who has not seen these instructions — no rule or methodology references. Include every game not in picks.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["picks", "pass_reasons"],
        },
    }

    # Forcing tool_choice suppresses thinking entirely on Opus 4.8 (verified: a forced
    # call returns a bare tool_use block and no thinking). Since the whole point of Opus
    # here is the per-game reasoning, run with tool_choice auto and fall back to a forced
    # follow-up turn on the rare occasions it answers in prose instead.
    _common = dict(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=_AI_SYSTEM_PROMPT,
        tools=[_tool],
    )
    try:
        client = _ant.Anthropic(api_key=api_key)
        messages = [{"role": "user", "content": user_msg}]
        response = client.messages.create(
            tool_choice={"type": "auto"}, messages=messages, **_common
        )
        record_claude(_common["model"], getattr(response, "usage", None))
        tool_block = next((b for b in response.content if getattr(b, "type", "") == "tool_use"), None)
        if not tool_block:
            print("[suggestions] No tool_use block — retrying with forced tool", file=sys.stderr)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "Now submit those results via the report_betting_suggestions tool."},
            ]
            response = client.messages.create(
                tool_choice={"type": "tool", "name": "report_betting_suggestions"},
                messages=messages, **_common,
            )
            record_claude(_common["model"], getattr(response, "usage", None))
            tool_block = next((b for b in response.content if getattr(b, "type", "") == "tool_use"), None)
        if not tool_block:
            print("[suggestions] No tool_use block in response", file=sys.stderr)
            return None
        result = tool_block.input
    except Exception as e:
        print(f"[suggestions] API error: {e}", file=sys.stderr)
        return None

    # ── Verification pass: audit each pick against the card it came from ──────
    picks = result.get("picks") or []
    if picks:
        blocks_by_game = {f"{g['away']} @ {g['home']}": b
                          for g, b in zip(unstarted, serialized)}
        kept, rejections = [], []
        for pick in picks:
            game = pick.get("game", "")
            block = blocks_by_game.get(game)
            if not block:
                # Can't audit what we can't match — keep it rather than drop blind.
                print(f"[verify] no card for '{game}' — keeping unaudited", file=sys.stderr)
                kept.append(pick)
                continue
            ok, why = _verify_pick(client, pick, block)
            if ok:
                kept.append(pick)
            else:
                print(f"[verify] REJECT {game} | {pick.get('bet')} — {why}", file=sys.stderr)
                rejections.append({
                    "date":          date_str,
                    "game":          game,
                    "bet_type":      pick.get("bet_type", ""),
                    "bet":           pick.get("bet", ""),
                    "odds":          pick.get("odds", ""),
                    "confidence":    pick.get("confidence", ""),
                    "reason":        pick.get("reason", ""),
                    "reject_reason": why,
                    "rejected_at":   datetime.now(timezone.utc).isoformat(),
                })
        result["picks"] = kept
        _log_rejections(rejections, rej_dir, date_str)
        print(f"[verify] {len(kept)} kept, {len(rejections)} rejected of {len(picks)}",
              file=sys.stderr)

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
