"""Usage ledger — what we spend on the paid APIs, recorded as we spend it.

Two very different things are being tracked, and the page must not blur them:

**The Odds API has a real quota.** Every response carries `x-requests-remaining` and
`x-requests-used`, so "remaining" is a fact we read off the wire, not an estimate.
Billing is per *market x region*, which is why the credit count moves ~7x faster than
the call count on per-event props requests.

**The Anthropic API has no quota to read.** It is pay-as-you-go, and the Usage & Cost
API needs an Admin API key (`sk-ant-admin...`) or an `org:admin` OAuth token — neither
of which is the `ANTHROPIC_API_KEY` this project uses, and the Admin API is unavailable
to individual accounts entirely. So spend is *self-metered*: every response carries a
`usage` block, and we price it against the published per-model rates. That is exact for
our own calls (it cannot see spend from anything else on the same key), and "remaining"
only means anything against a budget the operator chooses — `CLAUDE_MONTHLY_BUDGET_USD`.

The ledger is git-tracked under `usage/{YYYY-MM}.json` for the same reason `history/`
and `picks/` are: `data/` is wiped between runs, so anything left there is forgotten by
the next checkout. One small file per month.
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from season import ET

# USD per million tokens. Keep in step with the model actually used in suggestions.py.
# Cache reads bill at ~0.1x input and cache writes at ~1.25x, per the pricing docs.
PRICING = {
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-opus-5":   {"input": 5.00, "output": 25.00},
    # $2/$10 is Sonnet 5. The 3.00/15.00 that sat here until 2026-09-03 is the
    # Sonnet 4.6 rate, and it would have overstated the ledger by 50% from the day
    # suggestions.py started calling Sonnet 5.
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
_CACHE_READ_MULT  = 0.10
_CACHE_WRITE_MULT = 1.25

DEFAULT_MONTHLY_BUDGET_USD = 150.0


def monthly_budget_usd() -> float:
    """Operator-chosen ceiling for Claude spend. Anthropic enforces no such limit by
    default, so this is a yardstick for the page, not a quota."""
    try:
        return float(os.environ.get("CLAUDE_MONTHLY_BUDGET_USD", "")
                     or DEFAULT_MONTHLY_BUDGET_USD)
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET_USD


def cost_usd(model: str, inp: int, out: int, cache_read: int = 0,
             cache_write: int = 0) -> float:
    """Price one response. Unknown models fall back to Opus rates rather than 0, so a
    model swap shows up as roughly-right spend instead of silently free."""
    p = PRICING.get(model) or PRICING["claude-opus-4-8"]
    return (
        inp         / 1e6 * p["input"]
        + out       / 1e6 * p["output"]
        + cache_read  / 1e6 * p["input"] * _CACHE_READ_MULT
        + cache_write / 1e6 * p["input"] * _CACHE_WRITE_MULT
    )


def _path(usage_dir: Path, day: date) -> Path:
    return usage_dir / f"{day.strftime('%Y-%m')}.json"


def _load(usage_dir: Path, day: date) -> dict:
    p = _path(usage_dir, day)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"month": day.strftime("%Y-%m"), "days": {}}


def _save(usage_dir: Path, day: date, doc: dict) -> None:
    try:
        usage_dir.mkdir(parents=True, exist_ok=True)
        _path(usage_dir, day).write_text(json.dumps(doc, indent=2, sort_keys=True))
    except Exception as e:
        print(f"[usage] could not write ledger: {e}", file=sys.stderr)


def _today_entry(doc: dict, day: date) -> dict:
    return doc["days"].setdefault(day.isoformat(), {})


def record_claude(model: str, usage, usage_dir: Path = Path("./usage"),
                  day: Optional[date] = None) -> None:
    """Accumulate one Anthropic response into today's ledger row.

    Accepts the SDK usage object or a plain dict. Never raises — metering must not be
    able to take down a publish run.
    """
    try:
        day = day or datetime.now(ET).date()

        def _g(name):
            if usage is None:
                return 0
            v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
            return int(v or 0)

        inp, out = _g("input_tokens"), _g("output_tokens")
        cr, cw = _g("cache_read_input_tokens"), _g("cache_creation_input_tokens")

        doc = _load(usage_dir, day)
        row = _today_entry(doc, day).setdefault(
            "claude", {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                       "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0})
        row["calls"] += 1
        row["input_tokens"]      += inp
        row["output_tokens"]     += out
        row["cache_read_tokens"] += cr
        row["cache_write_tokens"] += cw
        row["cost_usd"] = round(row["cost_usd"] + cost_usd(model, inp, out, cr, cw), 6)
        row.setdefault("models", {})
        row["models"][model] = row["models"].get(model, 0) + 1
        _save(usage_dir, day, doc)
    except Exception as e:
        print(f"[usage] claude metering skipped: {e}", file=sys.stderr)


def record_odds(remaining, used, usage_dir: Path = Path("./usage"),
                day: Optional[date] = None) -> None:
    """Record an Odds API quota observation.

    `remaining` is authoritative and monotonically falls within a billing period, so the
    LOWEST value seen in a day is the end-of-day truth; a later higher value means the
    quota reset. Keeping both the low-water mark and the last reading lets the page tell
    a reset apart from a stall.
    """
    try:
        if remaining is None and used is None:
            return
        day = day or datetime.now(ET).date()
        doc = _load(usage_dir, day)
        row = _today_entry(doc, day).setdefault("odds", {})
        now = datetime.now(timezone.utc).isoformat()
        if remaining is not None:
            remaining = int(remaining)
            row["remaining_last"] = remaining
            row["remaining_min"] = (remaining if "remaining_min" not in row
                                    else min(row["remaining_min"], remaining))
            row.setdefault("remaining_first", remaining)
        if used is not None:
            used = int(used)
            row["used_last"] = used
            row.setdefault("used_first", used)
        row["observed_at"] = now
        _save(usage_dir, day, doc)
    except Exception as e:
        print(f"[usage] odds metering skipped: {e}", file=sys.stderr)


def anthropic_cost_report(start: date, end: date) -> Optional[dict]:
    """Authoritative month-to-date spend from Anthropic's Cost API, or None.

    Returns None — and the caller falls back to the self-metered ledger — unless
    ANTHROPIC_ADMIN_KEY is set. This is a *different* credential from the
    ANTHROPIC_API_KEY used to generate picks: an Admin key (`sk-ant-admin01-...`)
    minted in the Console, and the Admin API is unavailable to individual accounts
    (Console -> Settings -> Organization sets one up).

    Worth the extra credential because the ledger can only see calls this project
    makes. The Cost API is billing truth: it also captures Workbench usage, anything
    else sharing the key, server-tool costs (web search, code execution), and any
    model whose rate we have wrong in PRICING.

    It reports spend only. There is no budget or remaining-balance endpoint on this
    path — spend limits are a Claude Enterprise feature — so "remaining" is still
    measured against CLAUDE_MONTHLY_BUDGET_USD either way.
    """
    key = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
    if not key:
        return None
    try:
        import requests
        r = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            params={"starting_at": f"{start.isoformat()}T00:00:00Z",
                    "ending_at":   f"{end.isoformat()}T00:00:00Z"},
            headers={"anthropic-version": "2023-06-01", "x-api-key": key},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        # Never let a reporting call break the publish run.
        print(f"[usage] cost_report unavailable ({e}) — using local ledger",
              file=sys.stderr)
        return None

    # Costs come back as decimal strings in the currency's lowest unit (cents).
    # Sum every amount across buckets/results rather than assuming a shape, so a
    # response layout change degrades to a wrong-ish total rather than a crash.
    total_cents = 0.0
    def _walk(node):
        nonlocal total_cents
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("amount", "cost", "value") and isinstance(v, (str, int, float)):
                    try:
                        total_cents += float(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
    _walk(body)
    return {"cost_usd": round(total_cents / 100.0, 2), "source": "anthropic cost_report"}


def load_days(usage_dir: Path, start: date, end: date) -> dict:
    """{date_iso: row} across [start, end], reading only the months in range."""
    out: dict = {}
    months = set()
    d = start
    while d <= end:
        months.add(d.strftime("%Y-%m"))
        d = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    months.add(end.strftime("%Y-%m"))
    for m in sorted(months):
        p = usage_dir / f"{m}.json"
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text())
        except Exception:
            continue
        for iso, row in (doc.get("days") or {}).items():
            if start.isoformat() <= iso <= end.isoformat():
                out[iso] = row
    return out
