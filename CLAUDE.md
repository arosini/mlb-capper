# MLB Capper — Claude Session Guide

## Default Context: Production

**When the user says something is wrong, they mean production (`mlbautocap.com`) unless they explicitly say "local" or specify a local path.** Always check the deployed site's behavior first. Production data lives in the most recent `data/` files committed to the repo and deployed via CI — not whatever happens to be in your local `data/` directory.

## What This Is
A daily MLB handicapping dashboard published to Cloudflare Pages at `mlbautocap.com`. A GitHub Actions workflow runs every 3 hours, downloads fresh data, generates a static HTML page, and deploys it.

## Credentials & Secrets
**Never commit credentials.** Two sources:
- **Local**: `.env` file (gitignored) — auto-loaded by `config.py` at import time
- **CI**: GitHub Secrets → passed as env vars in the workflow `Download data` step

Keys in use: `HANDIGRAPHS_EMAIL`, `HANDIGRAPHS_PASSWORD`, `ODDS_API_KEY`, `ANTHROPIC_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`

## Data Sources

| Source | Auth | What it provides |
|--------|------|-----------------|
| Handigraphs API | JWT Bearer (login → token) | Starters (last 3), team offense stats (L12RHP/LHP), bullpen stats (last 12), ballpark weather |
| MLB Stats API | None (free) | Home/away determination, venue name, pitcher game logs |
| The Odds API | API key (query param) | Full-game ML/spread/total + F5 ML/spread/total + pitcher K/outs props for DK, FanDuel, Fanatics — 500 req/month free |
| Anthropic API | API key (`ANTHROPIC_API_KEY`) | Claude Opus 4.8 for AI Picks — one generation call per odds refresh plus one audit call per pick, cached to `data/suggestions_{date}.json` |

## Module Structure

The codebase is split into focused modules. Import order (no circular deps):

```
teams.py          — team code maps, logo helpers; no project deps
  ↓
odds.py           — Odds API parsing + format helpers; imports teams
loaders.py        — file loaders (CSV/JSON from data/); imports teams
mlb_api.py        — MLB Stats API + Open-Meteo weather; no project deps at module level
  ↓
analysis.py       — analyze_game(), flags, trends; imports teams
  ↓
render_terminal.py — terminal output (print_game, ANSI colors); imports analysis
suggestions.py    — AI picks generation + HTML rendering; imports analysis, odds
  ↓
render_html.py    — HTML page renderer (_html_game, render_html_page, CSS/JS); imports all above
  ↓
results.py        — Results page (record + unit P&L); imports render_html, suggestions
handicap.py       — slim entry point, main() only; imports everything
```

**Standalone scripts** (also import from teams.py now):
- `download.py` — fetches all data endpoints; only file that calls the Odds API
- `history.py` — permanent odds log; imports team maps from teams.py
- `picks.py` — permanent AI picks log; imports team maps from teams.py
- `results.py` — Results page renderer + unit math; reads `picks/`, writes `_site/results/index.html`

### Key exports by module

**`teams.py`**: `_STATS_MAP`, `_MLB_MAP`, `ODDS_TEAM`, `MLB_NAME_TO_CODE`, `to_stats()`, `to_mlb()`, `logo_img()`, `_LOGO`

**`odds.py`**: `load_odds()`, `get_game_odds()`, `fmt_ml()`, `fmt_spread()`, `fmt_total()`, `fmt_k_line()`, `fmt_outs_line()`

**`loaders.py`**: `load_starters()`, `load_team_stats()`, `load_bullpen()`, `load_ballpark_weather()`, `load_odds_meta()`, `load_pitcher_props()`

**`mlb_api.py`**: `get_mlb_schedule()`, `get_recent_starts()`, `get_team_schedule()`, `get_bullpen_stress()`, `get_weather()`, `stress_label_cls()`, `STADIUMS`, `HAS_REQUESTS`

**`analysis.py`**: `analyze_game()`, `build_games()`, `validate_pitchers()`, `flt()`, `fp1()`, `fp3()`, `wrc_label()`, `xera_label()`, `pitcher_csv_flags()`, `bullpen_flags()`, `weather_flags()`, `pitcher_history_flags()`, `extract_outings()`

