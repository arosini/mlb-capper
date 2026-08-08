#!/usr/bin/env python3
"""Results page — graded pick record and unit P&L over rolling windows.

Reads the git-tracked pick log in picks/ (already annotated with won/lost/push
by `picks.py --annotate`) and renders _site/results/index.html.

Unit convention — the standard "risk 1 to win 1" flat bettor:
  * Plus money  (+130): risk 1u, win 1.30u  → +1.30 / -1.00
  * Minus money (-130): risk 1.30u to win 1u → +1.00 / -1.30
  * Push: 0.00, and it does not count toward the W-L record.

Usage:
  python3 results.py --html > _site/results/index.html
  python3 results.py            # terminal summary
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from render_html import _CSS, _SWIPE_SCRIPT, _h
from suggestions import _pick_summary_title

from season import ET as _ET

# The AI picks prompt was rewritten (and the per-pick verification pass added) in
# e38317a on 2026-08-07. Picks logged before that came from a materially different
# system, so counting them would misstate the current one's track record. The older
# files stay in picks/ on purpose — they are the input to scripts/review_rejections.py
# and to any future prompt evaluation. Move this date if the prompt is rewritten again.
TRACK_RECORD_START = date(2026, 8, 7)


# ── Unit math ────────────────────────────────────────────────────────────────

def unit_pnl(odds_num, result: str) -> float:
    """Profit/loss in units for one graded pick. Unpriced picks count as 0."""
    if result == "push" or odds_num in (None, 0):
        return 0.0
    try:
        odds = float(odds_num)
    except (TypeError, ValueError):
        return 0.0
    if result == "won":
        # Plus money pays odds/100 per unit risked; minus money is sized to win 1u.
        return odds / 100.0 if odds > 0 else 1.0
    if result == "lost":
        # Plus money risks exactly 1u; minus money risked |odds|/100 to win that 1u.
        return -1.0 if odds > 0 else odds / 100.0
    return 0.0


def summarize(picks: list) -> dict:
    """Aggregate a list of picks into record + units. Ungraded picks are skipped."""
    won = lost = push = 0
    units = 0.0
    for p in picks:
        r = p.get("result")
        if r == "won":
            won += 1
        elif r == "lost":
            lost += 1
        elif r == "push":
            push += 1
        else:
            continue  # not graded yet
        units += unit_pnl(p.get("odds_num"), r)
    graded = won + lost + push
    decided = won + lost
    return {
        "won": won, "lost": lost, "push": push,
        "graded": graded,
        "units": round(units, 2),
        "win_pct": (won / decided * 100.0) if decided else None,
        "roi": (units / graded * 100.0) if graded else None,
    }


# ── Loading ──────────────────────────────────────────────────────────────────

def load_picks_range(picks_dir: Path, start: date, end: date) -> list:
    """All picks dated in [start, end] inclusive.

    Globs the directory and filters on the filename date rather than walking the
    range a day at a time. The two are equivalent for a 7-day window, but the
    all-time window grows without bound — a day-walk would stat one missing path
    per off-season day, forever, and the count only ever rises.
    """
    lo, hi = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    out = []
    for path in sorted(picks_dir.glob("*.json")):
        if not (lo <= path.stem <= hi):
            continue
        try:
            out.extend(json.loads(path.read_text()) or [])
        except Exception as e:
            print(f"[results] skipping {path.name}: {e}", file=sys.stderr)
    return out


def build_windows(picks_dir: Path, today: date) -> dict:
    """Record + units for the trailing week, month, and all time (ending yesterday).

    Every window is floored at TRACK_RECORD_START, so a window that reaches back
    further than the current prompt simply covers fewer days rather than mixing in
    picks the present system did not make.

    "All time" means exactly that — it starts at TRACK_RECORD_START, not at Jan 1.
    A year-to-date floor looked identical for the whole of 2026 and then silently
    reset the headline record to 0-0 on New Year's Day, discarding every graded
    pick the current model had made.
    """
    end = today - timedelta(days=1)  # only completed days are graded

    def _win(start: date) -> dict:
        return summarize(load_picks_range(picks_dir, max(start, TRACK_RECORD_START), end))

    return {
        "week":  _win(end - timedelta(days=6)),
        "month": _win(end - timedelta(days=29)),
        "all":   _win(TRACK_RECORD_START),
    }


# ── Rendering ────────────────────────────────────────────────────────────────

_RESULTS_CSS = """
.res-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem;margin:.5rem 0 .75rem}
.res-card{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:.65rem .5rem;text-align:center}
.res-label{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#9ca3af}
.res-rec{font-size:1.05rem;font-weight:800;margin:.2rem 0 .05rem;font-variant-numeric:tabular-nums}
.res-units{font-size:.86rem;font-weight:700;font-variant-numeric:tabular-nums}
.res-sub{font-size:.62rem;color:#9ca3af;margin-top:.15rem;font-variant-numeric:tabular-nums}
.pos{color:#16a34a}.neg{color:#dc2626}.flat{color:#6b7280}
.res-day{background:white;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;margin-bottom:.5rem}
.res-day-hd{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#9ca3af;padding:.5rem .875rem .35rem;border-bottom:1px solid #f0f0f0;display:flex;justify-content:space-between;align-items:baseline;gap:.5rem}
.res-day-tot{font-size:.72rem;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:0;text-transform:none}
.res-row{display:flex;align-items:flex-start;gap:.55rem;padding:.5rem .875rem;border-bottom:1px solid #f6f6f6}
.res-row:last-child{border-bottom:none}
.res-mark{width:1.25rem;height:1.25rem;border-radius:50%;color:#fff;font-size:.72rem;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:.05rem;line-height:1}
.m-won{background:#16a34a}.m-lost{background:#dc2626}.m-push{background:#6b7280}
/* Pending reads as an empty outline, not a filled chip, so it can't be mistaken
   for a graded push at a glance. */
.m-pend{background:transparent;border:1.5px dashed #d1d5db;color:#9ca3af}
.res-body{flex:1;min-width:0}
.res-game{font-size:.68rem;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.04em}
.res-bet{font-size:.86rem;font-weight:700;margin:.05rem 0;line-height:1.3}
.res-meta{font-size:.7rem;color:#6b7280;font-variant-numeric:tabular-nums}
.res-pl{font-size:.8rem;font-weight:800;font-variant-numeric:tabular-nums;flex-shrink:0;margin-top:.15rem}
.res-note{font-size:.62rem;color:#9ca3af;text-align:center;padding:.5rem 1rem 0;line-height:1.5}
.res-cutoff{padding:0 1rem .6rem}
@media(prefers-color-scheme:dark){
.res-card,.res-day{background:#111827;border-color:#1f2937}
.res-row{border-bottom-color:#1f2937}
.res-day-hd{border-bottom-color:#1f2937}
.res-bet{color:#f3f4f6}
.m-pend{background:transparent;border-color:#374151;color:#6b7280}
}
"""


def _units_cls(u: float) -> str:
    return "pos" if u > 0.0001 else ("neg" if u < -0.0001 else "flat")


def _fmt_units(u: float) -> str:
    return f"{u:+.2f}u" if abs(u) > 0.0001 else "0.00u"


def _stat_card(label: str, s: dict) -> str:
    if not s["graded"]:
        return (f'<div class="res-card"><div class="res-label">{_h(label)}</div>'
                f'<div class="res-rec flat">—</div>'
                f'<div class="res-sub">no graded picks</div></div>')
    rec = f'{s["won"]}-{s["lost"]}' + (f'-{s["push"]}' if s["push"] else "")
    u   = s["units"]
    sub = f'{s["win_pct"]:.0f}% · {s["roi"]:+.1f}% ROI' if s["win_pct"] is not None else ""
    return (f'<div class="res-card"><div class="res-label">{_h(label)}</div>'
            f'<div class="res-rec">{_h(rec)}</div>'
            f'<div class="res-units {_units_cls(u)}">{_h(_fmt_units(u))}</div>'
            f'<div class="res-sub">{_h(sub)}</div></div>')


_MARK = {"won": ("m-won", "✓"), "lost": ("m-lost", "✗"),
         "push": ("m-push", "P"), None: ("m-pend", "·")}


def _pick_row(p: dict) -> str:
    result = p.get("result")
    cls, glyph = _MARK.get(result, _MARK[None])
    # _pick_summary_title() already carries the price, so meta only adds confidence.
    title = _pick_summary_title(p)
    meta  = (p.get("confidence") or "").title()

    # Spell out the non-win/loss states. A grey circle alone reads as a push when the
    # pick is really still ungraded, which is a materially different thing.
    if result == "push":
        meta = " · ".join(x for x in ("Push", meta) if x)
    elif result is None:
        meta = " · ".join(x for x in ("Pending", meta) if x)

    if result in ("won", "lost", "push"):
        u = unit_pnl(p.get("odds_num"), result)
        pl = f'<div class="res-pl {_units_cls(u)}">{_h(_fmt_units(u))}</div>'
    else:
        pl = '<div class="res-pl flat">—</div>'

    return (f'<div class="res-row">'
            f'<div class="res-mark {cls}">{glyph}</div>'
            f'<div class="res-body">'
            f'<div class="res-game">{_h(p.get("game", ""))}</div>'
            f'<div class="res-bet">{_h(title)}</div>'
            f'<div class="res-meta">{_h(meta)}</div>'
            f'</div>{pl}</div>')


def _day_section(label: str, picks: list) -> str:
    if not picks:
        return (f'<div class="res-day"><div class="res-day-hd">{_h(label)}</div>'
                f'<div class="res-row"><div class="res-body">'
                f'<div class="res-meta">No picks logged.</div></div></div></div>')
    s = summarize(picks)
    rec = f'{s["won"]}-{s["lost"]}' + (f'-{s["push"]}' if s["push"] else "")
    tot = (f'<span class="res-day-tot {_units_cls(s["units"])}">'
           f'{_h(rec)} · {_h(_fmt_units(s["units"]))}</span>') if s["graded"] else ""
    rows = "".join(_pick_row(p) for p in picks)
    return (f'<div class="res-day"><div class="res-day-hd"><span>{_h(label)}</span>'
            f'{tot}</div>{rows}</div>')


def render_results_page(picks_dir: Path, today: date, generated_at: str = "") -> str:
    yesterday = today - timedelta(days=1)
    y_picks   = load_picks_range(picks_dir, yesterday, yesterday)
    windows   = build_windows(picks_dir, today)

    cards = "".join([
        _stat_card("Last 7d",  windows["week"]),
        _stat_card("Last 30d", windows["month"]),
        _stat_card("All Time", windows["all"]),
    ])
    # Say so plainly rather than letting "Last 30d" imply 30 days of picks when the
    # track record is younger than the window.
    start_s = TRACK_RECORD_START.strftime(f"%b {TRACK_RECORD_START.day}, %Y")
    cutoff_note = (f'<p class="res-note res-cutoff">Track record begins '
                   f'{_h(start_s)}, when the current picks model went live. '
                   f'Earlier picks came from a different prompt and are excluded.</p>')
    y_label = yesterday.strftime(f"Yesterday · %a, %b {yesterday.day}")
    gen = f'<p class="sub">Updated {_h(generated_at)}</p>' if generated_at else ""

    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>MLB Results · {_h(today.strftime(f"%b {today.day}"))}</title>\n'
        f'<style>{_CSS}{_RESULTS_CSS}</style>\n'
        f'</head>\n<body data-slot="results">\n'
        f'<header><h1>MLB Game Overviews</h1>'
        f'<p class="sub">Pick Results</p>{gen}'
        f'<nav class="day-toggle">'
        f'<a href="/results/" class="active">Results</a>'
        f'<a href="/">Today</a>'
        f'<a href="/tomorrow/">Tomorrow</a>'
        f'</nav></header>\n'
        f'<main>'
        f'<div class="res-grid">{cards}</div>'
        f'{cutoff_note}'
        f'{_day_section(y_label, y_picks)}'
        f'<p class="res-note">Flat 1-unit staking: plus money risks 1u, '
        f'minus money risks to win 1u. Pushes count as 0 and are excluded from '
        f'the win rate. Windows end yesterday — today\'s picks are still open.</p>'
        f'</main>'
        f'<footer style="text-align:center;padding:1.5rem 1rem;font-size:.75rem;color:#9ca3af">'
        f'Powered by <a href="https://handigraphs.com" target="_blank" rel="noopener" '
        f'style="color:#9ca3af">Handigraphs</a>'
        f'</footer>{_SWIPE_SCRIPT}\n</body>\n</html>'
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="MLB pick results and unit P&L")
    ap.add_argument("--html", action="store_true", help="Emit the results page to stdout")
    ap.add_argument("--picks-dir", default="./picks", help="Picks log directory")
    ap.add_argument("--date", default="today", help="today or YYYY-MM-DD")
    args = ap.parse_args()

    if args.date == "today":
        today = datetime.now(_ET).date()
    else:
        try:
            today = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit(f"ERROR: Invalid date '{args.date}'. Use today or YYYY-MM-DD.")

    picks_dir = Path(args.picks_dir)

    if args.html:
        gen = datetime.now(_ET).strftime("%b %-d, %-I:%M %p ET")
        print(render_results_page(picks_dir, today, gen))
        return

    w = build_windows(picks_dir, today)
    print(f"track record from {TRACK_RECORD_START}")
    for label, key in (("Last 7d", "week"), ("Last 30d", "month"), ("All Time", "all")):
        s = w[key]
        if not s["graded"]:
            print(f"{label:>9}: no graded picks")
            continue
        rec = f'{s["won"]}-{s["lost"]}' + (f'-{s["push"]}' if s["push"] else "")
        print(f'{label:>9}: {rec:>10}  {s["units"]:+7.2f}u  '
              f'{s["win_pct"]:5.1f}%  ROI {s["roi"]:+.1f}%')


if __name__ == "__main__":
    main()
