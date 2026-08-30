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
| Handigraphs API | JWT Bearer (login → token) | Starters (last 3), team offense stats (L6RHP/LHP primary + L12RHP/LHP for the comparison wRC+, plus unsplit L6G/L12G for bullpen games), bullpen stats (last 12), ballpark weather |
| MLB Stats API | None (free) | Home/away determination, venue name, pitcher game logs, posted lineups + bat sides (`get_lineups`/`get_bat_sides`), home plate umpire (recorded to `history/`, not on the card — see Adversarial Review) |
| The Odds API | API key (query param) | Full-game ML/spread/total + F5 ML/spread/total + team totals + pitcher K/outs props for DK, FanDuel, Fanatics. Paid plan (~20K credits/month); billed per market × region, **not** per call — see API Budget |
| Anthropic API | API key (`ANTHROPIC_API_KEY`) | Claude Opus 5 for AI Picks — ONE generation call per odds refresh, cached to `data/suggestions_{date}.json`. The per-pick audit call was removed 2026-08-30 |

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

**`season.py`**: `ET`, `GAME_TYPES`, `today_et()`, `game_count()`, `has_games()`, `season_start()`, `resolve_cli_date()`

**`venues.py`**: `home_venue_ids()`, `venue_geo()`, `coords_are_sane()`, `roof_kind()`, `ROOFED`

**`usage.py`**: `record_claude()`, `record_odds()`, `load_days()`, `cost_usd()`, `PRICING`, `monthly_budget_usd()`

**`budget.py`**: `collect()`, `render_budget_page()`

**`teams.py`**: `_STATS_MAP`, `_MLB_MAP`, `ODDS_TEAM`, `MLB_NAME_TO_CODE`, `MLB_ID_TO_CODE`, `to_stats()`, `to_mlb()`, `from_mlb_id()`, `normalize_name()`, `logo_img()`, `_LOGO`

**`odds.py`**: `load_odds()`, `get_game_odds()`, `best_outcome()`, `fmt_ml()`, `fmt_spread()`, `fmt_total()`, `fmt_k_line()`, `fmt_outs_line()`, `price_from()`, `point_from()`, `implied_prob()`, `no_vig_pair()`

**`loaders.py`**: `load_starters()`, `load_team_stats()`, `load_bullpen()`, `load_ballpark_weather()`, `load_odds_meta()`, `load_pitcher_props()`

**`mlb_api.py`**: `get_mlb_schedule()`, `get_recent_starts()`, `get_pitch_hands()`, `get_team_schedule()`, `get_bullpen_stress()`, `get_weather()` (takes lat/lon, not a team code), `get_lineups()`, `get_bat_sides()`, `wind_effect()`, `compass_point()`, `stress_label_cls()`, `HAS_REQUESTS`

**`analysis.py`**: `analyze_game()`, `build_games()`, `resolve_pitchers()`, `pitcher_ids_to_check()`, `pitcher_workload()`, `is_short_arm()`, `is_bulk_arm()`, `pitched_too_recently()`, `outing_ip()`, `has_sp_stats()`, `flt()`, `fp1()`, `fp3()`, `wrc_label()`, `xera_label()`, `pitcher_csv_flags()`, `bullpen_flags()`, `weather_flags()`, `pitcher_history_flags()`, `team_whiff_k_flag()`, `pitcher_whiff_k_flag()`, `extract_outings()`

**`render_terminal.py`**: `print_game()`, `bold()`, `cyan()`, `yellow()`, `dim()`, `use_color` (set to False for --no-color)

**`suggestions.py`**: `generate_suggestions()`, `_validate_pick()`, `_render_suggestions_html()`, `_ai_game_map()`, `_pick_dom_id()`, `_pick_summary_title()`

**`render_html.py`**: `render_html_page()`, `_html_game()`

**`results.py`**: `unit_pnl()`, `summarize()`, `load_picks_range()`, `build_windows()`, `render_results_page()`

## Data Flow

**`download.py`** saves files to `data/` → **`handicap.py`** loads them via `loaders.py` and `odds.py` → `analysis.py` builds game dicts → `render_terminal.py` or `render_html.py` renders output.

Run locally: `python3 handicap.py` (terminal) or `python3 handicap.py --html > out.html`

## Data Files (in `data/`)
- `starters_last3g_{slot}_{date}.json`
- `team_stats_L6RHP_{date}.json` / `team_stats_L6LHP_{date}.json` — **primary** team offense window
- `team_stats_L12RHP_{date}.json` / `team_stats_L12LHP_{date}.json` — longer window; only its wRC+ is used
- `team_stats_L6G_{date}.json` / `team_stats_L12G_{date}.json` — the same two windows **unsplit**; read only on bullpen games (see Openers and Bullpen Games)
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
(git-tracked log of picks rejected before publication — mechanical rejections only
since the audit pass was removed, for prompt tuning).

## Openers and Bullpen Games

MLB's `probablePitcher` and the Handigraphs starters feed disagree for two unrelated
reasons, and the old `validate_pitchers()` collapsed both into "Handigraphs is stale":

- **An opener.** MLB lists the reliever who throws the first inning or two; Handigraphs
  lists the arm expected to carry the bulk. Handigraphs is the one to believe — the bulk
  arm's rate stats describe most of the game, and it is HIS hand the lineup mostly bats
  against.
- **A genuinely stale row.** The same two clubs played yesterday and Handigraphs is still
  holding yesterday's starter.

Blanking the row on both is what produced the empty offense cards: a row with no hand has
no platoon split, and `_off()` returned `None` rather than a card. Three separate holes
fed that, and all three are now closed:

1. `resolve_pitchers()` (analysis.py) tells the cases apart from **recent workload**, not
   from the ids differing. `OPENER_IP = 2.5` / `BULK_IP = 3.0` / `REAL_START_IP = 4.0`,
   over the last `_ROLE_LOOKBACK = 5` appearances, with appearances on or after the target
   date excluded (a re-run of a played day would otherwise let an opener's completed
   1.0 IP count toward his own profile). Staleness is now tested **directly** —
   `pitched_too_recently()` asks whether the Handigraphs pitcher made a real start inside
   the last 3 days and so cannot be starting again — instead of being inferred from a
   disagreement.
2. `get_pitch_hands()` (mlb_api.py) batch-fetches `pitchHand` for every probable in **one**
   `/people` call. The schedule's `probablePitcher` hydrate carries only id/fullName/link,
   so a probable with no Handigraphs row otherwise has no hand anywhere.
3. `from_mlb_id()` (teams.py) recovers the club for a Handigraphs row whose `team` is null.
   The feed nulls `team` and `mlbam_id` for a pitcher it has no last-3 data on (a debut, a
   callup) but still sends the numeric `teamId`. Those rows were being dropped by
   `build_games()` — which blanked the **opposing** club's offense card, since the dropped
   row was carrying the hand. This is not rare: it hit roughly one day in three over
   June–August 2026.

Each side resolves to a `Mode`, carried onto the SP card as `sp["mode"]`:

| Mode | When | What the card shows |
|---|---|---|
| `starter` | ids agree, or MLB's probable profiles as a real starter | unchanged |
| `opener` | MLB's probable is a short arm **and** Handigraphs' is a bulk arm | the **bulk arm's** stats and hand; opener named in the sub-caption; offense split on the bulk arm's hand |
| `bullpen` | neither side's arm profiles as carrying a game | offense read **unsplit** (L6G/L12G); starter props and F5 reads flagged as not applying |

A bullpen game is also detected when the two sources **agree** — a club whose only listed
arm does not go deep is running a bullpen game no matter who is credited with the start.

On a bullpen game where Handigraphs' pick has no stats at all, the card is headed by MLB's
listed first arm instead: a bare name from a feed with no data on him is less use than the
pitcher actually taking the ball.

**Both new files degrade independently.** A date rendered before `L6G`/`L12G` were being
downloaded has no unsplit pool, so a bullpen game falls back to the first arm's hand split
rather than rendering an empty card.

**`resolve_pitchers()` makes no network calls.** Game logs are fetched by `handicap.py`
into a slate-wide `sp_logs` cache *before* the resolver runs, and handed in — the module
order in this file forbids analysis.py reaching for data itself.

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
3. **Matchup · SP Last 3 / Team Last 6/12** (open) — SP card: xERA, ERA, K%, Whiff% visible; HH%, Barrel%, IP/gs, H/gs, PC/gs, BB% behind "More Stats"; Offense card: two group headers "L6" and "L12", each with wRC+, K%, Whiff%, HH% for that window vs starter hand; outing table per SP. The SP card also carries a `BULK` or `BULLPEN GAME` badge and a one-line sub-caption when the game is not a conventional start, and the offense card is headed "all hands" instead of "vs RHP"/"vs LHP" on a bullpen game
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
> `scripts/review_rejections.py`. They were not — that script read `rejections/`, and it
> has since been removed (see "The weekly prompt review is removed" below).

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