**`render_terminal.py`**: `print_game()`, `bold()`, `cyan()`, `yellow()`, `dim()`, `use_color` (set to False for --no-color)

**`suggestions.py`**: `generate_suggestions()`, `_render_suggestions_html()`, `_ai_game_map()`, `_pick_dom_id()`, `_pick_summary_title()`

**`render_html.py`**: `render_html_page()`, `_html_game()`

**`results.py`**: `unit_pnl()`, `summarize()`, `load_picks_range()`, `build_windows()`, `render_results_page()`

## Data Flow

**`download.py`** saves files to `data/` → **`handicap.py`** loads them via `loaders.py` and `odds.py` → `analysis.py` builds game dicts → `render_terminal.py` or `render_html.py` renders output.

Run locally: `python3 handicap.py` (terminal) or `python3 handicap.py --html > out.html`

## Data Files (in `data/`)
- `starters_last3g_{slot}_{date}.json`
- `team_stats_L12RHP_{date}.json` / `team_stats_L12LHP_{date}.json`
- `bullpen_stats_last12g_{date}.json`
- `ballpark_weather_{date}.json`
- `odds_{date}.json` — bulk game odds (merged with started-game odds from prior fetch)
- `odds_meta_{date}.json` — timestamp of last odds fetch (used for throttle check)
- `props_{date}.json` — per-event pitcher K/outs props + F5 odds
- `bullpen_stress_{date}.json` — cached bullpen IP for past 2 calendar days (written once/day)
- `suggestions_{date}.json` / `suggestions_meta_{date}.json` — AI picks cache (post-verification)

Outside `data/`: `picks/{date}.json` (git-tracked pick log) and `rejections/{date}.json`
(git-tracked log of picks the verification pass threw out, for prompt tuning).

## Team Code Normalization
Handigraphs starters use codes like `KCR`, `TBR`, `SFG`, `SDP`, `CHW`, `WSN`, `ARI`. All canonical maps live in `teams.py`:

```python
_STATS_MAP = {"CHW": "CWS", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB", "WSN": "WSH"}
_MLB_MAP   = {**_STATS_MAP, "ARI": "AZ"}  # MLB API uses AZ; ATH stays as-is
```

- **Ballpark weather lookup**: use raw Handigraphs codes (`frozenset([t1_raw, t2_raw])`) — NOT normalized
- **Odds lookup**: use `ODDS_TEAM` (Handigraphs code → full team name for Odds API)
- **Team stats / bullpen lookup**: use `to_stats()` normalized codes

## HTML Card Structure (per game)
Each card renders collapsed `<details>` sections except Matchup (open by default):
1. **Summary** (always visible): `[logo] AWAY @ [logo] HOME` + `time · venue (roof status)` subtitle + weather/APF badge
2. **Betting Odds** — Full Game: 4×3 grid (ML / Spread / Total for away/home); First 5 Innings: same grid if available; Pitcher Props: K O/U and Outs O/U per starter (requires Odds API Starter plan+)
3. **Matchup · SP Last 3 / Team Last 12** (open) — SP card: xERA, K%, HH%, Barrel%, ERA, IP/gs, H/gs, PC/gs, BB%; Offense card: wRC+, K%, HH% vs starter hand; outing table per SP
4. **Bullpens · last 12** — xERA, ERA, and **2d stress** (Fresh/Normal/Elevated/Stressed based on avg relief IP per game over past 2 calendar days via MLB boxscores) (collapsed)
5. **Weather** — venue, roof, conditions, APF with color coding (collapsed)
6. **Flags** — auto-generated warnings (regression risk, small samples, weather, etc.) (collapsed)

CSS uses `prefers-color-scheme: dark` for automatic dark mode.

## Results Page (`/results/`)

Third tab alongside Today and Tomorrow. Built by `results.py --html` from the git-tracked
`picks/{date}.json` logs — it reads the `result` field (`won`/`lost`/`push`/absent) that
`picks.py --annotate` writes, and does no grading of its own.

Shows trailing-7d / trailing-30d / year-to-date record and unit P&L, then yesterday's
picks with a ✓ / ✗ / P mark and per-pick P&L.

