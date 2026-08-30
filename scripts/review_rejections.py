#!/usr/bin/env python3
"""
Weekly prompt review — read the picks that were rejected before publication, look for
recurring failure patterns, and propose an edit to the generation prompt.

Since the AI audit pass was removed on 2026-08-30 these are all MECHANICAL rejections
from suggestions._validate_pick: a price past the floor, a line or price the card does
not post, a stated edge under the minimum, or one of the Section 8 disqualifiers. They
say less about reasoning than the audit rejections did, but a recurring one still points
at a prompt that is asking for something the card cannot support.

This NEVER edits the prompt in place on main. It writes the proposed replacement to
disk and prints a report; the workflow turns that into a pull request for a human to
review and merge.

Usage:
  python3 scripts/review_rejections.py [--days 7] [--out proposal.json]

Exit codes:
  0  proposal written (a change is recommended)
  1  error
  2  nothing to do (no rejections in window, or no change recommended)
"""
import sys as _sys, pathlib as _pathlib

# Drop scripts/ from sys.path before importing anything else. Python puts the script's
# own directory first, so any module in here whose name matches a stdlib one shadows it
# for every later import — including imports made deep inside third-party packages.
# `scripts/inspect.py` did exactly that: anthropic → typing_extensions → `import inspect`
# resolved to this directory and the weekly review died on `inspect.signature`. Nothing
# here imports a sibling by name; the repo root below is the only path we need.
_HERE = str(_pathlib.Path(__file__).resolve().parent)
_sys.path[:] = [p for p in _sys.path if p not in ("", ".", _HERE)]
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from season import ET as _ET

# The prompt lives as a module-level triple-quoted string. We swap only the body
# between these sentinels so nothing else in suggestions.py can be disturbed.
_PROMPT_START = '_AI_SYSTEM_PROMPT = """\\\n'
_PROMPT_END = '"""\n'


def load_rejections(rej_dir: Path, days: int, today: date) -> list[dict]:
    """Every rejection logged in the last `days` days, newest file last."""
    out = []
    for n in range(days, -1, -1):
        p = rej_dir / f"{(today - timedelta(days=n)).isoformat()}.json"
        if not p.exists():
            continue
        try:
            out.extend(json.loads(p.read_text()))
        except Exception as e:
            print(f"[review] skipping unreadable {p}: {e}", file=sys.stderr)
    return out


def read_prompt(src: Path) -> tuple[str, str]:
    """Return (full_file_text, current_prompt_body). Raises if the markers move."""
    text = src.read_text()
    i = text.find(_PROMPT_START)
    if i < 0:
        raise RuntimeError("could not locate _AI_SYSTEM_PROMPT opening in suggestions.py")
    body_start = i + len(_PROMPT_START)
    j = text.find("\n" + _PROMPT_END, body_start)
    if j < 0:
        raise RuntimeError("could not locate _AI_SYSTEM_PROMPT closing delimiter")
    # Invariant: text == text[:body_start] + body + text[j:], where text[j:] opens with
    # the newline that closes the string. Keep the split exactly here so an unchanged
    # body round-trips byte-for-byte.
    return text, text[body_start:j]


def splice_prompt(text: str, new_body: str) -> str:
    i = text.find(_PROMPT_START)
    body_start = i + len(_PROMPT_START)
    j = text.find("\n" + _PROMPT_END, body_start)
    # Normalize the body to exactly one trailing newline regardless of what the model
    # returned; text[j:] then supplies the newline that closes the string literal.
    return text[:body_start] + new_body.rstrip("\n") + "\n" + text[j:]


