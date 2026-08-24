#!/usr/bin/env python3
"""Budget page — what the paid APIs have cost, and what is left.

Usage:
  python3 budget.py --html > _site/budget/index.html
  python3 budget.py                      # terminal summary

The two APIs are reported differently on purpose, because only one of them has a
quota to report:

* **The Odds API** returns `x-requests-remaining` on every response, so "remaining" is
  measured, not modelled. Credits are billed per market x region, which is why the
  credit count falls ~7x faster than the call count on per-event props requests.
* **The Anthropic API** has no readable balance in either mode. Spend is self-metered
  from each response's `usage` block by default, which counts this project's calls only.
  Setting ANTHROPIC_ADMIN_KEY switches the headline figure to Anthropic's own
  `cost_report` — billing truth, org-wide — but that needs an Admin key
  (`sk-ant-admin01-...`) and an organization rather than an individual account.
  Either way "remaining" is measured against CLAUDE_MONTHLY_BUDGET_USD: Anthropic
  exposes no budget or remaining-limit endpoint on this path (spend limits are a
  Claude Enterprise feature).
"""
from datetime import date, datetime, timedelta
from pathlib import Path

from render_html import _CSS, _h
from season import ET
from usage import load_days, monthly_budget_usd, anthropic_cost_report


def _month_bounds(today: date) -> tuple:
    start = today.replace(day=1)
    nxt = date(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return start, nxt - timedelta(days=1)


def collect(usage_dir: Path, today: date) -> dict:
    m_start, m_end = _month_bounds(today)
    month = load_days(usage_dir, m_start, m_end)
    week  = load_days(usage_dir, today - timedelta(days=6), today)

    def _claude(rows):
        c = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        for r in rows.values():
            cl = r.get("claude") or {}
            for k in c:
                c[k] += cl.get(k, 0) or 0
        c["cost_usd"] = round(c["cost_usd"], 2)
        return c

    # Odds credits consumed per day = the day's first reading minus its low-water mark.
    # Using first-minus-min rather than the raw `used` counter keeps a mid-month billing
    # reset from showing up as a single enormous negative day.
    def _odds_spend(rows):
        total, days = 0, 0
        for r in rows.values():
            o = r.get("odds") or {}
            f, lo = o.get("remaining_first"), o.get("remaining_min")
            if f is not None and lo is not None and f >= lo:
                total += f - lo
                days += 1
        return total, days

    latest = None
    for iso in sorted(month, reverse=True):
        if (month[iso].get("odds") or {}).get("remaining_last") is not None:
            latest = month[iso]["odds"]
            break

    m_spend, m_days = _odds_spend(month)
    w_spend, w_days = _odds_spend(week)
    burn = (w_spend / w_days) if w_days else (m_spend / m_days if m_days else 0)

    remaining = latest.get("remaining_last") if latest else None
    days_left_in_month = (m_end - today).days + 1
    return {
        "today": today,
        "month_start": m_start, "month_end": m_end,
        "days_left": days_left_in_month,
        "odds": {
            "remaining": remaining,
            "observed_at": (latest or {}).get("observed_at"),
            "month_spend": m_spend, "month_days": m_days,
            "burn_per_day": round(burn, 1),
            "projected_month_end": (round(remaining - burn * days_left_in_month)
                                    if remaining is not None else None),
            "days_to_zero": (round(remaining / burn, 1)
                             if remaining is not None and burn > 0 else None),
        },
        "claude": {
            "month": _claude(month), "week": _claude(week),
            "budget": monthly_budget_usd(),
            # Authoritative billing figure when an Admin key is configured; the
            # self-metered ledger otherwise. Only the headline number changes —
            # token counts and call counts stay local either way.
            "authoritative": anthropic_cost_report(m_start, today),
        },
    }


# ── Rendering ────────────────────────────────────────────────────────────────

_BUDGET_CSS = """
.bg-card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:.9rem 1rem;margin-bottom:.7rem}
.bg-hd{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#9ca3af;margin-bottom:.45rem}
.bg-big{font-size:1.5rem;font-weight:800;font-variant-numeric:tabular-nums;line-height:1.15}
.bg-sub{font-size:.72rem;color:#6b7280;margin-top:.15rem;font-variant-numeric:tabular-nums}
.bg-row{display:flex;justify-content:space-between;gap:.6rem;font-size:.78rem;padding:.28rem 0;border-bottom:1px solid #f4f4f5;font-variant-numeric:tabular-nums}
.bg-row:last-child{border-bottom:none}
.bg-row span:first-child{color:#6b7280}
.bg-bar{height:7px;border-radius:4px;background:#e5e7eb;overflow:hidden;margin:.5rem 0 .3rem}
.bg-bar>i{display:block;height:100%;border-radius:4px}
.ok{color:#16a34a}.warn{color:#d97706}.bad{color:#dc2626}.muted{color:#9ca3af}
.bg-bar>i.ok{background:#16a34a}.bg-bar>i.warn{background:#d97706}.bg-bar>i.bad{background:#dc2626}
.bg-note{font-size:.66rem;color:#9ca3af;line-height:1.55;margin-top:.4rem}
@media(prefers-color-scheme:dark){
.bg-card{background:#111827;border-color:#1f2937}
.bg-row{border-bottom-color:#1f2937}
.bg-bar{background:#1f2937}
}
"""


def _cls(frac: float) -> str:
    return "bad" if frac >= 0.9 else ("warn" if frac >= 0.75 else "ok")


def _bar(frac: float) -> str:
    pct = max(0.0, min(1.0, frac)) * 100
    return f'<div class="bg-bar"><i class="{_cls(frac)}" style="width:{pct:.1f}%"></i></div>'


def render_budget_page(usage_dir: Path, today: date, generated_at: str = "") -> str:
    d = collect(usage_dir, today)
    o, c = d["odds"], d["claude"]

    # ── Odds card ──
    if o["remaining"] is None:
        odds_body = ('<div class="bg-big muted">—</div>'
                     '<div class="bg-sub">No quota reading recorded yet. The next '
                     'scheduled run writes one.</div>')
    else:
        # Plan size is inferred: the highest remaining+spend seen this month is a floor.
        est_plan = o["remaining"] + o["month_spend"]
        frac = 1 - (o["remaining"] / est_plan) if est_plan else 0
        proj = o["projected_month_end"]
        proj_cls = "bad" if (proj is not None and proj < 0) else "ok"
        proj_s = (f'<span class="{proj_cls}">{proj:,}</span>'
                  if proj is not None else "—")
        dtz = o["days_to_zero"]
        dtz_s = f"{dtz:.0f} days" if dtz else "—"
        odds_body = (
            f'<div class="bg-big">{o["remaining"]:,}<span class="bg-sub"> credits left</span></div>'
            f'{_bar(frac)}'
            f'<div class="bg-row"><span>Burn rate (7d avg)</span><span>{o["burn_per_day"]:,.0f}/day</span></div>'
            f'<div class="bg-row"><span>Spent this month</span><span>{o["month_spend"]:,} over {o["month_days"]}d</span></div>'
            f'<div class="bg-row"><span>Projected at month end ({d["days_left"]}d left)</span><span>{proj_s}</span></div>'
            f'<div class="bg-row"><span>Runs out in</span><span>{dtz_s}</span></div>'
        )
    odds_note = (
        'Billed per <em>market x region</em>, not per call — a per-event props request '
        'covers 7 markets and costs 7 credits. Remaining is read from the API\'s own '
        '<code>x-requests-remaining</code> header; the plan size is inferred from '
        'remaining + spend, so it is a floor rather than a confirmed figure.'
    )

    # ── Claude card ──
    auth = c.get("authoritative")
    spend = auth["cost_usd"] if auth else c["month"]["cost_usd"]
    budget = c["budget"]
    frac = (spend / budget) if budget else 0
    left = budget - spend
    claude_body = (
        f'<div class="bg-big">${spend:,.2f}<span class="bg-sub"> this month'
        f'{" · billed" if auth else " · estimated"}</span></div>'
        f'{_bar(frac)}'
        f'<div class="bg-row"><span>Budget</span><span>${budget:,.2f}</span></div>'
        f'<div class="bg-row"><span>Remaining</span>'
        f'<span class="{_cls(frac)}">${left:,.2f}</span></div>'
        f'<div class="bg-row"><span>Last 7 days</span><span>${c["week"]["cost_usd"]:,.2f}</span></div>'
        f'<div class="bg-row"><span>Calls this month</span><span>{c["month"]["calls"]:,}</span></div>'
        f'<div class="bg-row"><span>Tokens in / out</span>'
        f'<span>{c["month"]["input_tokens"]:,} / {c["month"]["output_tokens"]:,}</span></div>'
    )
    if auth:
        claude_note = (
            "Spend is Anthropic's own <code>cost_report</code> figure — billing truth, "
            "covering everything on the organization including usage outside this "
            "project. There is still no balance to read: Anthropic exposes no budget or "
            "remaining-limit endpoint on this path, so the ceiling below is "
            "<code>CLAUDE_MONTHLY_BUDGET_USD</code>, chosen locally."
        )
    else:
        claude_note = (
            "Self-metered from each response's <code>usage</code> block at published "
            "rates, so it counts this project's calls only. Set "
            "<code>ANTHROPIC_ADMIN_KEY</code> to use Anthropic's <code>cost_report</code> "
            "instead, which is billing truth — it needs an Admin key "
            "(<code>sk-ant-admin01-…</code>), and the Admin API requires an organization "
            "rather than an individual account. Either way the ceiling is "
            "<code>CLAUDE_MONTHLY_BUDGET_USD</code>, chosen locally: Anthropic exposes no "
            "remaining-budget endpoint."
        )

    gen = f'<p class="sub">Updated {_h(generated_at)}</p>' if generated_at else ""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f'<title>API Budget · {_h(today.strftime(f"%b {today.day}"))}</title>\n'
        f'<style>{_CSS}{_BUDGET_CSS}</style>\n</head>\n<body data-slot="budget">\n'
        '<header><h1>API Budget</h1>'
        f'<p class="sub">{_h(d["month_start"].strftime("%B %Y"))}</p>{gen}'
        '<nav class="day-toggle"><a href="/results/">Results</a>'
        '<a href="/">Today</a><a href="/tomorrow/">Tomorrow</a></nav></header>\n'
        '<main>'
        f'<div class="bg-card"><div class="bg-hd">The Odds API</div>{odds_body}'
        f'<div class="bg-note">{odds_note}</div></div>'
        f'<div class="bg-card"><div class="bg-hd">Anthropic API</div>{claude_body}'
        f'<div class="bg-note">{claude_note}</div></div>'
        '</main>'
        '<footer style="text-align:center;padding:1.5rem 1rem;font-size:.75rem;color:#9ca3af">'
        '<a href="/" style="color:#9ca3af">&larr; Back to today</a></footer>'
        '\n</body>\n</html>'
    )


def main():
    import argparse
    ap = argparse.ArgumentParser(description="API budget page")
    ap.add_argument("--html", action="store_true", help="Emit the page to stdout")
    ap.add_argument("--usage-dir", default="./usage")
    ap.add_argument("--date", default="today")
    args = ap.parse_args()

    today = (datetime.now(ET).date() if args.date == "today"
             else datetime.strptime(args.date, "%Y-%m-%d").date())
    usage_dir = Path(args.usage_dir)

    if args.html:
        gen = datetime.now(ET).strftime("%b %-d, %-I:%M %p ET")
        print(render_budget_page(usage_dir, today, gen))
        return

    d = collect(usage_dir, today)
    o, c = d["odds"], d["claude"]
    print(f"Odds API : {o['remaining'] if o['remaining'] is not None else '—'} left, "
          f"burn {o['burn_per_day']}/day, "
          f"projected month-end {o['projected_month_end']}")
    print(f"Anthropic: ${c['month']['cost_usd']:.2f} of ${c['budget']:.2f} this month "
          f"({c['month']['calls']} calls)")


if __name__ == "__main__":
    main()
