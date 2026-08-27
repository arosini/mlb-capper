#!/usr/bin/env python3
"""
Measure where the AI data card's tokens go, block by block.

The card is the dominant input-token term in this project's Anthropic bill: it is sent
once per game in the generation call, and then AGAIN in full for every pick the audit
pass checks. CLAUDE.md's API Budget section models it at ~550 tokens; that figure was
written before the adversarial review added weather detail, bullpen rates, ou_trends,
head-to-head, no-vig probabilities, posted lineups and the alternate ladders, and it
has never been re-measured.

This does not guess. Feed it a dump of real cards:

    python3 handicap.py --dump-cards /tmp/cards.json
    python3 scripts/measure_card.py /tmp/cards.json

Token counts come from the Anthropic token-counting endpoint when ANTHROPIC_API_KEY is
set (that endpoint is free, and it is the only way to get a true count — a chars/4 rule
is off by enough on dense numeric text to rank the blocks wrongly). Without a key it
falls back to chars/4 and says so in the output.

Exit codes:
  0  report written
  1  error
"""
import sys as _sys, pathlib as _pathlib

# Drop scripts/ from sys.path before importing anything else — see the same guard in
# review_rejections.py. A module in here whose name matches a stdlib one shadows it for
# every later import, including ones made inside third-party packages.
_HERE = str(_pathlib.Path(__file__).resolve().parent)
_sys.path[:] = [p for p in _sys.path if p not in ("", ".", _HERE)]
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys

MODEL = "claude-opus-5"

# Ordered: the first pattern whose test matches a non-indented line opens that block.
# Indented lines and blank lines continue whichever block is open. The order matters
# only in that every non-indented line must match exactly one entry.
_SECTIONS = (
    ("header",      lambda l: l.startswith("=== ")),
    ("neutral",     lambda l: l.startswith("NEUTRAL SITE:")),
    ("weather",     lambda l: l.startswith("Weather:")),
    ("pitchers",    lambda l: l.startswith("STARTING PITCHERS")),
    ("offense",     lambda l: l.startswith("OFFENSE")),
    ("bullpens",    lambda l: l.startswith("BULLPENS")),
    ("h2h",         lambda l: l.startswith("STARTER vs TODAY'S OPPONENT")),
    ("odds",        lambda l: l.startswith("ODDS:")),
    ("trends",      lambda l: l.startswith("TEAM TRENDS")),
    ("ou_history",  lambda l: l.startswith("OVER/UNDER HISTORY")),
    ("lineup",      lambda l: l.startswith("POSTED LINEUP")),
    ("season",      lambda l: l.startswith("SEASON SERIES:")),
    ("flags",       lambda l: l.startswith("FLAGS:")),
    ("situational", lambda l: l.startswith("SITUATIONAL TRENDS")),
)


def split_blocks(card: str) -> dict[str, list[str]]:
    """Split one card into {block_name: [lines]}.

    Every line lands in exactly one block, so the block totals reconstruct the card
    without loss — verify_split() below asserts that rather than trusting it.
    """
    out: dict[str, list[str]] = {}
    current = "header"
    for line in card.split("\n"):
        if line[:1] not in (" ", "") :
            for name, test in _SECTIONS:
                if test(line):
                    current = name
                    break
        out.setdefault(current, []).append(line)
    return out


def split_alt_lines(odds_lines: list[str]) -> tuple[list[str], list[str]]:
    """Separate the alternate-line ladders from the rest of the ODDS block.

    The ladders are the newest and least-proven addition to the card, so they get
    their own row in the report rather than hiding inside the odds total.
    """
    main, alts, in_alts = [], [], False
    for line in odds_lines:
        if line.strip().startswith("ALT LINES"):
            in_alts = True
            alts.append(line)
        elif in_alts and line.startswith("    "):
            alts.append(line)
        else:
            in_alts = False
            main.append(line)
    return main, alts