**The retry path's `git pull` had never worked, and was fixed on 2026-08-30.** It read
`git pull origin main`, which carries two faults. A bare `git pull` is *fatal* on git
≥2.27 when the branches have diverged and no `pull.rebase` is set — "Need to specify how
to reconcile divergent branches" — and divergence is exactly the case the retry exists to
handle, so the concurrent-push recovery documented above had never once recovered
anything. Separately, `main` was hardcoded, so a run on any other branch pulled main into
it. It is `git pull --ff-only origin "${{ github.ref_name }}"` now: after the preceding
`git reset HEAD~1` our HEAD is the commit the run started from, so upstream can only be
ahead and a fast-forward is the only sound reconciliation — with `--ff-only` failing
loudly if that assumption ever breaks instead of inventing a merge.

It surfaced when a push to the branch landed mid-run, which is the same shape as two
scheduled runs racing. The `concurrency: publish` group makes that rarer, not impossible:
a push from a person or another workflow is not in the group.

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

## The Schedule Is Not Reliable — the hourly catch-up

GitHub Actions `schedule` is best-effort, and for this repo it degraded badly at the end
of August 2026. The cron is unchanged at `30 7`, `0 14`, `0 20`, `30 22` UTC, but the
deliveries stopped landing anywhere near those slots:

| ET day | actual deliveries (UTC) |
|---|---|
| 2026-08-26 | 08:07, 14:32, 22:39 — roughly on time |
| 2026-08-27 | 03:28, 18:21, 23:17 |
| 2026-08-28 | 03:58, 06:14, 19:30, 23:14 |
| 2026-08-29 | 02:38, 03:48 |

Twice — 08-27 and 08-29 — nothing fired between ET midnight and mid-morning, so the site
sat on yesterday's slate until someone noticed and dispatched a run by hand. **Every run
that fired succeeded**; nothing in this repo was broken either time. Four chances a day
is simply too few when any of them can silently not happen.

**The fix is a fifth cron, `0 * * * *`, gated by a `decide` job** so that almost every
firing is a few seconds of no-op:

- Any trigger that is **not** the hourly cron — the four original slots,
  `workflow_dispatch`, `push` — passes straight through, unchanged.
- The hourly one runs the full job **only** when `history/{today_et}.json` is absent from
  the repo. That file is written by the first successful run of each ET day, so its
  absence means today has not been published at all.

Three guards on that, each closing a real hole:

1. **Off-season.** `history.py` guards its write with `if records:`, so an empty slate
   writes **no file** — which means "file missing" also describes every winter day, the
   All-Star break, and a full rainout. Without a season check the catch-up would fire a
   complete run, Anthropic bill included, every hour from November to March. The gate
   calls `season.game_count()`, which returns 0 outside the season window without
   touching the network and **-1 (not 0) on a statsapi outage**, so an outage fails open
   to running rather than silently suspending the site.
2. **A time window of 3 AM – 10 PM ET.** Before the first slot there is nothing to catch
   up on and Handigraphs has not posted the slate; after 10 PM the day is over and the
   6:30 PM slot already owns tomorrow's board.
3. **`concurrency: {group: publish, cancel-in-progress: false}`.** A catch-up can now
   coincide with a late real delivery, and two concurrent runs race on the push of
   `history/` and `picks/`. The retry path in the commit step recovers from that, but not
   racing is cheaper. Queue rather than cancel — a superseded publish still needed to
   happen.

The gate derives its date from `season.today_et()` for the same reason everything else
does; a fixed offset here would disagree with the code by an hour under EST.

**This does not make the schedule reliable, it makes a dropped slot self-correcting** —
worst case the site is stale by up to an hour instead of until someone looks. If the
hourly cron itself stops being delivered, nothing here helps; that is GitHub-side and the
only remedy is an external trigger this project does not have.

## Dependencies — both pins are bounded on purpose

Two direct dependencies, in `requirements.txt`; everything else is transitive.

| | Pin | Latest on PyPI (checked 2026-08-24) | Why the ceiling |
|---|---|---|---|
| `requests` | `>=2.32.0,<3` | 2.34.2 — inside the pin | ordinary HTTP client; no 3.x exists yet |
| `anthropic` | `>=1.0.0,<2` | 1.0.0 — the floor is the latest | the SDK that generates picks |

Both pins were re-checked on 2026-08-24 with `pip index versions`: neither package has a
new major, so **nothing needed bumping**. The ceilings did not have to move.

**The ceilings are the point.** Every workflow runs `pip install -r requirements.txt`
and deploys the result, so an unbounded floor means the next major release of either
package ships to production unreviewed. **That already happened**: `anthropic` 1.0.0
was published 2026-08-20 and went live the same day under the old `>=0.112.0` floor.
It was compatible — but by luck, not by design. Verified after the fact: 25–27
successful Claude calls a day on 8/21–8/23, recorded in `usage/2026-08.json`.

`anthropic` 1.x is a narrow break and this project touches none of it — no Text
Completions, no `temperature`/`top_p`/`top_k`, no raw `output_format={...}` dict, no
`.with_raw_response`, no direct `httpx` use, no Bedrock client. All three call sites
(`suggestions.py`, one call site) passes only `model`,
`max_tokens`, `thinking`, `output_config`, `system`, `tools`, `tool_choice`, and
`messages`, none of which changed; `messages.stream()` + `get_final_message()` is
likewise unchanged. 1.x raises the Python floor to 3.10 and the workflows pin 3.11,
so that is already satisfied.

The floor stays at 1.x-era capability because `thinking={"type": "adaptive"}` and
`output_config` are load-bearing here and no release before 0.112.0 accepts them.

**To move either package to a new major:**

1. Bump the ceiling in `requirements.txt` and install it locally.
2. Re-run the checks in `## Early-Season Robustness` below, plus a render of all
   three pages, and diff them against the pre-bump output — a dependency bump must
   not change a single byte of rendered HTML.
3. Grep for the removed surface the new major names in its own `MIGRATION.md`.
4. Only then deploy.

GitHub Actions are dependencies too. Re-checked 2026-08-24 — all four are on their
current major, and none needed bumping: `actions/checkout@v7` (latest v7.0.1),
`actions/setup-python@v7` (v7.0.0), `actions/cache@v6` (v6.1.0),
`cloudflare/wrangler-action@v4` (v4.0.0). The workflows float on the major tag, so the
patch releases arrive on their own. Check with
`gh api repos/{owner}/{repo}/releases/latest`.

> ⚠️ **The model id is not a dependency and is not covered by these pins.**
> `suggestions.py` hardcodes `claude-opus-5` as of
> 2026-08-24 (previously `claude-opus-4-8`; same price, $5/$25 per MTok). Moving is a
> deliberate change, not a version bump, and `usage.PRICING` must carry a rate for
> whatever id is used — it already carries both. `PRICING` keeps the `claude-opus-4-8`
> key as the unknown-model fallback and so historical `usage/*.json` days still price
> correctly; do not delete it.
>
> **The prompt has not been re-tuned for Opus 5.** `_AI_SYSTEM_PROMPT` was written and
> calibrated against 4.8, including the selectivity language in `## Selectivity`. The swap was verified for API contract and
> output shape, not for pick quality or pick volume. Re-tune against real Opus 5
> rejections in `rejections/` once a few days have accumulated.

## AI Picks — one generation call

**The per-pick AI audit was removed on 2026-08-30.** There is now exactly ONE model call
per scheduled run: `generate_suggestions`, Claude Opus 5 with `thinking: {"type":
"adaptive"}`, one call for the whole slate.

It ran with `tool_choice: auto`, **not** forced. Forcing the tool suppresses thinking
entirely, which would defeat the point of using Opus. Verified on Opus 4.8, and
re-verified on Opus 5 on 2026-08-24 against this exact system prompt and tool schema:
`auto` returned `['thinking', 'text', 'tool_use']` and 7345 output tokens with 4 picks,
while a forced call returned a bare `['tool_use']` — no thinking block — and 2684 tokens.
The suppression is not a 4.8 quirk; do not "simplify" this to a single forced call. If the
model answers in prose instead of calling the tool, a follow-up turn re-asks with the tool
forced — the reasoning is already in context, so nothing is lost.

### What removing the auditor cost, so it is not re-litigated by accident