_REVIEW_SYSTEM = """\
You maintain the system prompt of an automated MLB betting analyst. Deterministic checks
screen every pick that prompt produces and reject any that quote a price or line the card
does not post, fall below the price floor or the minimum stated edge, or trip one of the
Section 8 disqualifiers. You are reading a week of those rejections to decide whether the
PROMPT should change.

You are diagnosing a prompt, not a slate. A pick was already correctly thrown out — the
question is whether the prompt's wording allowed a mistake it should have prevented. These
are mechanical rejections, so the usual answer is that the prompt invited the model to
reason about something the card does not actually carry.

═══════════════════════════════════════════════════════════
WHEN TO RECOMMEND A CHANGE
═══════════════════════════════════════════════════════════

Recommend a change ONLY for a RECURRING, ADDRESSABLE pattern:
  • The same class of error appears at least twice, or once with an obvious systemic
    cause, AND
  • You can point to the specific instruction that failed — missing, ambiguous, buried,
    or contradicted elsewhere — AND
  • You can state a concrete edit that would have prevented it.

Do NOT recommend a change for:
  • A one-off slip with no pattern behind it
  • An error the prompt already forbids clearly — that is a model lapse, not a prompt
    defect, and piling on more emphasis makes the prompt worse, not better
  • Anything you would fix by making the prompt longer without making it clearer

A week with no prompt-level pattern is a NORMAL and expected outcome. Returning
recommend_change=false is the right answer more often than not. Do not invent work.

═══════════════════════════════════════════════════════════
HOW TO EDIT
═══════════════════════════════════════════════════════════

If you do recommend a change, return the COMPLETE new prompt text — not a diff.

  • Make the SMALLEST edit that fixes the pattern. Surgical, not a rewrite.
  • PRESERVE the existing structure, section numbering, and voice exactly.
  • Do NOT weaken or delete an existing rule to make room for a new one.
  • Do NOT touch sections unrelated to the pattern you identified.
  • Prefer sharpening an existing instruction over appending a new one. The prompt is
    already long; length is a cost.
  • Never use triple double-quotes anywhere in the text — it is embedded in a Python
    triple-quoted string and would terminate it.

In your rationale, quote the offending rejections and name the exact wording you changed
and why. A human reads this before merging.
"""

