# MLB Capper — Claude Session Guide

## What This Is
A daily MLB handicapping dashboard published to GitHub Pages at `www.adamrosini.com/mlb-capper/`. A GitHub Actions workflow runs at 6 AM ET, downloads fresh data, generates a static HTML page, and deploys it.

## Credentials & Secrets
**Never commit credentials.** Two sources:
- **Local**: `.env` file (gitignored) — auto-loaded by `config.py` at import time
- **CI**: GitHub Secrets → passed as env vars in the workflow `Download data` step

Keys in use: `HANDIGRAPHS_EMAIL`, `HANDIGRAPHS_PASSWORD`, `ODDS_API_KEY`

## Data Sources

| Source | Auth | What it provides |
|--------|------|-----------------|
| Handigraphs API | JWT Bearer (login → token) | Starters (last 3), team offense stats (L12RHP/LHP), bullpen stats (last 12), ballpark weather |
| MLB Stats API | None (free) | Home/away determination, venue name, pitcher game logs |
| The Odds API | API key (query param) | Full-game ML/spread/total for DK, FanDuel, Fanatics — 500 req/month free |

## File Overview

**`config.py`** — reads credentials from env/`.env`, defines `API_URLS` and `DATA_DIR`

**`download.py`** — fetches all endpoints and saves to `data/` as dated JSON files:
- `starters_last3g_{slot}_{date}.json`
- `team_stats_L12RHP_{date}.json` / `team_stats_L12LHP_{date}.json`
- `bullpen_stats_last12g_{date}.json`
- `ballpark_weather_{date}.json`
- `odds_{date}.json`

Handigraphs needs JWT login first (`login()` → sets `Authorization: Bearer` header). Odds API is a plain GET with `?apiKey=`.

**`handicap.py`** — analysis + rendering. Key sections:
- **Loaders**: `load_starters`, `load_team_stats`, `load_bullpen`, `load_ballpark_weather`, `load_odds` — all read from `data/`
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
Each `<details open>` card renders (in order):
1. **Summary**: `[logo] AWAY @ [logo] HOME` + `time · venue (roof status)` subtitle
2. **Odds** — 2×3 grid: ML / Spread / Total for away/home (best across DK/FD/Fanatics)
3. **Starters · last 3 starts** — xERA, K%, BB%, HH%, Barrel% (if available), IP/gs
4. **Offense vs Starter · last 12** — wRC+, wOBA, K%, Hard% (split by opponent handedness)
5. **Bullpens · last 12** — xERA, ERA, K%, BB%, HH%, Barrel% (if available)
6. **Weather** — venue, roof, time, conditions, adjusted park factor with color coding
7. **Flags** — auto-generated warnings (regression risk, small samples, weather, etc.)

CSS uses `prefers-color-scheme: dark` for automatic dark mode. No JavaScript.

## Deployment
- **Repo**: `github.com/arosini/mlb-capper`
- **Branch**: `main` (source) → GitHub Actions → `gh-pages` is NOT used; `actions/deploy-pages` deploys directly
- **Workflow**: `.github/workflows/publish.yml` — cron `0 10 * * *` (6 AM ET), also `workflow_dispatch`
- **Trigger manually**: `gh workflow run publish.yml`
- **Concurrency**: `group: pages, cancel-in-progress: true` prevents deploy conflicts

## Adding New Data Fields
1. Check what's available: `python3 download.py --inspect`
2. Map the raw JSON key in the appropriate `_load_*_json()` loader
3. Add to the `_sp()` / `_bp()` / `_off()` dict inside `analyze_game()`
4. Render it in `_sp_row()` / `_bp_row()` / `_off_row()` in `_html_game()`

## Odds API Budget
500 req/month free tier. Current usage: ~30/month (once daily). To check remaining quota, run `python3 download.py` and watch the odds line — it prints `X API calls remaining`.