The audit pass worked. Over 2026-08-09..08-27 it rejected 16 picks, 6.2% of everything
submitted, and the catches were not style — they were **false statements of fact** headed
for public copy:

- a rationale pairing Baltimore's own starter against Baltimore's own lineup (the
  backwards-baseball failure this project has fought since the beginning);
- "he struck out 9 and 9 in two of those three" against a card showing 9 and 8;
- "a 2.84 ERA" against a card listing 3.18; "an 18.5 outs line" against a posted 17.5;
- DET's 18.2 Whiff% quoted as Tampa Bay's, where the real figure cut against the bet;
- "five left-handed or switch bats" against a posted lineup with four and no switch hitters.

It was removed anyway, deliberately: it was ~26 of ~30 daily calls and roughly **$48 of a
$127/month bill**, and the operator's judgement is that a weak pick is acceptable output.
**The thing that is gone is the check on factual claims, not the check on pick quality.**
If published rationales start carrying numbers that are not on the card, this is why.

What still stands between the model and the page is `_validate_pick()` — free,
deterministic, and now the ONLY automated gate:

1. price worse than the **-200 floor**;
2. a **price or line the card does not post** for that market and side (the alt ladder counts);
3. **stated edge under `MIN_EDGE_PTS`** (6.0) over the card's no-vig price;
4. the two §8 disqualifiers — a pitcher **OVER into rain risk**, and any bet on a **NO
   STATS** pitcher.

Anything it rejects is logged to `rejections/` with a `[mechanical]` prefix. Nothing reads
that log automatically any more — the weekly prompt review was removed the same day — so
it is now a record for a human to look at, not an input to anything.

**Cost after removal**, against the measured before:

| | before | after |
|---|---|---|
| calls/day | 30 | **4** |
| input tok/day | 423,000 | 268,000 |
| output tok/day | 85,000 | 52,000 |
| **$/month** | **127** | **79** |

`_CARD_FORMAT` remains a named constant even though only one prompt now interpolates it —
it describes the card rather than the analysis, and the next thing that reads a card
should reuse it rather than re-describe it.

**The card must name each starter's club, because nothing else does.** `_sp_line()` used
to print `  Dean Kremer (R): xERA …` with no team — the offense, bullpen and trend lines
were all team-labelled, but the pitcher block was positional only, so which club a starter
threw for had to be inferred from list order (away first, home second). That inference
loses to the model's own prior the moment a starter is traded mid-season. On 2026-08-10,
Kremer had moved BAL → MIN; the data was correct end to end (`team: MIN`, `opponent: BAL`,
`analyze_game` put him on the home side) and the published rationale still had him facing
Minnesota — the matchup inverted, with a strikeout rate attributed to the wrong lineup.
Lines now read `MIN (home) — Dean Kremer (R): …`, the props line carries the team too, and
`_CARD_FORMAT` says the card's club is authoritative over prior knowledge.

Note the failure was invisible to the audit pass (while it existed) for the same reason it
was invisible to generation: the verifier was re-sent the *same card*, so an unlabelled
pitcher block gave it no way to catch the swap either. That is the general lesson, and it
matters more now that no audit pass exists at all — anything the model must not get wrong
has to be **on the
card** — a second opinion over identical data does not add a fact.

**Rationales are public-facing copy, not a reasoning transcript.** Section 10 of
`_AI_SYSTEM_PROMPT` (and the `reason` / `pass_reasons` tool-schema descriptions) forbid
leaking the prompt's own rules into the output: no "Tier 1"/"per the rules", no reminders
of methodology ("their own starter's ERA doesn't affect how they hit"), no self-correction
or process narration ("Calibrating:", "Note:", "that is irrelevant here"), no defending
stats the model chose not to use. This was never enforced downstream — style is not worth
dropping an otherwise sound pick — so it lives at generation time and nowhere else.

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

## Selectivity — demanding numbers and pick volume

Added 2026-08-23 after a P&L review of every graded pick from 2026-06-25 to 2026-08-23
(619 graded, `picks/` plus `picks/archive/`). The complaint being addressed was volume:
the model was making 10-15 picks a day on ~15-game slates, i.e. a bet on well over half
the board, which is not what "find mispriced numbers" produces.

**Read the two eras separately.** `picks/archive/` is the pre-rewrite prompt (-5.7% ROI
over 435 picks); `picks/` from 2026-08-08 on is the current one (+5.9% over 184). Several
of the archive's worst leaks are already fixed and must not be "fixed" again:

| Leak | Archive | Current | Status |
|---|---|---|---|
| Games carrying 4+ picks | -34.3% ROI (n=49) | n=0 | fixed by §4 per-game caps |
| Two totals on one game | -16.8% ROI (n=119) | rare | fixed by §4 |
| F5 totals | -54.0% ROI (n=25) | n=0 | model stopped taking them |

**What is still live.** Only two findings clear noise at this sample size (t < -2.5); the
rest are directional and were encoded as *scrutiny*, not bans, on that basis:

| Pattern | n | ROI | t | Encoded as |
|---|---|---|---|---|
| Pitcher props priced -130 or worse | 20 | -49.3% | -2.77 | **rule removed same day** — see below |
| Team total OVER at 4.5+ | 92 | -16.2% | -1.57 | demanding number, §5/§6 |
| Team total OVER at 5.5+ | 26 | -31.4% | -1.66 | demanding number, §5 |
| K UNDER at 5.5+ | 10 | -43.6% | -1.51 | **not encoded** — see below |
| K OVER at 8.5+ | 8 | -21.2% | -0.55 | demanding number, §8 |

Two of the hypotheses that prompted this review did **not** survive the data and were
encoded anyway, as scrutiny, on the understanding that they are priors rather than findings:

- **K over at 7.5+** is +10.6% over the full sample (n=17). Only the 8.5+ slice loses, and
  the recent-era 7.5+ result (1-5) is six picks. The prompt flags 7.5 and reserves the
  sharper language for 8.5.
- **K under at 3.5 or lower** is the single best bucket on the board (see below).

**K unders at 5.5 or higher are a known, unencoded leak.** 3-7, -43.6%. It is tempting to
file this under the price rule — three of the ten were juiced at -134/-136/-142 — but the
other seven were priced -110 or better and lost at the same rate (-44.2%). The juice is not
what is wrong with them, so no price rule covers this bucket and nothing else in
the prompt targets it either. Left alone at n=10; it is the first thing to look at on the
next review. §8 does now point the model at 3.5-4.5 as the productive under band, which
leans against 5.5+ indirectly.

**On price — read this before re-deriving the -130 rule.** Props at -130 or worse went
6-14 (-49.3%, t=-2.77), while -101..-129 went 27-14 (+23.6%) and plus money is also
profitable. That finding briefly became a hard rule: "pitcher props at -130 or worse are a
pass." **It was removed the same day, deliberately, and must not be reinstated from the
numbers alone.**

The reason is that the finding does not mean what it looks like. All 20 of those juiced
props were the **main posted line** — the only line the system could see at the time. Paying
-140 for the number the book leads with is a genuinely bad bet. Paying -160 for a number
two rungs easier is a different bet entirely, and the data says nothing about it because
alternate ladders did not exist yet. Screening on the price alone would have banned the
strategy the ladders were added to enable.

The floor is now **-200 in every market, on main and alternate lines alike**, with the
-150..-200 band requiring the reason to say what the juice is buying. Audit check 4
enforces the floor and explicitly does *not* reject for juice inside the range. Section 5
states the principle the -130 rule got wrong: price follows the number, so a juiced price
on an easy number can beat a cheap price on a hard one, and what disqualifies a bet is the
price being wrong for the chance — never the price being high.

When alt-line picks have accumulated, re-run the analysis **split by main vs alternate
line**. If juiced alternates lose the way juiced main lines did, that is a real finding;
the current pooled number is not.

**Demanding numbers (§5, "THE NUMBER ITSELF IS EVIDENCE").** Team total over 4.5+ **and
under 3.5-**, K over 7.5+, K under 3.5-, outs over 18.5+ **and under 14.5-**, game total
over 9.5+ / under 7.5-. **The list is symmetric as of 2026-08-24 and must stay symmetric**
— see "Adversarial Review — 2026-08-24" below for why the earlier one-sided version pushed
the picks under. These are a
**scrutiny prompt, not a filter, not a gate, and not a disqualifier** — the section says so
in those words, and it must keep saying so. They are the numbers where "I like this side"
most often passes itself off as value, so the prompt asks three questions before one gets
recommended (is the edge in the number's direction; does the recent box-score record
actually clear it; does the published reason name what clears it) and points at the cheaper
line carrying the same read as the usual alternative to passing.

