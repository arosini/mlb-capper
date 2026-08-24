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

# Team-offense split windows. The API validates the split token — an unknown value is a
# 400, not a silently ignored parameter — so these two are the only thing to change if
# Handigraphs ever renames the windows.
#
# Verified live 2026-08-13: L6RHP/L6LHP and L12RHP/L12LHP all return 30 rows with an
# identical schema, and the L6 numbers differ materially from the L12 ones (they are a
# genuinely shorter window, not the same data under another name).
TEAM_SPLIT_PRIMARY = "L6"    # every offense number on the card comes from this window
TEAM_SPLIT_CONTEXT = "L12"   # wRC+ only, carried alongside L6 for comparison

# The same two windows with no platoon split at all. Handigraphs spells the unsplit
# form "<window>G" — "L6G", not "L6"; a bare "L6" is a 400 Invalid split parameter.
# Only used for bullpen games, where there is no starter whose hand to split on.
TEAM_SPLIT_ALL_SUFFIX = "G"

API_URLS = {
    "starters":         f"{HANDIGRAPHS_BASE_URL}/api/starters?split=last3g&day={{slot}}&include_season_stats=true",
    "team_rhp":         f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split={TEAM_SPLIT_PRIMARY}RHP&include_season_stats=true",
    "team_lhp":         f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split={TEAM_SPLIT_PRIMARY}LHP&include_season_stats=true",
    "team_rhp_ctx":     f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split={TEAM_SPLIT_CONTEXT}RHP&include_season_stats=true",
    "team_lhp_ctx":     f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split={TEAM_SPLIT_CONTEXT}LHP&include_season_stats=true",
    "team_all":         f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split={TEAM_SPLIT_PRIMARY}{TEAM_SPLIT_ALL_SUFFIX}&include_season_stats=true",
    "team_all_ctx":     f"{HANDIGRAPHS_BASE_URL}/api/team-stats?split={TEAM_SPLIT_CONTEXT}{TEAM_SPLIT_ALL_SUFFIX}&include_season_stats=true",
    "bullpen":          f"{HANDIGRAPHS_BASE_URL}/api/bullpen-stats/team?split=last12g&include_season_stats=true",
    "ballpark_weather": f"{HANDIGRAPHS_BASE_URL}/api/ballpark-weather",
}

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = "./data"