**Unit convention** — flat "risk 1 to win 1" staking, in `unit_pnl()`:
- Plus money `+130`: risk 1u → win **+1.30u**, lose **−1.00u**
- Minus money `−130`: risk 1.30u to win 1u → win **+1.00u**, lose **−1.30u**
- Push → 0.00u, and excluded from the win-rate denominator

Rolling windows **end yesterday**, not today — today's picks are still open, and counting
them would drag every number toward zero as the slate fills in.

Every window is floored at `TRACK_RECORD_START` (2026-08-07, the day the rewritten prompt
went live). Picks before that came from a materially different system. The older files
stay in `picks/` deliberately — they are the input to `scripts/review_rejections.py`.
Move the constant if the prompt is rewritten again.

A handful of older picks have `odds_num: null` (~14 of 448 as of 2026-08-08). They count
in the W-L record but contribute 0 units, so P&L is very slightly understated rather than
inventing a price for them.

Pending and push are visually distinct on purpose: push is a filled grey chip, pending is
a dashed outline, and both spell the state out in the meta line. They were both plain grey
circles at first and got misread as the same thing.

## Published Picks Are Immutable

`save_picks()` is an append-only merge keyed on `(game, bet_type, line, team_side)`,
keeping the first price seen. That guarantee depends entirely on `picks/{date}.json`
being **committed and pushed in the same run that deployed it** — the next run starts
from a fresh checkout, so an uncommitted pick log means `existing = []` and the freshly
regenerated suggestions become the whole file. The public page then shows a different
pick set every 3 hours.

This is exactly what happened on 2026-08-08: the commit step had been failing since 8/7,
so no pick log was ever pushed and the deployed picks silently changed every run.

Two ordering rules in `publish.yml` protect the invariant:

1. **`Annotate results` → `Commit history and picks` → HTML steps → `Deploy`.** Picks are
   persisted *before* anything is published, and the pages render the exact state that
   was committed. Annotating first also means grades ship this run instead of one behind.
2. **The commit step `exit 1`s when the push fails after retries**, which skips the
   deploy. Leaving the last good page up beats publishing picks that cannot be
   reproduced.

## Deployment
- **Repo**: `github.com/arosini/mlb-capper`
- **Hosting**: Cloudflare Pages — project `mlb-capper`, custom domain `mlbautocap.com`
- **Workflow**: `.github/workflows/publish.yml` — cron `0 */3 * * *` (every 3 hours), also `workflow_dispatch` and push-to-main
- **Deploy step**: `cloudflare/wrangler-action@v3` with `pages deploy _site --project-name=mlb-capper --commit-dirty=true`
- **Pages built per run**: `_site/index.html` (today), `_site/tomorrow/index.html`, `_site/results/index.html`
- **`git add` in the commit step goes through `_stage()`**, which `mkdir -p`s `history picks rejections` first. `rejections/` only exists on days a pick is rejected, and `git add <missing-dir>/` is fatal (exit 128) under `bash -e`. The mkdir must be inside `_stage()`, not run once up front: the retry path's `git clean -f rejections/` deletes the empty directory before the second add.
- **No-cache headers**: written inline in workflow (`printf '/*\n  Cache-Control: no-cache...' > _site/_headers`) — not a repo file
- **Secrets needed**: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `HANDIGRAPHS_EMAIL`, `HANDIGRAPHS_PASSWORD`, `ODDS_API_KEY`
- **Trigger manually**: `gh workflow run publish.yml`

## AI Picks — generation and verification

Two model calls, both Claude Opus 4.8 with `thinking: {"type": "adaptive"}`:

1. **Generation** (`generate_suggestions`) — one call for the whole slate. Runs with
   `tool_choice: auto`, **not** forced. Forcing the tool suppresses thinking entirely on
   Opus 4.8 (verified: a forced call returns a bare `tool_use` block, no thinking), which
   would defeat the point of using Opus. If the model answers in prose instead of calling
   the tool, a follow-up turn re-asks with the tool forced — the reasoning is already in
   context, so nothing is lost.
2. **Verification** (`_verify_pick`) — one call *per pick*. Re-sends the exact data card
   that pick came from plus its rationale to a fresh context, and returns ACCEPT/REJECT.
   Rejected picks are dropped before anything is saved and logged to
   `rejections/{date}.json` for prompt tuning. **Fails open**: an API error or a missing
   verdict keeps the pick rather than silently discarding it.

