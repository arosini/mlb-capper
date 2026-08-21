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
| Handigraphs API | JWT Bearer (login → token) | Starters (last 3), team offense stats (L6RHP/LHP primary + L12RHP/LHP for the comparison wRC+), bullpen stats (last 12), ballpark weather |
| MLB Stats API | None (free) | Home/away determination, venue name, pitcher game logs |
| The Odds API | API key (query param) | Full-game ML/spread/total + F5 ML/spread/total + team totals + pitcher K/outs props for DK, FanDuel, Fanatics. Paid plan (~20K credits/month); billed per market × region, **not** per call — see API Budget |
| Anthropic API | API key (`ANTHROPIC_API_KEY`) | Claude Opus 4.8 for AI Picks — one generation call per odds refresh plus one audit call per pick, cached to `data/suggestions_{date}.json` |

## Module Structure

The codebase is split into focused modules. Import order (no circular deps):

```
season.py         — ET clock, MLB game types, in-season detection; NO project deps
venues.py         — venue registry, neutral-site detection, roof kind; imports season
usage.py          — paid-API usage ledger (usage/{YYYY-MM}.json); imports season
teams.py          — team code maps, logo helpers; no project deps
  ↓
odds.py           — Odds API parsing + format helpers; imports teams
loaders.py        — file loaders (CSV/JSON from data/); imports teams, season
mlb_api.py        — MLB Stats API + Open-Meteo weather; imports season
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

**`season.py`**: `ET`, `GAME_TYPES`, `today_et()`, `game_count()`, `has_games()`, `season_start()`

**`venues.py`**: `home_venue_ids()`, `venue_geo()`, `coords_are_sane()`, `roof_kind()`, `ROOFED`

**`usage.py`**: `record_claude()`, `record_odds()`, `load_days()`, `cost_usd()`, `PRICING`, `monthly_budget_usd()`

**`budget.py`**: `collect()`, `render_budget_page()`

**`teams.py`**: `_STATS_MAP`, `_MLB_MAP`, `ODDS_TEAM`, `MLB_NAME_TO_CODE`, `to_stats()`, `to_mlb()`, `logo_img()`, `_LOGO`

**`odds.py`**: `load_odds()`, `get_game_odds()`, `fmt_ml()`, `fmt_spread()`, `fmt_total()`, `fmt_k_line()`, `fmt_outs_line()`

**`loaders.py`**: `load_starters()`, `load_team_stats()`, `load_bullpen()`, `load_ballpark_weather()`, `load_odds_meta()`, `load_pitcher_props()`

**`mlb_api.py`**: `get_mlb_schedule()`, `get_recent_starts()`, `get_team_schedule()`, `get_bullpen_stress()`, `get_weather()` (takes lat/lon, not a team code), `wind_effect()`, `compass_point()`, `stress_label_cls()`, `STADIUMS` (legacy fallback only), `HAS_REQUESTS`

**`analysis.py`**: `analyze_game()`, `build_games()`, `validate_pitchers()`, `flt()`, `fp1()`, `fp3()`, `wrc_label()`, `xera_label()`, `pitcher_csv_flags()`, `bullpen_flags()`, `weather_flags()`, `pitcher_history_flags()`, `team_whiff_k_flag()`, `pitcher_whiff_k_flag()`, `extract_outings()`

**`render_terminal.py`**: `print_game()`, `bold()`, `cyan()`, `yellow()`, `dim()`, `use_color` (set to False for --no-color)

**`suggestions.py`**: `generate_suggestions()`, `_render_suggestions_html()`, `_ai_game_map()`, `_pick_dom_id()`, `_pick_summary_title()`

**`render_html.py`**: `render_html_page()`, `_html_game()`

**`results.py`**: `unit_pnl()`, `summarize()`, `load_picks_range()`, `build_windows()`, `render_results_page()`

## Data Flow

**`download.py`** saves files to `data/` → **`handicap.py`** loads them via `loaders.py` and `odds.py` → `analysis.py` builds game dicts → `render_terminal.py` or `render_html.py` renders output.

Run locally: `python3 handicap.py` (terminal) or `python3 handicap.py --html > out.html`

## Data Files (in `data/`)
- `starters_last3g_{slot}_{date}.json`
- `team_stats_L6RHP_{date}.json` / `team_stats_L6LHP_{date}.json` — **primary** team offense window
- `team_stats_L12RHP_{date}.json` / `team_stats_L12LHP_{date}.json` — longer window; only its wRC+ is used
- `bullpen_stats_last12g_{date}.json`
- `ballpark_weather_{date}.json`
- `odds_{date}.json` — bulk game odds (merged with started-game odds from prior fetch)
- `odds_meta_{date}.json` — timestamp of last odds fetch (used for throttle check)
- `props_{date}.json` — per-event pitcher K/outs props + F5 odds
- `bullpen_stress_{date}.json` — cached bullpen IP for past 2 calendar days (written once/day)
- `suggestions_{date}.json` / `suggestions_meta_{date}.json` — AI picks cache (post-verification)
- `season_{date}.json` — cached count of qualifying MLB games (off-season gate; see Season Window)
- `home_venues_{year}.json` — each club's registered home venue id (neutral-site detection)
- `venue_{id}.json` — cached venue geo (coords, azimuth, elevation)

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
3. **Matchup · SP Last 3 / Team Last 6/12** (open) — SP card: xERA, ERA, K%, Whiff% visible; HH%, Barrel%, IP/gs, H/gs, PC/gs, BB% behind "More Stats"; Offense card: two group headers "L6" and "L12", each with wRC+, K%, Whiff%, HH% for that window vs starter hand; outing table per SP
4. **Bullpens · last 12** — xERA, ERA, and **2d stress** (Fresh/Normal/Elevated/Stressed based on avg relief IP per game over past 2 calendar days via MLB boxscores) (collapsed)
5. **Weather** — venue, roof, conditions, APF with color coding (collapsed)
6. **Flags** — auto-generated warnings (regression risk, small samples, weather, etc.) (collapsed)

CSS uses `prefers-color-scheme: dark` for automatic dark mode.

## Results Page (`/results/`)

Third tab alongside Today and Tomorrow. Built by `results.py --html` from the git-tracked
`picks/{date}.json` logs — it reads the `result` field (`won`/`lost`/`push`/absent) that
`picks.py --annotate` writes, and does no grading of its own.

Shows trailing-7d / trailing-30d / all-time record and unit P&L, then yesterday's
picks with a ✓ / ✗ / P mark and per-pick P&L.

**Unit convention** — flat "risk 1 to win 1" staking, in `unit_pnl()`:
- Plus money `+130`: risk 1u → win **+1.30u**, lose **−1.00u**
- Minus money `−130`: risk 1.30u to win 1u → win **+1.00u**, lose **−1.30u**
- Push → 0.00u, and excluded from the win-rate denominator

Rolling windows **end yesterday**, not today — today's picks are still open, and counting
them would drag every number toward zero as the slate fills in.

Every window is floored at `TRACK_RECORD_START` (**2026-08-10** — a deliberate clean
slate after the old prompt and the unsettled days replacing it). The widest window starts
*at* that date rather than at Jan 1 — a year-to-date floor looked identical all of 2026
and would have silently reset the headline record to 0-0 on New Year's Day.

The 40 pre-cutoff files live in **`picks/archive/`**. `load_picks_range()` globs
`picks/*.json` **non-recursively**, so archiving is all it takes to exclude them — and
they stay available for comparing the old prompt against the new one. Move the constant
(and archive the superseded files) if the prompt is rewritten again.

The constant only controls what the **record counts**. Picks are still generated,
published and displayed every day, which is why the site keeps showing picks over a
weekend that does not count toward the number.

> An earlier version of this file said the old pick files were the input to
> `scripts/review_rejections.py`. They are not — that script reads `rejections/`.
> Archiving or deleting `picks/` does not affect the weekly prompt review.

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

**Reversed matchups break the dedupe key.** `_canon_pick_key()` contains `game`, so a
pick the model emits backwards ("BOS @ ATH" for a game that is really ATH @ BOS) does not
match the copy already logged — it lands as a second entry with no `game_time_utc` and a
`team_side` pointing at the wrong club. `save_picks()` now looks the matchup up in both
orientations and snaps it back to the real one.

When correcting, the side is re-derived from the **team named in the bet text**, not by
flipping the model's `away_`/`home_` prefix. In the case that surfaced this (2026-08-08,
BOS Team Total Over 5.5) `team_side` was already correct for the true orientation and only
`game` was wrong — a blind flip would have graded the Athletics' runs instead of Boston's.
The leading code in the bet text is the one field independent of the model's framing.

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
- **Pages built per run**: `_site/index.html` (today), `_site/tomorrow/index.html`, `_site/results/index.html`, `_site/budget/index.html`
- **`git add` in the commit step goes through `_stage()`**, which `mkdir -p`s `history picks rejections` first. `rejections/` only exists on days a pick is rejected, and `git add <missing-dir>/` is fatal (exit 128) under `bash -e`. The mkdir must be inside `_stage()`, not run once up front: the retry path's `git clean -f rejections/` deletes the empty directory before the second add.
- **No-cache headers**: written inline in workflow (`printf '/*\n  Cache-Control: no-cache...' > _site/_headers`) — not a repo file
- **Secrets needed**: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `HANDIGRAPHS_EMAIL`, `HANDIGRAPHS_PASSWORD`, `ODDS_API_KEY`, `ANTHROPIC_API_KEY`
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

**The card must name each starter's club, because nothing else does.** `_sp_line()` used
to print `  Dean Kremer (R): xERA …` with no team — the offense, bullpen and trend lines
were all team-labelled, but the pitcher block was positional only, so which club a starter
threw for had to be inferred from list order (away first, home second). That inference
loses to the model's own prior the moment a starter is traded mid-season. On 2026-08-10,
Kremer had moved BAL → MIN; the data was correct end to end (`team: MIN`, `opponent: BAL`,
`analyze_game` put him on the home side) and the published rationale still had him facing
Minnesota — the matchup inverted, with a strikeout rate attributed to the wrong lineup.
Lines now read `MIN (home) — Dean Kremer (R): …`, the props line carries the team too, and
both prompts say the card's club is authoritative over prior knowledge. The auditor rejects
a rationale that has a starter facing the lineup he is actually pitching for.

Note the failure was invisible to the audit pass for the same reason it was invisible to
generation: the verifier is re-sent the *same card*, so an unlabelled pitcher block gave it
no way to catch the swap either. Anything the model must not get wrong has to be **on the
card** — a second opinion over identical data does not add a fact.

**Rationales are public-facing copy, not a reasoning transcript.** Section 10 of
`_AI_SYSTEM_PROMPT` (and the `reason` / `pass_reasons` tool-schema descriptions) forbid
leaking the prompt's own rules into the output: no "Tier 1"/"per the rules", no reminders
of methodology ("their own starter's ERA doesn't affect how they hit"), no self-correction
or process narration ("Calibrating:", "Note:", "that is irrelevant here"), no defending
stats the model chose not to use. The auditor does **not** reject on this — style is not
worth dropping an otherwise sound pick — so it is enforced only at generation time.

**Stat windows are temporal and the prompt depends on it.** SP xERA/ERA/K%/BB% are the
last 3 starts (`starters_last3g_*`), team wRC+/K%/HH% are the **last 6** games split by
opposing starter hand (`team_stats_L6{RHP,LHP}_*`), bullpens are the last 12. xERA and
ERA cover the *same* 3-start window — they are not "season vs recent". The prompt
requires every stat in a rationale to carry its window in the output text.

**Team offense carries two wRC+ windows and only two.** The last-6 split drives every
offense number on the card and the offense edge; the last-12 split contributes exactly
one figure, a comparison wRC+ printed beside it (`team_stats_L12{RHP,LHP}_*`). Nothing
else comes from the 12-game file. The two are labelled by window everywhere they appear
— HTML card, terminal, AI data card — because a published rationale naming the wrong
window states a false time period as fact; §1 and §10 of `_AI_SYSTEM_PROMPT` and check 2
of the verification prompt all enforce that. The split tokens live in one place,
`config.TEAM_SPLIT_PRIMARY` / `config.TEAM_SPLIT_CONTEXT`, and the API 400s on an
unknown split rather than silently ignoring it. **Bullpens are a genuinely separate
last-12 dataset** (`bullpen_stats_last12g`) and are not part of this.

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

## `scripts/` — never name a file after a stdlib module

Python puts a script's own directory first on `sys.path`, so any module in `scripts/`
whose name collides with the standard library shadows it for **every** later import,
including ones made deep inside third-party packages. `scripts/inspect.py` did exactly
that: `Weekly Prompt Review` ran `python3 scripts/review_rejections.py`, whose
`import anthropic` reached `typing_extensions`, whose `import inspect` resolved to the
ad-hoc CLI, and the job died on `module 'inspect' has no attribute 'signature'`. It had
been broken since the file landed (commit `3a874af`) and only surfaced on the Monday cron.

The CLI is `scripts/inspect_data.py` now, and `review_rejections.py` strips its own
directory from `sys.path` before importing anything else — neither script imports a
sibling by name, so the repo root it inserts is the only path either needs.

`review_rejections.py` also **streams**: `max_tokens=32000` is deliberate (the tool
returns a whole rewritten prompt) and the SDK refuses a non-streaming request it estimates
could run past ~10 minutes. `get_final_message()` returns the same Message `create()`
would have. The client runs with `max_retries=6` because a single `overloaded_error` fails
a job that only gets one shot a week.

## Adding New Data Fields
1. Check what's available: `python3 download.py --inspect`
2. Map the raw JSON key in the appropriate `_load_*_json()` in `loaders.py`
3. Add to the `_sp()` / `_bp()` / `_off()` dict inside `analyze_game()` in `analysis.py`
4. Render it in `_sp_card()` / `_bp_row()` / `_bat_card()` in `render_html.py`

## Season Window — regular + postseason only

`season.py` owns two facts the whole project depends on.

**1. ET is `ZoneInfo("America/New_York")`, not a fixed −4 offset.** Every module used to
define its own `_ET = timezone(timedelta(hours=-4))`. That is EDT, correct from mid-March
to early November and wrong the rest of the year — including the back half of the World
Series. Import `from season import ET as _ET`; never re-derive it. The two workflows call
`season.today_et()` for the same reason.

**2. `GAME_TYPES = "R,F,D,L,W"` — regular season plus all four postseason rounds.**
Spring training (`S`), exhibition (`E`) and the All-Star game (`A`) are excluded on
purpose; this site covers the regular and postseason only.

This list is load-bearing. `handicap.py` treats the MLB schedule as the authoritative game
list and drops any game missing from it, so the previous `gameType: "R"` filter meant a
**blank page every day of the postseason** — verified against the live API: 2025-10-25
(World Series) and 2025-10-15 (LCS) both return `totalGames: 0` under `R` and `1` under
`GAME_TYPES`. It also meant `history.py --annotate` could never grade a postseason pick.
All four call sites use `GAME_TYPES` now (three in `mlb_api.py`, one in `history.py`).

**Off-season / spring-training gating.** `download_all()` calls `season.has_games()` and
skips the Odds API entirely when MLB has no qualifying game that day. This matters because
the Odds API's `baseball_mlb` feed keeps serving *spring training* events — without the
gate we paid for a bulk call plus one per-event props call per exhibition game, every run,
for all of March, on games the site would never display. Verified: 2026-03-16 returns 0
qualifying games alongside 12 spring-training games.

`has_games()` **fails open**. A statsapi outage returns "unknown" and the run proceeds
normally — skipping a real slate is far worse than one wasted call. It answers from a date
window (Mar 15 – Nov 15) without a network call outside the season, and caches inside it to
`data/season_{date}.json` so the three `handicap.py` invocations per run share one lookup.
The Mar 15 floor is deliberately earlier than any real opening day (2024's Seoul Series
opened Mar 20).

Empty pages now distinguish three cases: off-season, tomorrow's slate not yet posted, and
no games today.

## Venue Truth and Weather

**The home team is not the ballpark.** MLB plays a handful of neutral-site games a year
(Field of Dreams, the Little League Classic, Mexico City, Tokyo/London, Bristol), and a
club occasionally spends a whole season elsewhere — Tampa Bay played all of 2025 at the
Yankees' spring park. Scanning 2025+2026 finds **96 such games**. Upcoming: **MIN at
Field of Dreams 2026-08-13** and **MIL at Williamsport 2026-08-23**.

**Detection is exact, not heuristic.** Every game carries a `venue.id`; every club has a
registered home `venue.id` from `/api/v1/teams`. A mismatch *is* a neutral site.
`venues.home_venue_ids()` caches the map per season; `handicap.py` sets
`mlb_info["neutral_site"]`, which flows into the game dict, a flag, the AI card header,
and a `· NEUTRAL SITE` mark on the card. It self-corrects on relocations, because MLB
re-registers the club's home venue (the Athletics at Sutter Health Park read as normal).

`get_mlb_schedule()` hydrates `venue(location)`, so coordinates, azimuth, and elevation
arrive with the schedule at no extra call.

**On a neutral site the park factor is suppressed** (`weather_flags(..., neutral_site=True)`).
APF is keyed to the home club's usual park, so at Dyersville it describes Target Field —
and §9 of the prompt uses APF as a totals tiebreaker. A park factor for the wrong park is
worse than none.

**Do not trust MLB's venue coordinates blindly.** They are good for the 30 regular parks
(Coors: 39.756, −104.994, elev 5190) but degrade at exactly the one-off venues that
matter: Estadio Alfredo Harp Helú returns `latitude: null`, and Bristol Motor Speedway
returns a latitude 5° off (31.5156 vs 36.5156 — ~350 miles into Georgia, while `state`
correctly reads Tennessee). `venues.coords_are_sane()` gates them, and a venue that fails
gets **no weather at all** rather than the home team's usual park.

### Why the weather was "way off"

`get_weather()` read the **daily aggregate** — `temperature_2m_max`,
`precipitation_probability_max`, `windspeed_10m_max` — and reported it as conditions for
a 7 PM first pitch. Measured at Coors on 2026-08-09:

| | Reported | Actual at game time | Error |
|---|---|---|---|
| Temperature | 98.4°F | 81.2°F | **+17.2°F** |
| Wind | 19.5 mph | 6.2 mph | **+13.3 mph** |

Three separate bugs, all fixed:

1. **Daily max → hourly at game time.** The function now takes `first_pitch_utc` and
   averages the 4 hours the game occupies. A 4 AM rain band no longer sets
   `precip_risk_during_game` for a dry evening game — which matters because that trips
   the prompt's disqualifier on pitcher overs.
2. **Wind direction was never consulted.** The old rule was literally
   `"Out" if wind > 15 else ""` — so any windy day read as "blowing out," backwards
   about half the time, feeding straight into totals reasoning. `wind_effect()` now
   compares the meteorological wind direction against the park's azimuth (home plate →
   center field); wind blowing out originates at `azimuth + 180`. Validated against the
   best-known case in baseball: Wrigley's azimuth is 37, so a SW wind (217) reads "Out",
   which is exactly the wind that famously blows out there. Distribution over uniform
   random input is 25% Out / 25% In / 50% Cross, matching the cone geometry.
3. **Roof was hardcoded `"Open Air"`.** Domes reported wind and rain. `venues.roof_kind()`
   knows the fixed roof (Tropicana) and the seven retractables; fixed roofs report
   `Indoor` with no wind/precip, and retractables carry an explicit
   "conditions apply only if open" caveat, since the fallback path cannot know the state.

`get_weather()` now takes **coordinates**, not a team code. `STADIUMS` survives only as a
last-resort map and must not be reintroduced on the primary path — it is team-keyed and
therefore wrong for exactly the games this section is about.

## Data Volume — what grows, and what is bounded

`history/` and `picks/` are append-only and git-tracked, so they grow by one file per day
forever. Current rates: history ~70 KB/day (~13 MB/season), picks ~20 KB/day. The repo is
fine; the risk is code that reads *all* of it.

- **`load_history_games(history_dir, target_date)` is windowed** to the last 45 days,
  floored at `season_start()`. This is a **correctness** fix more than a speed one: without
  the season floor, an April 2027 page would compute "last 10 games" over/under records
  from September 2026 games — last year's roster presented as this year's trend. Filenames
  are the slate date, so the window is applied before any file is opened. Verified: opening
  day 2027 loads 0 historical games.
- **`load_picks_range()` globs and filters** instead of walking the range day by day. The
  all-time window grows without bound; a day-walk would stat one missing path per
  off-season day, forever.
- **`data/`** is bounded by the workflow's `Clean stale data files` step (keeps only
  today/tomorrow).

If `history/` ever does become a problem, the fix is to archive completed seasons into
`history/{year}/` — the loader's filename-prefix filter would need to learn the subdirectory.

## API Budget — where the money goes

### The Odds API — the binding constraint

Cost is **markets × regions per call**, not one credit per call:

| Call | Markets | Credits |
|------|---------|---------|
| Bulk odds (`/odds`) | h2h, spreads, totals | **3** |
| Per-event props (`/events/{id}/odds`) | 2 pitcher props + 3 F5 + 2 team totals = 7 | **7** |

With 4 cron runs/day, a 300-minute throttle, and a ~15-game slate:

| Stream | Calls/day | Credits/day |
|--------|-----------|-------------|
| Today's bulk | 3 | 9 |
| Tomorrow's bulk | 4 (6:30 PM run forces) | 12 |
| Today's props | 3 × ~14 games = 42 | 294 |
| Tomorrow's props | 4 × 15 games = 60 | **420** |
| **Total** | ~109 | **~735** |

That is **~22,000 credits/month**. The one recorded observation — ~15,795 remaining on
2026-08-07 — implies a **20,000/month plan** and ~647 credits/day actual, within 12% of
the model above.

**So the Odds API is running at roughly 98–110% of plan and is the thing most likely to
break first.** It is not a future problem; some months are already at or over the cap.
Postseason is cheap (1–8 games/day), so the exposure is every month from April to
September. Adding one market to the props URL costs ~14% more; adding a fifth cron run
costs ~25% more. Do neither without checking the numbers.

**Tomorrow's odds are now fetched once a day, not four times.** They are only wanted from
about 6 PM ET, so `Download tomorrow's data` checks the ET hour and passes `--no-odds`
before 17:00 — Handigraphs data still refreshes on every run, the metered API does not.
That cut the largest line on the bill from 420 credits/day to 105:

| | Before | After |
|---|---|---|
| Tomorrow's props | 4 × 15 × 7 = 420 | 1 × 15 × 7 = **105** |
| Daily total | ~735 | **~420** |
| Monthly | ~22,000 (over a 20k plan) | **~12,800** (comfortably inside) |

The evening scheduled run uses `--force-odds` so the first real pull always lands rather
than being skipped by a stale throttle timestamp; manual runs after 5 PM stay throttled so
repeated dispatches do not re-buy the same board at ~108 credits each.

`odds_meta_{date}.json` and `props_meta_{date}.json` now persist `quota_remaining` and
`quota_used` from the response headers, so burn rate is readable from the repo instead of
re-derived by hand. Check with:
`jq '{fetched_at,quota_remaining,quota_used}' data/odds_meta_*.json`

**CRITICAL**: Never let Claude run `curl https://api.the-odds-api.com/` — this costs
credits. `.claude/settings.json` denies it automatically.

### The Anthropic API — comfortable

Per scheduled run: 1 generation call + one verification call per pick returned (~8).
Measured from `picks/`, the model returns ~7–8 picks per run (~4 survive dedup as new).

| | Input tokens | Output tokens |
|---|---|---|
| Generation (system 4.0K + ~15 cards × ~550) | ~12,400 | ~12,000 |
| Verification × 8 (system 0.95K + card + rationale) | ~13,600 | ~16,000 |
| **Per run** | ~26,000 | ~28,000 |
| **Per day (×4 runs)** | ~104,000 | ~112,000 |

At Opus 4.8 pricing ($5/MTok in, $25/MTok out): **~$3.30/day, ~$100/month, ~$700/season.**
Output is ~84% of the bill, and verification is ~57% of the output. 36 calls/day is
nowhere near any rate limit — spend is the only constraint, and it is not urgent.

The AI call is skipped when no game on the slate has odds posted (early opening day, or a
failed odds fetch) — the prompt cannot produce a bet without a price, so that call was
guaranteed to return an empty picks array at full cost.

## Budget Page (`/budget/`)

Rendered by `budget.py --html` to `_site/budget/index.html`, reached by a small link in
the footer of every page rather than the nav strip (the nav's three tabs and
`_SWIPE_SCRIPT`'s `ORDER` array stay in sync at three). Marked `noindex`.

**The two APIs are reported differently because only one has a readable balance.**

| | Odds API | Anthropic |
|---|---|---|
| Source | `x-requests-remaining` header — measured | `response.usage` × published rates — self-metered |
| "Remaining" means | real quota | spend against `CLAUDE_MONTHLY_BUDGET_USD`, an operator-chosen ceiling |
| Sees other usage on the key? | yes | **no** — this project's calls only |

**Anthropic spend can be authoritative — it just needs a second credential.** Setting
`ANTHROPIC_ADMIN_KEY` switches the headline figure to `GET /v1/organizations/cost_report`,
which returns real USD and reconciles with billing: it also captures Workbench usage,
anything else sharing the key, server-tool costs, and any model whose rate `usage.PRICING`
has wrong. That is an **Admin key** (`sk-ant-admin01-…`), *not* `ANTHROPIC_API_KEY`, and
the Admin API needs an organization rather than an individual account (Console → Settings
→ Organization). Absent or invalid, the page logs and falls back to the ledger — it never
fails the run.

**What does NOT exist either way is a remaining balance.** Both Anthropic endpoints report
spend to date; there is no budget or limit endpoint on the Console path (spend limits are
a Claude Enterprise feature). So `CLAUDE_MONTHLY_BUDGET_USD` remains a locally chosen
ceiling regardless of which spend source is in use — do not present it as an Anthropic
quota.

⚠️ The `cost_report` response shape has **not been verified against a live call** (no Admin
key available). `usage.anthropic_cost_report()` sums any `amount`/`cost`/`value` field it
finds recursively and treats the total as cents, which degrades to a wrong-ish number
rather than a crash if the layout differs. Verify against one real response before
trusting the figure. Rates for the fallback live in `usage.PRICING`; keep them in step
with the model `suggestions.py` actually calls.

`usage/{YYYY-MM}.json` is **git-tracked**, for the same reason `history/` and `picks/`
are: `data/` is wiped between runs, so a ledger there would be forgotten at the next
checkout. `_stage()` and `_restore()` in `publish.yml` both cover `usage/`.

Odds credits consumed per day are computed as **first reading − low-water mark**, not
from the `used` counter, so a mid-month billing reset shows up as a new baseline instead
of one enormous negative day.

## CI Cache — the run-scoped key is load-bearing

`actions/cache` **never overwrites an existing key**. It logs
`Cache hit occurred on the primary key, not saving cache` and discards whatever the run
downloaded. With the old date-only key (`odds-${CACHE_DATE}`) that froze `data/` at
whatever the *first* run of the day happened to save — on 2026-08-08 a sparse 83 KB
snapshot. Every later run restored the sparse copy: the 6:30 PM run downloaded
tomorrow's Handigraphs files, used them in-job, and threw them away.

That is invisible until something re-renders without downloading. **Push-triggered runs
do exactly that** — `Clean stale data files`, `Download data` and `Download tomorrow's
data` are all gated on `schedule`/`workflow_dispatch`, but the HTML steps and the deploy
are not. So every push rebuilt `/tomorrow/` from a data dir with no tomorrow files and
deployed it: 15 game cards, every stat `?` or "No data". Cloudflare Pages deploys are
full-snapshot replacements, so a good page from the scheduled run was overwritten by an
empty one from the next push.

The key is now `odds-${CACHE_DATE}-${github.run_id}` with `restore-keys` falling back
through `odds-${CACHE_DATE}-` then `odds-`. Every run saves; every run restores the
newest. **Do not collapse it back to a date-only key.**

**Push runs now refresh Handigraphs data themselves.** `Download data` and `Download
tomorrow's data` run on every event; on a `push` they pass `--no-odds`, which fetches
every Handigraphs endpoint and never touches the Odds API. Handigraphs is a flat
subscription so this is free, while the Odds API is metered per market — odds continue
to come from the restored cache and are only re-fetched on `schedule`/`workflow_dispatch`.
That is what stops a push from clobbering good pages with stat-less ones.

The AI and picks steps (`Clear stale suggestions`, `Generate AI suggestions`,
`Save AI picks`) stay gated on `schedule`/`workflow_dispatch`. **Do not un-gate them** —
a push must never mint new picks, or the immutability guarantee goes with it.

Belt and braces: the `Generate tomorrow HTML` step checks for
`data/starters_last3g_tomorrow_${TOMORROW_DATE}.json` first and, if it is missing,
curls the live page into `_site/tomorrow/index.html` rather than rebuilding it empty.
The today page needs no such guard — `data/starters_last3g_today_*.json` is git-tracked
(the one `.gitignore` exception), which is why today survived while tomorrow did not.

## Secrets — where they can leak

The repo is private and no secret value appears in any commit or tracked file (checked
by searching git history for each literal `.env` value). The deployed pages are clean.
Two things to keep that way:

- **`download.py` builds the Odds API URL with `?apiKey=…` in the query string**, and
  `requests` quotes the full URL in connection errors — so printing a bare exception put
  a live key in the run log. Every such print goes through `_redact()` now. GitHub masks
  registered secrets, but masking only matches the literal value: it does not survive
  URL-encoding or truncation and does nothing when the script runs locally. **Any new
  print of an Odds API exception or response body must use `_redact()`.**
- **`_site/` is deployed publicly.** The budget page deliberately contains no credential
  — but it does disclose operational figures (quota remaining, monthly spend). That is a
  judgement call, not an accident; if that should not be public, gate `/budget/` behind
  Cloudflare Access rather than removing the numbers.

## Early-Season Robustness

The whole pipeline is exercised against a synthetic opening-day slate: one game, both
starters making their first start (no last-3 stats), no team stats, no bullpen, no odds,
no history, no prior picks. It renders without error — `analyze_game` returns
`TOSS-UP / no clear edge`, `ou_trends` returns `None` below its sample floors
(`_OU_MIN_TEAM = 3`, `_OU_MIN_PITCHER = 2`), `_team_trends` returns `None` on an empty
record, and the results page reports "no graded picks" rather than dividing by zero.

Two guards keep it cheap as well as safe: no odds → no AI call, and the prompt's own
"NEVER bet a pitcher marked NO STATS" rule means an early-April slate correctly produces
zero picks rather than noise.

Re-run that check after touching `analysis.py` or `suggestions.py`:

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from datetime import date
from analysis import analyze_game, build_games
from render_html import render_html_page
s=[{'Name':'A','Team':'NYY','Opponent':'BOS','Throws':'R','game_number':1,'mlbam_id':1},
   {'Name':'B','Team':'BOS','Opponent':'NYY','Throws':'L','game_number':1,'mlbam_id':2}]
p1,p2=build_games(s)[0]; g=analyze_game(p1,p2,{},{},{},{},{},date(2027,3,25))
g['odds']=None; g['game_time_utc']=''; g['away_ou']=g['home_ou']=None
print(len(render_html_page([g],date(2027,3,25),'x',slot='today')),'bytes OK')"
```