_REVIEW_TOOL = {
    "name": "report_prompt_review",
    "description": "Report the weekly rejection analysis and any proposed prompt change.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-4 sentences: what the week's rejections show. Always required.",
            },
            "patterns": {
                "type": "array",
                "description": "Distinct recurring failure patterns found. Empty if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "The failure class, one line."},
                        "count": {"type": "integer", "description": "How many rejections fit it."},
                        "example": {"type": "string", "description": "A quoted rejection reason."},
                        "prompt_gap": {
                            "type": "string",
                            "description": "The specific instruction that failed, and how.",
                        },
                    },
                    "required": ["pattern", "count", "prompt_gap"],
                },
            },
            "recommend_change": {
                "type": "boolean",
                "description": "True only for a recurring, addressable, prompt-level defect.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this edit, or why no edit is warranted. Human-readable.",
            },
            "proposed_prompt": {
                "type": ["string", "null"],
                "description": (
                    "REQUIRED when recommend_change is true: the complete new prompt text, "
                    "structure preserved, smallest viable edit. Null otherwise."
                ),
            },
            "change_summary": {
                "type": ["string", "null"],
                "description": "One line naming exactly what changed, for the PR title.",
            },
        },
        "required": ["summary", "patterns", "recommend_change", "rationale"],
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--rej-dir", default="./rejections")
    ap.add_argument("--src", default="./suggestions.py")
    ap.add_argument("--out", default="prompt_proposal.json")
    ap.add_argument("--write", action="store_true",
                    help="Splice the proposed prompt into --src (workflow uses this on a branch)")
    args = ap.parse_args()

    today = datetime.now(_ET).date()
    rejections = load_rejections(Path(args.rej_dir), args.days, today)
    if not rejections:
        print(f"[review] no rejections in the last {args.days} days — nothing to review")
        return 2

    print(f"[review] {len(rejections)} rejection(s) in the last {args.days} days")

    src = Path(args.src)
    try:
        file_text, current_prompt = read_prompt(src)
    except RuntimeError as e:
        print(f"[review] {e}", file=sys.stderr)
        return 1

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        try:
            import config
            api_key = config.ANTHROPIC_API_KEY
        except Exception:
            pass
    if not api_key:
        print("[review] ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    import anthropic

    # Strip fields the reviewer doesn't need, keeping the payload focused.
    trimmed = [
        {k: r.get(k) for k in ("date", "game", "bet_type", "bet", "odds", "reason", "reject_reason")}
        for r in rejections
    ]
    user_msg = (
        f"Window: last {args.days} days ending {today.isoformat()}.\n"
        f"{len(trimmed)} pick(s) were rejected before publication.\n\n"
        f"REJECTIONS\n{json.dumps(trimmed, indent=2)}\n\n"
        f"{'=' * 70}\n\nCURRENT GENERATION PROMPT\n\n{current_prompt}\n\n"
        f"{'=' * 70}\n\nAnalyze and call report_prompt_review."
    )

    # This runs once a week from cron with no retry above it, and a single
    # `overloaded_error` fails the whole run — three in a row were observed while
    # fixing this job. The SDK's own backoff is the cheapest place to absorb that.
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)
    # Both calls stream. max_tokens=32000 is deliberate — the tool returns a full
    # rewritten prompt — and the SDK refuses a non-streaming request it estimates
    # may run past ~10 minutes, which is what killed this job. get_final_message()
    # gives back the same Message object create() would have returned.
    try:
        with client.messages.stream(
            model="claude-opus-5",
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=_REVIEW_SYSTEM,
            tools=[_REVIEW_TOOL],
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            resp = stream.get_final_message()
        block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if not block:
            # Same auto/forced pattern as generation: thinking is suppressed by a forced
            # tool_choice, so ask freely first and only force the structuring turn.
            with client.messages.stream(
                model="claude-opus-5", max_tokens=32000,
                thinking={"type": "adaptive"}, output_config={"effort": "high"},
                system=_REVIEW_SYSTEM, tools=[_REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "report_prompt_review"},
                messages=[
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": "Now submit that via report_prompt_review."},
                ],
            ) as stream:
                resp = stream.get_final_message()
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if not block:
            print("[review] no structured verdict returned", file=sys.stderr)
            return 1
        result = block.input or {}
    except Exception as e:
        print(f"[review] API error: {e}", file=sys.stderr)
        return 1

    Path(args.out).write_text(json.dumps(result, indent=2))

    print(f"\n{'=' * 70}\n{result.get('summary', '')}\n")
    for p in result.get("patterns") or []:
        print(f"  • [{p.get('count')}x] {p.get('pattern')}")
        print(f"      gap: {p.get('prompt_gap')}")
    print(f"\nrecommend_change: {result.get('recommend_change')}")

    if not result.get("recommend_change"):
        print("[review] no prompt change recommended")
        return 2

    new_prompt = result.get("proposed_prompt") or ""
    # Guard the splice: a truncated or mangled prompt would silently gut the analyst.
    if len(new_prompt) < len(current_prompt) * 0.6:
        print(f"[review] proposed prompt is suspiciously short "
              f"({len(new_prompt)} vs {len(current_prompt)} chars) — refusing to write",
              file=sys.stderr)
        return 1
    if '"""' in new_prompt:
        print("[review] proposed prompt contains a triple-quote — refusing to write",
              file=sys.stderr)
        return 1

    if args.write:
        updated = splice_prompt(file_text, new_prompt)
        src.write_text(updated)
        # Fail loudly rather than committing a file that won't import.
        import py_compile
        try:
            py_compile.compile(str(src), doraise=True)
        except py_compile.PyCompileError as e:
            src.write_text(file_text)
            print(f"[review] proposed prompt broke the module, reverted: {e}", file=sys.stderr)
            return 1
        print(f"[review] wrote proposed prompt into {src}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