The auditor's top check is backwards baseball logic — backing a team while citing that
team's *own* pitcher's bad xERA/ERA. Each team bats against the OPPOSING pitcher; this
was the most common failure in the pre-rewrite prompt.

**Rationales are public-facing copy, not a reasoning transcript.** Section 10 of
`_AI_SYSTEM_PROMPT` (and the `reason` / `pass_reasons` tool-schema descriptions) forbid
leaking the prompt's own rules into the output: no "Tier 1"/"per the rules", no reminders
of methodology ("their own starter's ERA doesn't affect how they hit"), no self-correction
or process narration ("Calibrating:", "Note:", "that is irrelevant here"), no defending
stats the model chose not to use. The auditor does **not** reject on this — style is not
worth dropping an otherwise sound pick — so it is enforced only at generation time.

**Stat windows are temporal and the prompt depends on it.** SP xERA/ERA/K%/BB% are the
last 3 starts (`starters_last3g_*`), team wRC+/K% are the last 12 games split by opposing
starter hand, bullpens are the last 12. xERA and ERA cover the *same* 3-start window —
they are not "season vs recent". The prompt requires every stat in a rationale to carry
its window in the output text.

`extract_outings()` returns outings **newest-first**. `render_html.py` slices `[:n]`;
`suggestions.py` takes `[:3]` then reverses for chronological display. Do not `[-3:]`.

## Grading Pitcher Props

Prop picks grade off `history/{date}.json` → `pitchers[]`, which is built from **posted
prop lines**. Books pull a pitcher's line once his game starts, so rebuilding that list
purely from the current `bookmakers` payload silently dropped pitchers — and any pick
naming one could never grade. 62% of prop picks (82 of 133) were stuck at `result: null`
before this was fixed on 2026-08-08.

Three pieces keep it working; all three are load-bearing:

1. `_build_pitcher_props()` **carries forward** any pitcher already in the record whose
   props have since vanished.
2. Annotation **adds starters found in the boxscore** but missing from `pitchers[]`, with
   a `null` line. Starters only — `_fetch_pitcher_stats()` marks index 0 per side as
   `started`, and adding relievers risks a last-name collision in the substring match
   `_resolve_pick()` uses.
3. `_resolve_pick()` **falls back to the pick's own `line`** when the history entry has
   none. This is the more correct semantics anyway: grade against the line that was
   actually taken, not whatever the books last showed.

To re-grade a day after a fix, clear `annotated_at` on the affected records (history) and
on ungraded picks, then re-run `history.py --annotate --date X` and `picks.py --annotate
--date X`. Both skip anything already annotated.

## Day Navigation

Three sibling pages — `/results/`, `/`, `/tomorrow/` — ordered chronologically in the nav
strip. `_SWIPE_SCRIPT` in `render_html.py` maps a horizontal swipe onto that same order
and is included by both `render_html_page()` and `results.py`. Keep the nav order and the
`ORDER` array in sync or the gesture direction stops matching the visual layout.

Guards: ignores multi-touch, swipes under 60px, swipes with |dy| > 80, gestures where the
vertical axis wins, and anything starting inside a horizontally scrollable child.

## Adding New Data Fields
1. Check what's available: `python3 download.py --inspect`
2. Map the raw JSON key in the appropriate `_load_*_json()` in `loaders.py`
3. Add to the `_sp()` / `_bp()` / `_off()` dict inside `analyze_game()` in `analysis.py`
4. Render it in `_sp_card()` / `_bp_row()` / `_bat_card()` in `render_html.py`

## Odds API Budget
Paid tier — a full run on 2026-08-07 (1 bulk + 15 prop calls) reported ~15,795 remaining, so the old "500/month free" note no longer applies. Usage is ~16 calls per refresh (once per 3-hour window × number of games for props). Bulk odds: 1 call/run. Props: 1 call/game/run (skipped if < 3h old). To check remaining quota, run `python3 download.py` and watch the odds/props lines — they print `X API calls remaining`.

**CRITICAL**: Never let Claude run `curl https://api.the-odds-api.com/` — this costs credits. The `.claude/settings.json` denies this automatically.
