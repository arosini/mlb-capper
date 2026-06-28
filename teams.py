"""Team code normalization, logo helpers, and name mappings."""

# Handigraphs starters use codes that differ from team_stats CSVs and MLB API
_STATS_MAP = {"CHW": "CWS", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB", "WSN": "WSH"}
_MLB_MAP = {**_STATS_MAP, "ARI": "AZ"}  # MLB API uses "AZ" for Diamondbacks; ATH stays as-is

# ESPN CDN logo codes (keyed by Handigraphs team codes)
_LOGO = {
    "ARI": "ari", "ATH": "oak", "ATL": "atl", "BAL": "bal", "BOS": "bos",
    "CHC": "chc", "CHW": "cws", "CIN": "cin", "CLE": "cle", "COL": "col",
    "DET": "det", "HOU": "hou", "KCR": "kc",  "LAA": "laa", "LAD": "lad",
    "MIA": "mia", "MIL": "mil", "MIN": "min", "NYM": "nym", "NYY": "nyy",
    "PHI": "phi", "PIT": "pit", "SDP": "sd",  "SEA": "sea", "SFG": "sf",
    "STL": "stl", "TBR": "tb",  "TEX": "tex", "TOR": "tor", "WSN": "wsh",
}

# Odds API team names (keyed by Handigraphs team codes)
ODDS_TEAM = {
    "ARI": "Arizona Diamondbacks",  "ATH": "Athletics",
    "ATL": "Atlanta Braves",        "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",        "CHC": "Chicago Cubs",
    "CHW": "Chicago White Sox",     "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",   "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",        "HOU": "Houston Astros",
    "KCR": "Kansas City Royals",    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",   "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",     "MIN": "Minnesota Twins",
    "NYM": "New York Mets",         "NYY": "New York Yankees",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
    "SDP": "San Diego Padres",      "SEA": "Seattle Mariners",
    "SFG": "San Francisco Giants",  "STL": "St. Louis Cardinals",
    "TBR": "Tampa Bay Rays",        "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",     "WSN": "Washington Nationals",
}

# Reverse mapping: MLB API team name → Handigraphs code
MLB_NAME_TO_CODE: dict[str, str] = {v: k for k, v in ODDS_TEAM.items()}
MLB_NAME_TO_CODE.update({
    "Oakland Athletics": "ATH",  # pre-relocation name still in some MLB API responses
})


def to_stats(t: str) -> str:
    """Normalize Handigraphs code to team_stats/bullpen key."""
    return _STATS_MAP.get(t, t)


def to_mlb(t: str) -> str:
    """Normalize Handigraphs code to MLB Stats API abbreviation."""
    return _MLB_MAP.get(t, t)


def logo_img(team: str) -> str:
    """Return an <img> tag for a team logo via ESPN CDN."""
    code = _LOGO.get(team, team.lower())
    url = f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/mlb/500/{code}.png&h=28&w=28"
    return f'<img src="{url}" class="tm-logo" alt="{team}" onerror="this.style.display=\'none\'">'