> **Do not turn these into hard filters** — not in the prompt's wording and not in code
> (`_validate_pick` is now the only place a filter could live, which makes this easier to
> violate by accident, not harder). The samples are 8-26 picks wide, and a ban would have
> thrown out the K over 6.5 bucket's +33.8% along with the 8.5 bucket's losses. This was
> tightened once during the original change and walked back deliberately: an earlier draft
> made the three questions a checklist ("ALL THREE must hold") and had the audit pass
> REJECT any demanding number argued only directionally. That is a hard stop wearing
> scrutiny's
> clothes: a mechanical check is a binary, so *any* size-based reject rule is a ban.
> **Audit check 8 (was 9) is scoped to self-contradiction only**: a rationale whose own cited
> evidence points away from the bet (a K over sold on a streak built against higher-K
> lineups than today's), or one that concedes the number is a stretch and bets it anyway.
> It explicitly instructs ACCEPT for a coherent case at a demanding number. Keep it there.

**The K under threshold is a deliberate exception to the evidence.** K unders at 3.5 or
lower are historically the *best* bucket on the board (12 picks, +34.9%; 11 picks and
+47.2% in the current era). They are in the demanding-number list anyway, because a low
number means the market has already forecast a quiet outing and the bucket is thin enough
that it could be noise. §8 says this explicitly — the instruction is to confirm the price
still pays, not to avoid the bet. If this bucket keeps winning, take it back out.

**Volume is addressed in §4 by an EDGE THRESHOLD, not a game-count target** (changed
2026-08-24). Two picks resting on one underlying read still count as one opinion sold
twice. There is still no hard slate limit — the right number of picks on a slate is a
function of the slate — but every pick must now clear the card's own no-vig price by at
least 4 percentage points, stated in `win_probability` and `market_probability` and
enforced in `suggestions._validate_pick`, not asserted.

The sentence this replaced ("touch fewer than half the games") was measured and did not
hold: over 17 slates the model touched 6-10 games of ~15, every day. A quota you cannot
check one pick against does not bind; a per-pick threshold does, and it limits volume as a
consequence rather than as an instruction.

**Re-run the review before changing any of this.** The analysis is a straight replay of
`picks/*.json` — every pick carries `bet`, `line`, `odds_num`, and `result`, so ROI by
line/side/price bucket is reproducible without touching the API. Over/under has to be
parsed out of the `bet` string for props (`team_side` is null there) and off `team_side`
for totals.

## Caps Are Not the Limiter — 2026-08-30

The per-game caps were tightened (one prop per GAME) and then loosened again the same
day, back to **one prop per PITCHER**. Both moves were about volume; only the second is
right, and the reason is worth keeping.

Capping is the wrong instrument. It cannot tell a good pick from a bad one — it just
stops the fourth one, whatever it is. The measured effect of the tighter cap was real
(15 picks → 8, and games carrying 2-3 picks went 21-of-33 → 0) but it worked by
truncation, and it pushed the output into a corner: **8 of 8 picks were pitcher props and
7 of 8 were unders**, because with F5 gone and the prop cap tight, that was most of what
was left reachable.

So the caps now say what they are for — stopping ONE read being sold through several
correlated markets — and say explicitly that a strong slate should not be trimmed to look
disciplined.

**The limiter is evidence, and §4 now tests it in the one way a self-reported number
cannot be gamed:** every pick must name the strongest thing on the card pointing the
OTHER way and say why it does not carry. A pick with nothing against it means the model
has not looked; a pick whose counter-evidence is itself heavy is a game where two real
forces disagree, which is what the price already reflects.

`MIN_EDGE_PTS` went 4 → 6 → 8 across the same day. Read that as a failure, not a policy:
at 4 the floor sat below the entire distribution (min 5.0, median 11.5); at 6 — with the
prompt already showing the model its own numbers and calling them unbelievable — the next
slate came back at median 14.5, min 7.0. **Unmoved.** 8 binds at the bottom of the
observed distribution rather than below it, but it is arms-racing a number the model
writes itself. Watch the *distribution*: if the median climbs to track the floor, the
floor is being gamed and raising it again will not help.

## Evidence, Not Tiers — 2026-08-30

`_AI_SYSTEM_PROMPT` §2 used to be a three-tier hierarchy: Tier 1 "decides the game",
Tier 3 was "never the reason for a bet", head-to-head "cannot be the case", and §9 said
weather "cannot create a bet". That framing was wrong about how handicapping works. It is
now a **weighted-evidence** model, and every "Tier N" reference in both prompts is gone.

- **Everything on the card is evidence.** What differs is WEIGHT, not permission to count.
  HEAVIEST is starter xERA and opposing wRC+; MODERATE is starter ERA, head-to-head, box
  scores, bullpen, team trends and the over/under block; LIGHT is weather, park, flags and
  situational trends.
- **A pick is a balance, not a hierarchy** — evidence for against evidence against. A
  single heavy signal with several arguing back is a game you understand, not a mispriced
  one.
- **Light evidence accumulates and can form part of a real case.** A hot day in a hitter's
  park with the wind out, behind two short starters and a tired bullpen, is a genuine
  argument for runs with nothing heavy driving it.

**The guard that makes accumulation honest is the independence rule, and it must stay.**
Park factor, temperature and wind are three readings of ONE thing — the run environment —
and citing all three is one piece of evidence, not three. §2 says so explicitly, §9
repeats it for weather, and the test given is: ask what would have to be true for the
stack to be wrong, and if one answer knocks all of it down, it is one signal. Without
that, "weak signals add up" is a licence to manufacture a case from restatements.

**No mechanical correlation check exists, and adding one would be a mistake.** Whether
several signals are independent or one fact restated is a judgement about a specific
game; a rule that tried to enforce it would become a hard filter on case *shape* — the
same trap this file already records for the demanding-number list. The independence rule
lives in the prompt, where it can be applied with judgement, and nowhere else. (This was
written when the AI auditor still existed and would have been the place to put such a
check; the reasoning survives the auditor's removal.)

## Volume — the edge threshold was never binding

Measured 2026-08-30 over the four slates carrying both probability fields (57 picks):

| | |
|---|---|
| picks/day | **14.2** |
| games touched/day | 4–13 of a ~15-20 game slate |
| **touched games carrying 2-3 picks** | **21 of 33 — 64%** |
| props as a share of picks | 72% (Ks 44%, outs 28%) |
| smallest stated edge | **5.0 points** |
| median stated edge | **11.5 points** |

Two things follow, and both were acted on.

**The 4-point minimum edge never rejected anything.** The floor sat below the entire
distribution — the smallest submitted edge was 5.0. `MIN_EDGE_PTS` is 6.0 now, but raising
a *self-reported* number is only half a fix: the model can inflate past any threshold. So
§4 also tells it what its own distribution looked like, points out that a median 11.5-point
edge against a liquid market is not believable, and asks it to derive `win_probability`
from the game BEFORE checking it against the bar. Re-measure the distribution after a week:
if the median is still double digits, the estimates are the problem, not the market.

**Multiple picks per game was the volume, and §4C was the multiplier.** The prompt said
filling slots "should be rare"; it was the majority case. §4C allowed one prop per
PITCHER, so both starters could carry one — and props are 72% of all picks. It is now
**one prop per GAME**. §4 also states the measured 64% figure, because "rare" as an
adjective did not bind and a number might.

`_validate_pick` enforces the 6-point floor mechanically. Since the audit pass was
removed it is the only automated gate left, so a check that can be expressed
deterministically belongs there rather than in the prompt.

## A malformed tool field took down a publish — 2026-08-30

Run 402 failed with `AttributeError: 'str' object has no attribute 'items'` in
`_ai_game_map`: the model returned `pass_reasons` as a bare **string** where the schema
declares an object. The tool is not `strict`, so the API accepted it.

`strict: true` is not the fix — it requires `additionalProperties: false`, and
`pass_reasons` deliberately uses `additionalProperties` as an open `{game: reason}` string
map. Instead `_normalize_tool_result()` now coerces the tool output at the point it is
parsed (bad `picks` → `[]`, bad `pass_reasons` → `{}`, non-dict result → both empty), and
`_ai_game_map` re-checks the type because a `suggestions_{date}.json` cached before this
fix can still hold the bad value.

**The step ordering did its job.** Picks were committed at 13:22 and the HTML step then
died, so the run left a pick log with no page rather than publishing something
unreproducible — the last good deploy (run 401, 13:14) stayed up. That is the invariant
`publish.yml` is ordered for; the stranded picks render on the next successful run.

## Adversarial Review — 2026-08-24

A red-team pass over `_AI_SYSTEM_PROMPT`, `_VERIFY_SYSTEM_PROMPT`, `_serialize_game_for_ai`
and the flag generators in `analysis.py`, read against all 200 picks logged since the
rewrite (196 graded, 189 priced, +5.3% ROI). Everything `CLAUDE.md` already recorded as
known and deliberate was excluded. What it found and what changed:

### The flags were giving betting advice — this was the biggest one

`analysis.py` wrote directional verdicts into the card, and a card line beats a prompt
rule because it is specific to the game while the rule is generic:

| Flag | Tail that was removed |
|---|---|
| bullpen stress | "— manager likely leaves starter in longer; lean SP K/outs OVER" / "...may hook starter early; lean SP K/outs UNDER" |
| park factor | "— park boosts offense, favor the over and HR props" / "— park suppresses offense, favor the under" |
| wind | "— supports the over and HR props" / "— suppresses the over" |
| divisional | "— the underdog can carry a bit more value than the price suggests" |
| swept / shut out / 1-0 | "classic bounce-back spot", "due for some offensive regression", "extra motivation to avoid it" |
| whiff-vs-K gap | "— strikeout rate looks due for positive regression (fewer Ks)" |

The stress one is the mechanism behind the outs skew the §8 rewrite was aimed at and did
not fix: §8 now says both sides of an outs prop are live, and the card went on saying
"lean UNDER" next to the numbers. Outs picks after that rewrite ran **9 unders to 1 over**.
The park-factor one contradicted §9 in as many words ("ordinary hitter-friendly or
pitcher-friendly labels are not a reason for anything").

**The rule now is: flags state facts, the prompt assigns weight.** `CLAUDE.md` already
said anything the model must not get wrong has to be on the card. The converse holds just
as hard — anything the card asserts, the model will act on. Do not put a lean, a
recommendation, or a "due for" back into a flag string.

### Wording that leaned, and the picks that followed it

| Finding | Evidence | Change |
|---|---|---|
| §6's only worked totals example was an under; the over appeared only as the "pass" case | game totals 11 U / 2 O | a matched over example; both framed as distance from the posted number |
| Demanding numbers gated overs and left unders open (no team-total-under entry, no outs-under entry, game-total under threshold 1.5 runs off the mean vs 1.0 for the over) | all 13 game-total picks and 9 of 10 outs picks landed inside the ungated corridor | list symmetrised; the asymmetry is called out in the prompt so absence stops reading as permission |
| §6 carried the one bare directional preference in the prompt ("the under side of a team total is passed over too often") | team-total overs were still the most common bet type, 31 to 22 — it bought nothing | cut |
| §11 pointed one way in all five bullets | sides 22 away / 10 home, F5 ML 4-0 away | rewritten: mirror cases added, weight dropped below Tier 2, "due" reasoning named as the fallacy it is |
| §8A's ladder was being climbed toward plus money, the opposite of its purpose | median price +100; **zero** of 193 picks worse than -155 against a -200 floor; team-total overs 18 at 4.5 vs 10 at ≤3.5 | `rung_rejected` is now a schema field — the pick must name the rung it turned down |

### Numbers that were computed and never reached the model

Temperature, humidity and elevation were fetched, rendered on the HTML weather badge, and
dropped from the AI card — every game serialized as "Clear/Calm". Bullpen stress reached
the card only through the flag above, which fired on three of five labels, so on **56% of
team-games** (Normal, or no recent games) §2 and §8 asked the model to weigh something with
no line on the card at all. Also added: bullpen K%/BB%/HH%, starter K-BB%, the
`ou_trends()` over/under block (computed and rendered since forever, never serialized), the
season head-to-head line, and no-vig probabilities on every two-sided market.

### Concepts that changed weight

- **Head-to-head is Tier 2 now, not Tier 1.** Up to three starts against one club is
  roughly 60-80 plate appearances across a roster that has turned over. The prompt had it
  outranking xERA and wRC+ on conflict, *and* the auditor was explicitly told to ACCEPT
  picks resting primarily on it — so the thinnest evidence on the card was the one thing
  the audit could not question. Both are gone. The per-meeting box scores stay: they show
  HOW he pitched, which the average destroys.
- **The xERA/ERA noise floor went from 0.75 to 1.50.** Three starts is ~18 IP, a smaller
  sample than the six games §1 spends forty lines being careful about, and it carries no
  longer window to check against. §1 says that out loud now.
- **"The market may be pricing the ERA" was cut.** It asserts a naive book as the standing
  mechanism for a whole class of bets. The inference worth keeping is about the pitcher.
- **League baselines are on the card** (§1), measured from `history/` over 1,589 games:
  8.9 mean game total, 4.4 runs per club, a club scores 5+ 42% of the time and 3 or fewer
  30% of the time. The model had no anchor for "is 4.5 high" and was supplying one from
  its prior — the exact thing §1 forbids for every other number.

### Mechanical checks moved from the prompt into code

`suggestions._validate_pick()` runs before the paid audit call and rejects deterministically:

1. price worse than the **-200 floor**;
2. a **price or line the card does not post** for that market and side (the alt ladder
   counts as posted);
3. **stated edge under 4 points** over the card's no-vig price;
4. the two §8 disqualifiers — a pitcher **OVER into rain risk**, and any bet on a **NO
   STATS** pitcher.

None of these needed a model. The floor used to be a sentence in two prompts with nothing
comparing a submitted price against the card, and the rejection log has the failure in it
(a pick citing "an 18.5 outs line" against a card posting 17.5). Audit checks 4 and 6 were
removed as redundant and the remaining checks renumbered **1-8**; the audit call now spends
its tokens only on reasoning. Mechanical rejections are logged to `rejections/` with a
`[mechanical]` prefix so the two causes could be told apart. (Moot since 2026-08-30:
the audit pass is gone, so every rejection is mechanical.)

### Umpires — measured, not shipped

The audit called the home plate umpire the largest missing input. It was tested before
being built, and **the number does not survive the test**, so no umpire figure is on the
card:

| Metric | Sample | Observed sd of umpire means | Null (shuffled) sd | p95 of null |
|---|---|---|---|---|
| Runs/game | 1,965 games, 82 umpires with 15+ | 1.034 | 0.947 | 1.065 |
| Starter Ks | 1,356 outings, only 6 umpires with 20+ | 0.433 | 0.487 | 0.778 |

Both sit **inside** the noise band — the spread across umpires is what random assignment
produces. The metric that would work is called-strike rate on a real zone, which needs
pitch-level data this project does not have. Putting a name on the card without a tendency
is worse than nothing: it invites the model to supply a prior, which is what §1 exists to
stop.

What *was* built: `history.py` now records `hp_umpire` on every graded record (free — it
rides the schedule call the annotate pass already makes), so a ledger accrues. Re-run the
test above before surfacing anything. One `/schedule` call covers a 23-day range with
`hydrate=officials,linescore`, so the backfill is a handful of requests, not 150.

### Posted lineups

`mlb_api.get_lineups()` + `get_bat_sides()`, two calls per slate, both free. MLB posts
lineups 1-2 hours before first pitch, so the early runs get nothing and **the card says so
explicitly** rather than going silent — the absence must not read as information. Where a
lineup exists the card prints the batting order with each hitter's bat side and a count of
how many carry the platoon advantage against today's starter. Switch hitters count on the
advantage side.

This does not fix the underlying gap — every offense number on the card is a team-level
last-6 that is blind to who is in the box, and no per-player offense data reaches this
project — but it is the one place a missing regular is visible at all.

### Section balance — the prompt's shape was part of the bias

Attention in the prompt maps onto the output, so section length is a design decision, not
an accident. Measured before and after:

| | Before | After |
|---|---|---|
| §7 Sides | 1,001 chars | 3,312 |
| §6 Totals | 2,657 | 2,657 |
| §8 Pitcher props | 8,824 | 9,981 |
| — strikeouts within §8 | 6,414 | 6,414 |
| — outs within §8 | 2,091 | 3,248 |
| — K:outs ratio | 3.07x | **1.97x** |

§7 was by far the thinnest market section and sides carried the worst ROI in the log
(spreads -41%, F5 ML -100%, both on small n). It now covers the two things that actually
make a side hard — the ladder cannot help you, and baseball sides are compressed so a big
run-differential edge converts into a small win-probability one — plus the run-line and
take-the-dog cases. §8's outs block gained the matched pair of demanding numbers (over
18.5+, under 14.5-) that §5 now lists, which is both the symmetry fix and the rebalance.

Re-measure with `parts = _AI_SYSTEM_PROMPT.split("═"*67)` when editing; a section that
grows by a third has quietly changed what the model reaches for.

### Still open

- **No CLV.** The whole thesis is "this number is wrong" and the only feedback is win/loss,
  which needs ~1,000 bets to say anything. `history/` overwrites its odds each run until
  first pitch, so it holds a near-closing line but not an opening one. Keeping the first
  snapshot alongside the last would make CLV computable at zero API cost, and it is the
  single best measurement this project could add.
- **No team defense.** xERA deliberately strips defense out, so a good-xERA arm in front of
  a bad glove is systematically overrated by the card's own Tier 1 input.
- **`projection` / `win_probability` are new and ungraded.** Once a few weeks have
  accumulated, regress realised outcomes on the stated projections — that is a far better
  calibration signal than W-L, and it is the reason those fields exist.

## Alternate Lines — the ladder

Added 2026-08-23. Strikeout props and team totals now arrive as a **ladder**: the same bet
priced at every number the book offers, not just the one it leads with. `odds.alt_ladder()`
walks `pitcher_strikeouts_alternate` / `alternate_team_totals`, keeping the best price per
(point, side) across books; `merge_main_rung()` folds the main posted line in, because the
alternate feed prices *around* the main number without repeating it and a ladder missing
its own anchor gives the model nothing to compare against. `fmt_ladder()` renders one line
per subject with `*` on the main rung. Section 8A of `_AI_SYSTEM_PROMPT` is the usage rule.

**Ladders are trimmed to ±3 rungs around the main line** (`span`). Full ladders run 20+
rungs out to absurd numbers (K over 12.5 at +900) and every one of them costs card tokens
on every game of every slate. The rungs adjacent to the main number are the ones a step up
or down actually lands on. A ladder that ends up with one rung is suppressed entirely —
that is just the main line printed twice.

**The pick's own `line` now wins when grading props.** This was a live bug the moment alt
lines existed. `_resolve_pick()` preferred `k_line` from the history record and used the
pick's `line` only as a fallback — invisible while every prop was taken at the main posted
number, since the two always agreed. An alt-line pick at "Over 4.5 Ks" would have graded
against the 6.5 the books led with: a won bet recorded as a loss, with nothing anywhere
reporting an error. Team totals were always fine (they grade off `pick["line"]` directly).

**Two rungs of one ladder are one pick, and `_canon_pick_key` already enforces it** — the
key is the correlated *slot* (`(game, "pitcher", last)`, `(game, "runstotal")`) and has
never included the line, so "Over 4.5 Ks" and "Over 6.5 Ks" on the same pitcher collapse.
Do not add `line` to that key to "fix" alt lines; it would break exactly the guarantee that
makes them safe.

**Cost**: +2 credits per per-event call, ~16,000/month against a 20,000 plan. See
API Budget above before adding a third alternate market.

## Prompt Audit — 2026-08-23

A full consistency and simplification pass over `_AI_SYSTEM_PROMPT`, `_VERIFY_SYSTEM_PROMPT`,
the tool schema, and `_serialize_game_for_ai`. What it found, so the same ground is not
re-walked:

**Real defects, now fixed:**

| Defect | Effect |
|---|---|
| `odds_num` was optional in the tool schema | 7 of 197 current picks had a good `odds` string and a null `odds_num`. Nothing errored — they were silently absent from every ROI number. `picks._odds_num()` now parses the string, and the field is required. |
| At-park splits on a NEUTRAL SITE | The away starter's "at" split is his starts at the HOME club's park; the home starter's is his own home starts. On a neutral site neither describes today's venue, and both were printed under a Tier 1 heading. Now suppressed, with the neutral-site header saying so — same reasoning that already suppresses the park factor. |
| Auditor check 6 rejected *every* pitcher prop on rain risk | §8 disqualifies pitcher OVERS only; a delay shortens the outing, which supports an under. The auditor was throwing out good unders. |
| Auditor had no idea alt lines exist | Check 2 rejects figures not on the card. A pick quoting "the 2.5 over at -160" would read as an invented price. The preamble now states that the ALT LINES block is real card data and a non-main rung is normal. |
| `bet_type` was a free-text string | `picks.py` routes on exact tokens for both grading and slot dedup; a near-miss spelling grades as `None` forever rather than failing loudly. Now an enum, and every value is verified to route to a real slot. |
| `confidence` was a required enum with no guidance anywhere in the prompt | Nothing told the model what "high" meant. New §12 defines it as the strength of the *mispricing*, not the chance of winning, and makes high rare. |

**Contradictions introduced by the alt-line and demanding-number work, now reconciled:**

- §7 routed "strong offense, weak opposing starter" straight into a team total over — the
  exact bet §5 and §6 had just been rewritten to discourage. It now hands off to §6/§8A.
- §8's absolutes ("NEVER take an over into a low-K lineup") predate ladders, which make
  "unless the number is so low it is undeniable" a thing you can now *choose*. Scoped to
  the posted line, with the ladder named as the one legitimate way around it — in the
  direction that makes the bet easier only. Auditor check 7 matches.
- The auditor's tail routed a single-meeting head-to-head case to check 2 (numbers not
  matching the card). It is a weighting failure, not a misquote; check 2's subject is
  figures that disagree with the card.

**Duplication removed** (the prompt lost ~1.7KB net despite gaining §12): the rain and
"NO STATS" disqualifiers were stated in both §8 and §9; the Whiff%/K% process-outcome gap
was explained in full in §1 and restated twice inside §8's numbered inputs; the outs-line
length rule was stated in input 2 and again under LENGTH GATES THE OVER; `alt_suggestion`
was explained in both §8 and §8A. Each now has exactly one home, with pointers.

**`_serialize_game_for_ai` odds block** was four near-identical if-blocks per market; it is
now one `_ODDS_ROWS` table. Verified byte-identical to the old code over 4,000 randomized
inputs before replacing it.

**Known, not fixed:** `picks._extract_picks()` still carries a `best_bet`/`other_bets`
branch for a schema the tool can no longer produce, so `is_best` is dead — `True` on 1 of
737 picks ever, from the pre-`picks[]` era. Left alone because old cached suggestion files
still parse through it; delete it with the archive, not on its own.

## Prop Market Bias — Ks vs outs

Measured 2026-08-23, after the AI picks looked strikeout-heavy. It was not a hunch:

| | K props | Outs props |
|---|---|---|
| Current prompt (8/08-8/23) | 91 | 10 |
| Archive (6/25-8/07) | 125 | 8 |
| **All** | **216 (92%)** | **18 (8%)** |

Within outs it is worse still — **16 unders to 2 overs, all-time**. The model treated outs
as an under-only market.

**This is not a supply problem, which was the first thing checked.** Across
`history/2026-08-*.json`, 68% of pitcher-game rows carry a K line and **65% carry an outs
line**, with 64% carrying both. When both are on the card the model took strikeouts ~92%
of the time.

**It was prompt attention.** §8 spent 5,275 characters on strikeouts and 852 on outs — a
6.2x ratio. Worse, every one of the nine sentences in the prompt containing "outs line"
sat inside the STRIKEOUTS block, framing outs as an *input* to a K prop (the length half
of rate × length) rather than a bet. The only place outs appeared as an alternative at all
was §4's hard limit — "either strikeouts OR outs, never both" — which is a restriction, not
an invitation. And the one directional hint the OUTS block gave ("if the line sits right at
his normal depth, the under is often the play") is the likely source of the 16-to-2 skew.

Three changes, all in §8:
- A **CHOOSE THE MARKET** step now opens the section, mapping the shape of the read to the
  market: bat-missing → strikeouts, length → outs, "he is good/bad today" → usually outs.
- The OUTS block was rewritten with a fifth input (BB%, since walks are what end outings)
  and an explicit **both sides are live** treatment, naming the stressed-bullpen-behind-a-
  deep-starter spot as the cleanest outs over. Ratio is now 2.8x, not 6.2x.
- "DO NOT DEFAULT TO STRIKEOUTS" says out loud that the section's own length imbalance is
  not evidence about which market is the better bet.

> Re-measure this after a few weeks. The target is not 50/50 — strikeouts may genuinely be
> the better market — but 92/8 with equal line availability was the prompt talking, not the
> board. If outs picks are still in single digits, the fix did not take.
>
> **Update 2026-08-24: it did not take, and §8 was not where the problem was.** Outs picks
> since the rewrite ran 9 unders to 1 over. The bullpen-stress flag was telling the model
> "lean SP K/outs UNDER" in imperative form on the card itself, which no §8 wording can
> outvote. That tail is now gone (see Adversarial Review). Re-measure again from
> 2026-08-24 — and if a prompt fix does not move a behaviour, check whether the card is
> arguing with it before rewriting the prompt a third time.

**Related: `pass_reasons` now have to cover the props.** A pass reason that explained why
there was no side or total and said nothing about either starter was answering half the
question, since the reader can see the posted K and outs numbers on the same card. The
prompt and the tool-schema description both now require two to three sentences covering
the game lines AND both starters' strikeouts and outs. Note §10 still applies to pass
reasons, so the explanation has to be in baseball terms — "the lineup does not strike out",
not "the signals disagreed", which is a phrase §10 explicitly bans.

**Known gap, not addressed:** `render_html.py` renders `pass_reason` only in an `elif` —
a game with *any* pick shows its picks and no pass reason at all. So a game where the model
bet the total but passed both props still explains nothing about the props. Fixing that
means either per-market pass reasons in the schema or rendering both blocks; neither is
worth doing until the fuller pass reasons above have been seen in production.

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

The CLI is `scripts/inspect_data.py` now. `review_rejections.py` (since removed) also
stripped its own directory from `sys.path` before importing anything else, and any new
script in `scripts/` that imports from the repo root should do the same — nothing here
imports a sibling by name, so the repo root is the only path such a script needs.

## PR #5's K-prop rule, ported by hand — 2026-08-30

The weekly review's one surviving output was PR #5: a §8 paragraph requiring every K prop
reason to name today's opponent's K% (last 6 vs hand) and the posted outs line, and to
confront whichever of the two argues against the bet. It was raised against a 2026-08-24
main and could not merge — §8 has been rewritten twice since — so the paragraph was ported
into the current §8 by hand and PR #5 closed.

**It matters more now than when it was written.** Audit checks 6 ("K PROP FIGHTING THE
LINEUP") and 7 ("K OVER WITH NO INNINGS TO GET THERE") enforced exactly this, and both
went with the audit pass. §8 already covered the outs half — "taking it anyway requires
saying in the reason why he goes longer than the number" — but nothing required the
opponent's K% to appear at all. With no auditor downstream, the generation prompt is the
only place that discipline can live.

## The weekly prompt review is removed — 2026-08-30

`.github/workflows/prompt-review.yml` and `scripts/review_rejections.py` are deleted.
Both are recoverable from git history if it is wanted back.

Two reasons it was not worth keeping running:

1. **It never worked.** Two runs are recorded and both failed — 2026-08-08 (the first
   dispatch) and 2026-08-10 (the first Monday cron). There is no successful run in the
   history. The stdlib-shadowing bug documented above is why the second one died.
2. **Its input mostly went away.** It read `rejections/` for reasoning failures the audit
   pass had caught. With the audit pass removed, every entry in `rejections/` is a
   mechanical rejection — a price past the floor, a line not on the card — which says
   much less about the prompt's wording.

`rejections/` is still written and still git-tracked. `_stage()` in `publish.yml` still
covers it, and the directory still only exists on days something was rejected, so the
`mkdir -p` in that function is still load-bearing.

## Adding New Data Fields
1. Check what's available: `python3 download.py --inspect`
2. Map the raw JSON key in the appropriate `_load_*_json()` in `loaders.py`
3. Add to the `_sp()` / `_bp()` / `_off()` dict inside `analyze_game()` in `analysis.py`
4. Render it in `_sp_card()` / `_bp_row()` / `_bat_card()` in `render_html.py` — these
   are module-level functions, not nested inside `_html_game()`. If the new field should
   be colour-graded, add a scale to `_SCALES` and call `_scaled_row()` rather than writing
   a new threshold ladder.
5. **Serialize it in `_serialize_game_for_ai()` in `suggestions.py`, or decide out loud
   that you are not.** Step 5 did not exist and things fell through it: temperature,
   humidity, elevation, bullpen K%/BB%/HH%, starter K-BB%, offense wOBA, the whole
   `ou_trends()` block and the season head-to-head were all computed and rendered on the
   page while the model never saw any of them. A field that reaches `analyze_game` and
   stops at the HTML is a field the picks are made without.
6. If it needs a new prompt rule to be usable, that rule goes in `_AI_SYSTEM_PROMPT`, and
   its meaning goes in `_CARD_FORMAT` alongside the other blocks. **The converse still
   binds: anything REMOVED from the card must also come out of what the prompt invites the
   model to quote.** That pairing used to be enforced by the auditor rejecting figures not
   on the card; with the auditor gone nothing catches it, so a stale invitation now
   produces a fabricated number in published copy instead of a rejection. Two such misses
   have already happened (F5 and the at-park split) — grep the prompt for the block name
   before you consider a card cut finished.

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

`get_weather()` now takes **coordinates**, not a team code. The old team-keyed `STADIUMS`
map has been deleted: it had no remaining callers, and it was wrong for exactly the games
this section is about. Do not reintroduce a team-keyed park lookup — resolve the venue and
its coordinates from the schedule (see `venues.py`).

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
| Per-event props (`/events/{id}/odds`) | 2 pitcher props + 3 F5 + 2 team totals + 2 alternate ladders = 9 | **9** |

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

**The alternate ladders (added 2026-08-23) spent part of that headroom.** The props call
went from 7 markets to 9 — `alternate_team_totals` and `pitcher_strikeouts_alternate` —
which is +2 credits on *every* per-event call, the most expensive line on the bill:

| | 7 markets | 9 markets |
|---|---|---|
| Today's props | 3 × ~14 × 7 = 294 | 3 × ~14 × 9 = **378** |
| Tomorrow's props | 1 × 15 × 7 = 105 | 1 × 15 × 9 = **135** |
| Daily total | ~420 | **~534** |
| Monthly | ~12,800 (36% headroom) | **~16,000** (20% headroom) |

Still inside a 20,000 plan, but the margin is now thin enough that a fifth cron run or a
tenth market would break it. Verify against real headers before adding anything:
`jq '{fetched_at,quota_remaining,quota_used}' data/props_meta_*.json`.

The evening scheduled run uses `--force-odds` so the first real pull always lands rather
than being skipped by a stale throttle timestamp; manual runs after 5 PM stay throttled so
repeated dispatches do not re-buy the same board at ~108 credits each.

`odds_meta_{date}.json` and `props_meta_{date}.json` now persist `quota_remaining` and
`quota_used` from the response headers, so burn rate is readable from the repo instead of
re-derived by hand. Check with:
`jq '{fetched_at,quota_remaining,quota_used}' data/odds_meta_*.json`

**CRITICAL**: Never let Claude run `curl https://api.the-odds-api.com/` — this costs
credits. `.claude/settings.json` denies it automatically.

### The Anthropic API — input-dominated, and the old estimate was wrong

Per scheduled run: 1 generation call + one verification call per pick returned (~8).
Measured from `picks/`, the model returns ~7–8 picks per run (~4 survive dedup as new).

**Measured** from `usage/2026-08.json`, 18 days (2026-08-09 → 08-26), 535 calls:

| | Actual | The estimate this replaced |
|---|---|---|
| Cost/day | **$4.02** | ~$3.30 |
| Input tokens/day | **429,000** | ~104,000 |
| Output tokens/day | 75,000 | ~112,000 |
| Input share of the bill | **53%** | ~16% |

> Superseded 2026-08-30: with the audit pass removed the bill is **~$2.64/day,
> ~$79/month, 4 calls/day**. The measurement below is kept because it is what identified
> the audit pass as ~$48 of the bill, and because the per-call arithmetic still applies to
> the one remaining call.

**~$121/month.** The headline was close; the composition was inverted. The old table
said "output is ~84% of the bill" — it is 47%, and **input is now the majority**. Two
things drifted after it was written and neither was re-measured:

- **`_AI_SYSTEM_PROMPT` is ~12,950 tokens, not the 4.0K the table assumed.** It roughly
  tripled across the 2026-08-23 prompt audit and the 08-24 adversarial review. Measure it,
  don't estimate it: `len(_AI_SYSTEM_PROMPT)` is 51,819 chars as of 2026-08-27.
- **The card is far past the ~550 tok/game the table assumed.** See below.

Regressing daily input tokens on daily call count over those 18 days gives
`input ≈ -84,847 + 17,278 × calls` (R² = 0.85): **~17,300 input tokens per marginal
call**, nearly all of it the data card, which the audit pass re-sends in full once per
pick. That multiplier is why the card — not the system prompt — is the thing to shrink.

Two things follow, and both are load-bearing:

- **That analysis is why the audit pass was removed on 2026-08-30.** It was ~26 of the
  ~30 daily calls and roughly $48/month; see `## AI Picks — one generation call` for what
  the removal cost in accuracy. The bill is ~$79/month after it.
- **Prompt caching went with it, and there is nothing left to cache.** `_verify_pick`
  marked its tools+system prefix ephemeral and got real value from it — the audit was a
  sequential loop, so 6 of every 7 calls were cache reads at 0.1x. `generate_suggestions`
  deliberately did NOT cache and still should not: it fires once per run, six hours apart,
  so its cache could never be read and a breakpoint would only add the 1.25x write premium
  on ~14K tokens. **Do not add `cache_control` to the generation call.** With one call per
  run there is no repeated prefix to amortise. (`usage.cost_usd()` still prices reads at
  0.10x and writes at 1.25x, so if a second call is ever reintroduced the ledger is ready.)

36 calls/day is nowhere near any rate limit — spend is the only constraint.

The AI call is skipped when no game on the slate has odds posted (early opening day, or a
failed odds fetch) — the prompt cannot produce a bet without a price, so that call was
guaranteed to return an empty picks array at full cost.

## Measuring the AI Card

The card is the dominant input-token term in the bill (see API Budget above), and it had
never been measured — CLAUDE.md modelled it at ~550 tokens/game while the adversarial
review kept adding to it. There is now tooling instead of an estimate:

```bash
python3 handicap.py --dump-cards /tmp/cards.json   # no API call — serializes and exits
python3 scripts/measure_card.py /tmp/cards.json    # per-block token profile
```

`--dump-cards` writes exactly what `generate_suggestions` would send.
`scripts/measure_card.py` splits each card into its named blocks, pools them across the
slate, and counts tokens — **exactly**, via `messages.count_tokens`, when
`ANTHROPIC_API_KEY` is set. That endpoint is free; a chars/4 rule is off by enough on
dense numeric text to rank the blocks wrongly, so treat any run that prints
`ESTIMATED (chars/4)` as indicative only. The splitter asserts that block line counts
reconstruct the card, so a section header added to `_serialize_game_for_ai` without a
matching entry in `_SECTIONS` fails loudly rather than silently pooling into its
predecessor.

**`.github/workflows/measure-card.yml`** runs both on demand (`workflow_dispatch`) at
zero cost: it restores the `data/` cache the publish workflow already saved instead of
downloading, so the metered Odds API is never touched, and token counting is free. It
uses `actions/cache/restore` and **never saves a cache** — saving would claim a key that
publish's run-scoped `restore-keys` then falls back to, and publish must own that data.

### First profile — 2026-08-27, exact

Measured in CI with `messages.count_tokens` over 7 real cards. **The card is 2,926
tokens.** The earlier estimate in this file (~550) was low by 5x; the ~15,000 implied by
the input-vs-calls regression was high by 5x, because bigger slates produce both more
picks and more calls and the regression could not separate them.

The measured number closes the budget: 4 generation calls (system 12,954 + 20 cards) plus
~26 audits (system 2,377 + one card + rationale) predicts **~433,000 input tokens/day**
against **~429,000 actually recorded**. The cost model is now validated end to end.

| Block | tok/game | % of card |
|---|---|---|
| pitchers (incl. 3 recent starts each) | 553 | 18.9% |
| **odds** | 537 | 18.4% |
| offense | 372 | 12.7% |
| h2h (splits + every meeting) | 369 | 12.6% |
| **odds_alt_ladders** | 267 | 9.1% |
| ou_history | 204 | 7.0% |
| flags | 168 | 5.8% |
| bullpens | 153 | 5.2% |
| trends | 137 | 4.7% |
| lineup | 62 | 2.1% |
| header / season / situational / weather | 103 | 3.5% |
| **total** | **2,926** | |

Three things the profile says that the estimates did not:

- **Odds is the biggest thing on the card, not the pitchers.** `odds` + `odds_alt_ladders`
  is **804 tok/game, 27.5%** — 11.7 market rows plus the ladders. Half those rows are F5
  (F5 ML, F5 total, F5 spread, and both F5 team totals), which is the family this file
  already records as -54% ROI on F5 totals, -100% on F5 ML, and n=0 picks since the
  rewrite. It is the best-evidenced cut on the card.
- **`lineup` at 62 tok is the "not yet posted" placeholder, not a lineup.** This run was
  9:37 AM ET. Measured again at 5:16 PM ET the block is real and **320 tok**, so the
  evening card runs ~260 tokens heavier than the morning one. Three of the four daily
  runs pay the placeholder, not the lineup.
- **`ou_history` + `trends` = 341 tok/game (11.7%)** for the two things §11 says are worth
  very little.

Two caveats on the slate rather than the card. Handigraphs had only 7 of today's 20
matchups posted at the 7:14 AM ET fetch, so 2,926 is the average of a *fully populated*
card; games carried in from the MLB schedule alone are thinner. And a 20-game slate puts
the generation call near **58,000 input tokens**, not the 20,414 printed for 7.

### After the 2026-08-27 cuts — measured

Four cuts landed the same day: F5 markets off the card, the at-park split out of the
head-to-head block, OVER/UNDER HISTORY from four slices to two, and the per-card prose
hoisted into `_CARD_FORMAT`.

**Controlled A/B, identical inputs: 1,456 → 1,042 tok/card, −28.4%.**

The two CI runs are NOT a controlled comparison and must not be read as one — the second
restored a cache eight hours fresher, so lineups had posted, the books had filled in the
alternate ladders, and more flags were firing. Same 7 games, different data:

| Block | before | after | Δ | |
|---|---|---|---|---|
| pitchers | 553 | 499 | −54 | header hoisted |
| **odds** | 537 | 336 | **−201** | F5 rows removed |
| offense | 372 | 232 | **−140** | header hoisted |
| h2h | 369 | 232 | **−137** | at-park split removed |
| ou_history | 204 | 85 | **−119** | 4 slices → 2 |
| lineup | 62 | 320 | +258 | *confound* — placeholder → real lineup |
| odds_alt_ladders | 267 | 364 | +97 | *confound* — books filled the ladders |
| flags | 168 | 191 | +23 | *confound* — fresher data fires more |
| **total excl. lineup** | **2,863** | **2,306** | **−557** | **−19.5%** |

Read the per-block deltas, not the total: every cut moves the right way and by roughly
the expected amount, while all three increases are the data being newer.

**Worth ~$8/month.** 4 generation calls × 20 games × 557 plus 26 audits × 557 is ~59,000
input tokens/day, less ~566 tokens of system-prompt growth per call for `_CARD_FORMAT`
(absorbed on the audit side by the cache). The card was never the $50/month line the
regression implied — at 2,926 tokens there was only so much in it.

**`_CARD_FORMAT` is a named constant interpolated into `_AI_SYSTEM_PROMPT`.** It fed the
auditor's prompt too until that pass was removed; it stays a constant because it describes
the *card* rather than the analysis, so the next thing that reads a card should reuse it
rather than re-describe it.

**Anything cut from the card must also come out of what a rationale may quote.** Audit
check 2 rejects figures not on the card, so a block removed from `_serialize_game_for_ai`
while the prompts still invite the model to reason about it turns good picks into
rejections. Each of these four cuts moved the prompt too: §4A/§4B/§6/§7 lost their F5
options and the `bet_type`/`period` enums stopped offering them; §11's framing of the
outcome blocks now lives in `_CARD_FORMAT`. Keep that pairing for any future cut.

> **Superseded 2026-08-30, same day: F5 was restored.** The cut cited this file's own
> "-54% ROI on F5 totals, -100% on F5 ML" — and the -100% is FOUR picks. Recomputed with
> `results.unit_pnl` over the whole log: **F5 ML is 21-10, +21.5% over 34**, the largest
> F5 sample there is and well ahead of the archive era's -5.7% baseline. Only F5 totals
> are genuinely bad (**6-19, -62.2%, n=25**). Cutting the whole family on a four-pick
> number was wrong. Restoring costs ~$2.41/month now that the card is sent once per run
> rather than re-sent for every audit call, and §6 carries the F5-totals warning so the
> record is on the card rather than enforced by hiding the market. **Do not re-cut F5
> without splitting ML from totals first.**

**The paragraph below is the superseded rationale, kept for the record.**

**F5 was gone from the card and the tool schema, not from the project.** `analysis.py`
still computes it, the HTML page still shows it, `history.py` still records it, and
`picks.py` still grades it — past picks have to render and settle. `download.py` also
still fetches the three F5 markets, so **this saves no Odds API credits**; removing them
from the props URL is a separate change with its own consequences for the page.

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