def verify_split(card: str, blocks: dict[str, list[str]]) -> None:
    """A dropped line would silently understate a block. Fail loudly instead."""
    rebuilt = sum(len(v) for v in blocks.values())
    actual = len(card.split("\n"))
    if rebuilt != actual:
        raise SystemExit(f"block split lost lines: {rebuilt} != {actual}")


class Counter:
    """Exact token counts via the API when a key is present, chars/4 otherwise."""

    def __init__(self) -> None:
        self.exact = False
        self._client = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                self._client = anthropic.Anthropic()
                self.exact = True
            except Exception as e:  # noqa: BLE001 - fall back, never fail the report
                print(f"[measure] token API unavailable ({e}); using chars/4",
                      file=sys.stderr)

    def count(self, text: str) -> int:
        if not text.strip():
            return 0
        if self._client is not None:
            try:
                return self._client.messages.count_tokens(
                    model=MODEL, messages=[{"role": "user", "content": text}]
                ).input_tokens
            except Exception as e:  # noqa: BLE001
                print(f"[measure] count_tokens failed ({e}); using chars/4",
                      file=sys.stderr)
                self._client = None
        return len(text) // 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", help="JSON written by `handicap.py --dump-cards`")
    ap.add_argument("--json-out", metavar="PATH",
                    help="Also write the per-block totals as JSON")
    args = ap.parse_args()

    cards = json.loads(_pathlib.Path(args.dump).read_text())
    if not cards:
        print("[measure] no cards in dump — nothing to measure", file=sys.stderr)
        return 1

    # Aggregate each block across the whole slate, then count once per block. Counting
    # per card would be n_games x n_blocks API calls for the same answer.
    pooled: dict[str, list[str]] = {}
    for entry in cards:
        blocks = split_blocks(entry["card"])
        verify_split(entry["card"], blocks)
        if "odds" in blocks:
            main, alts = split_alt_lines(blocks.pop("odds"))
            blocks["odds"] = main
            if alts:
                blocks["odds_alt_ladders"] = alts
        for name, lines in blocks.items():
            pooled.setdefault(name, []).extend(lines)

    counter = Counter()
    n = len(cards)
    rows = []
    for name, lines in pooled.items():
        text = "\n".join(lines)
        rows.append((name, counter.count(text), len(text), len(lines)))
    rows.sort(key=lambda r: -r[1])
    total = sum(r[1] for r in rows)

    whole = counter.count("\n\n".join(c["card"] for c in cards))
    mode = "exact (count_tokens)" if counter.exact else "ESTIMATED (chars/4, no API key)"

    print(f"\nAI data card token profile — {n} games, {mode}\n")
    print(f"{'block':22} {'tok':>8} {'tok/game':>9} {'% card':>7} {'lines/g':>8}")
    print("-" * 60)
    for name, tok, _chars, lines in rows:
        print(f"{name:22} {tok:8,} {tok/n:9,.0f} {tok/total*100:6.1f}% {lines/n:8.1f}")
    print("-" * 60)
    print(f"{'TOTAL (blocks)':22} {total:8,} {total/n:9,.0f}")
    print(f"{'TOTAL (whole slate)':22} {whole:8,} {whole/n:9,.0f}   <- what generation sends\n")

    # The card is paid for twice: once per game in generation, once per PICK in the
    # audit pass. The audit multiplier is what makes the card the dominant term.
    print(f"Per generation call : {whole:,} input tok  (~${whole/1e6*5:.2f})")
    print(f"Per verification call: {total/n:,.0f} input tok  (~${total/n/1e6*5:.3f}) — one card, re-sent\n")

    if args.json_out:
        _pathlib.Path(args.json_out).write_text(json.dumps(
            {"games": n, "exact": counter.exact, "whole_slate_tokens": whole,
             "blocks": {r[0]: {"tokens": r[1], "per_game": r[1] / n} for r in rows}},
            indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
