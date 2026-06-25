import os
from pathlib import Path

# Credentials are read from environment variables.
# For local use, create a .env file next to this file (it's gitignored):
#
#   HANDIGRAPHS_EMAIL=you@example.com
#   HANDIGRAPHS_PASSWORD=yourpassword
#
# config.py auto-loads .env so you don't need to source it manually.

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#"):
            continue
        if _line.startswith("export "):
            _line = _line[7:]
        if "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

HANDIGRAPHS_EMAIL    = os.environ.get("HANDIGRAPHS_EMAIL", "")
HANDIGRAPHS_PASSWORD = os.environ.get("HANDIGRAPHS_PASSWORD", "")
ODDS_API_KEY         = os.environ.get("ODDS_API_KEY", "")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Handigraphs API ───────────────────────────────────────────────────────────
HANDIGRAPHS_BASE_URL  = "https://www.handigraphs.com"
HANDIGRAPHS_LOGIN_URL = f"{HANDIGRAPHS_BASE_URL}/api/auth/login"

API_URLS = {
    "starters":         f"{HANDIGRAPHS_BASE_URL}/api/starters?split=last3g&day={{slot}}&include_season_stats=true",
    "team_rhp":         f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split=L12RHP&include_season_stats=true",
    "team_lhp":         f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split=L12LHP&include_season_stats=true",
    "bullpen":          f"{HANDIGRAPHS_BASE_URL}/api/bullpen-stats/team?split=last12g&include_season_stats=true",
    "ballpark_weather": f"{HANDIGRAPHS_BASE_URL}/api/ballpark-weather",
}

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = "./data"
