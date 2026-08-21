"""Team code normalization, logo helpers, and name mappings."""

import unicodedata


def normalize_name(name: str) -> str:
    """Lowercase a person's name and strip diacritics for comparison/keying.

    The Odds API returns pitcher names without diacritics ("Walbert Urena")
    while the MLB boxscore keeps them ("Walbert Ureña"). Comparing raw
    `.lower()` strings treats the same pitcher as two different people, so
    every place that matches pitcher names across those two sources must key
    off this instead.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()

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

# Divisions, keyed by Handigraphs team code — used to flag divisional games.
DIVISIONS = {
    "BAL": "AL East", "BOS": "AL East", "NYY": "AL East", "TBR": "AL East", "TOR": "AL East",
    "CHW": "AL Central", "CLE": "AL Central", "DET": "AL Central", "KCR": "AL Central", "MIN": "AL Central",
    "ATH": "AL West", "HOU": "AL West", "LAA": "AL West", "SEA": "AL West", "TEX": "AL West",
    "ATL": "NL East", "MIA": "NL East", "NYM": "NL East", "PHI": "NL East", "WSN": "NL East",
    "CHC": "NL Central", "CIN": "NL Central", "MIL": "NL Central", "PIT": "NL Central", "STL": "NL Central",
    "ARI": "NL West", "COL": "NL West", "LAD": "NL West", "SDP": "NL West", "SFG": "NL West",
}


def division(team: str) -> str:
    """Division name for a Handigraphs team code, or '' if unknown."""
    return DIVISIONS.get(team, "")


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
