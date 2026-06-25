# MLB Capper — Claude Session Guide

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
| Anthropic API | API key (`ANTHROPIC_API_KEY`) | Claude Sonnet 4.6 for AI Picks section — called once per odds refresh, cached to `data/suggestions_{date}.json` |

## File Overview

**`config.py`** — reads credentials from env/`.env`, defines `API_URLS` and `DATA_DIR`

**`download.py`** — fetches all endpoints and saves to `data/` as dated JSON files:
- `starters_last3g_{slot}_{date}.json`
- `team_stats_L12RHP_{date}.json` / `team_stats_L12LHP_{date}.json`
- `bullpen_stats_last12g_{date}.json`
- `ballpark_weather_{date}.json`
- `odds_{date}.json` — bulk game odds (merged with started-game odds from prior fetch)
- `odds_meta_{date}.json` — timestamp of last odds fetch (used for throttle check)
- `props_{date}.json` — per-event pitcher K/outs props + F5 odds (keyed by `(away_name, home_name)`)

Handigraphs needs JWT login first (`login()` → sets `Authorization: Bearer` header). Odds API is a plain GET with `?apiKey=`.

**Odds throttle**: `download_odds()` and `download_pitcher_props()` both skip refetch if their file is < 180 minutes old, to preserve the 500 req/month free quota. Started games are dropped from the Odds API response; their odds are merged back from the previous `odds_{date}.json` so they remain visible.

**`handicap.py`** — analysis + rendering. Key sections:
- **Loaders**: `load_starters`, `load_team_stats`, `load_bullpen`, `load_ballpark_weather`, `load_odds`, `load_odds_meta`, `load_pitcher_props` — all read from `data/`
- **`analyze_game(p1, p2, rhp, lhp, bullpen, mlb_info, wx)`** → returns structured dict; no I/O
- **`print_game()`** → terminal renderer (calls `analyze_game` internally)
- **`_html_game(g)`** → HTML renderer for one game card
- **`render_html_page(games, date, generated_at)`** → full page; sorts games by start time
- **`main()`** → parses args, loads all data, iterates game pairs, calls renderers

Run locally: `python3 handicap.py` (terminal) or `python3 handicap.py --html > out.html`

## Team Code Normalization
Handigraphs starters use codes like `KCR`, `TBR`, `SFG`, `SDP`, `CHW`, `WSN`, `ARI`.

```python
_STATS_MAP = {"CHW": "CWS", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB", "WSN": "WSH"}
_MLB_MAP   = {**_STATS_MAP, "ARI": "AZ"}  # MLB API uses AZ; ATH stays as-is
```

- **Ballpark weather lookup**: use raw Handigraphs codes (`frozenset([t1_raw, t2_raw])`) — NOT normalized
- **Odds lookup**: use `_ODDS_TEAM` dict (Handigraphs code → full team name for Odds API)
- **Team stats / bullpen lookup**: use `to_stats()` normalized codes

## HTML Card Structure (per game)
Each card renders collapsed `<details>` sections except Matchup (open by default):
1. **Summary** (always visible): `[logo] AWAY @ [logo] HOME` + `time · venue (roof status)` subtitle + weather/APF badge
2. **Betting Odds** — Full Game: 4×3 grid (ML / Spread / Total for away/home); First 5 Innings: same grid if available; Pitcher Props: K O/U and Outs O/U per starter (requires Odds API Starter plan+)
3. **Matchup · SP Last 3 / Team Last 12** (open) — SP card: xERA, K%, HH%, Barrel% (if available), ERA, IP/gs, H/gs, PC/gs, BB%; Offense card: wRC+, K%, HH% vs starter hand; outing table per SP
4. **Bullpens · last 12** — xERA, ERA (collapsed)
5. **Weather** — venue, roof, conditions, APF with color coding (collapsed)
6. **Flags** — auto-generated warnings (regression risk, small samples, weather, etc.) (collapsed)

CSS uses `prefers-color-scheme: dark` for automatic dark mode. No JavaScript.

## Deployment
- **Repo**: `github.com/arosini/mlb-capper`
- **Hosting**: Cloudflare Pages — project `mlb-capper`, custom domain `mlbautocap.com`
- **Workflow**: `.github/workflows/publish.yml` — cron `0 */3 * * *` (every 3 hours), also `workflow_dispatch` and push-to-main
- **Deploy step**: `cloudflare/wrangler-action@v3` with `pages deploy _site --project-name=mlb-capper --commit-dirty=true`
- **No-cache headers**: written inline in workflow (`printf '/*\n  Cache-Control: no-cache...' > _site/_headers`) — not a repo file
- **Secrets needed**: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `HANDIGRAPHS_EMAIL`, `HANDIGRAPHS_PASSWORD`, `ODDS_API_KEY`
- **Trigger manually**: `gh workflow run publish.yml`

## Adding New Data Fields
1. Check what's available: `python3 download.py --inspect`
2. Map the raw JSON key in the appropriate `_load_*_json()` loader
3. Add to the `_sp()` / `_bp()` / `_off()` dict inside `analyze_game()`
4. Render it in `_sp_row()` / `_bp_row()` / `_off_row()` in `_html_game()`

## Odds API Budget
500 req/month free tier. Current usage: ~30/month (once per 3-hour window × number of games for props). Bulk odds: 1 call/run. Props: 1 call/game/run (skipped if < 3h old). To check remaining quota, run `python3 download.py` and watch the odds/props lines — they print `X API calls remaining`.
