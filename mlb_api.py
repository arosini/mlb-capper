"""MLB Stats API + Open-Meteo weather fetches."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ET = timezone(timedelta(hours=-4))

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

MLB_API = "https://statsapi.mlb.com/api/v1"


# ── IP / stress helpers ───────────────────────────────────────────────────────

def mlb_ip_to_real(ip_str: str) -> float:
    """Convert MLB IP notation ('6.1' = 6⅓, '6.2' = 6⅔) to real float."""
    try:
        ip = float(ip_str)
        whole = int(ip)
        thirds = round((ip - whole) * 10)
        return whole + thirds / 3
    except (TypeError, ValueError):
        return 0.0


def stress_label_cls(ip_2d: float, games_2d: int) -> tuple[str, str]:
    """Return (label, css_class) for bullpen stress based on avg relief IP per game."""
    if games_2d == 0:
        return "No recent games", "era-na"
    avg = ip_2d / games_2d
    if avg < 2.5:
        return "Fresh", "era-elite"
    elif avg < 4.0:
        return "Normal", "era-avg"
    elif avg < 5.5:
        return "Elevated", "era-below"
    else:
        return "Stressed", "era-poor"


# ── MLB schedule / game log calls ─────────────────────────────────────────────

def get_mlb_schedule(target_date: date) -> dict:
    """Fetch today's schedule; returns {(frozenset([away, home]), game_number): game_info_dict}.

    game_number distinguishes doubleheader legs (MLB API's "gameNumber" field, 1 or 2).
    """
    if not HAS_REQUESTS:
        return {}
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": target_date.isoformat(),
                "hydrate": "probablePitcher,venue,team",
                "gameType": "R",
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"Warning: MLB API unavailable ({e})", file=sys.stderr)
        return {}

    games = {}
    for date_entry in r.json().get("dates", []):
        for g in date_entry.get("games", []):
            teams = g.get("teams", {})
            home  = teams.get("home", {})
            away  = teams.get("away", {})
            ha    = home.get("team", {}).get("abbreviation", "")
            aa    = away.get("team", {}).get("abbreviation", "")
            hp    = home.get("probablePitcher", {})
            ap    = away.get("probablePitcher", {})
            gn    = g.get("gameNumber") or 1
            games[(frozenset([ha, aa]), gn)] = {
                "home": ha, "away": aa,
                "home_mlb_id": home.get("team", {}).get("id"),
                "away_mlb_id": away.get("team", {}).get("id"),
                "venue":       g.get("venue", {}).get("name", ""),
                "home_pid":    hp.get("id"),   "home_pname": hp.get("fullName", ""),
                "away_pid":    ap.get("id"),   "away_pname": ap.get("fullName", ""),
                "game_date":   g.get("gameDate", ""),
                "game_number": gn,
            }
    return games


def get_recent_starts(player_id: int) -> list[dict]:
    """Fetch pitcher game log for the current and prior season."""
    if not HAS_REQUESTS or not player_id:
        return []
    current_year = datetime.now(_ET).year
    all_splits: list[dict] = []
    for season in [current_year - 1, current_year]:
        try:
            r = requests.get(
                f"{MLB_API}/people/{player_id}/stats",
                params={"stats": "gameLog", "season": season, "group": "pitching"},
                timeout=10,
            )
            r.raise_for_status()
            from analysis import flt  # avoid circular at module level
            splits = r.json().get("stats", [{}])[0].get("splits", [])
            all_splits.extend(
                s for s in splits
                if flt(s.get("stat", {}).get("inningsPitched")) is not None
            )
        except Exception:
            pass
    return all_splits


def get_team_schedule(team_id: int, season: int) -> list[dict]:
    """Fetch completed game results for a team in the given season."""
    if not HAS_REQUESTS or not team_id:
        return []
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={"teamId": team_id, "season": season, "sportId": 1, "gameType": "R"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception:
        return []
    results = []
    for date_entry in r.json().get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            teams   = g.get("teams", {})
            home    = teams.get("home", {})
            away    = teams.get("away", {})
            is_home = home.get("team", {}).get("id") == team_id
            my      = home if is_home else away
            opp     = away if is_home else home
            results.append({
                "game_pk":      g.get("gamePk"),
                "date":         date_entry.get("date", ""),
                "is_home":      is_home,
                "won":          bool(my.get("isWinner")),
                "runs_scored":  int(my.get("score") or 0),
                "runs_allowed": int(opp.get("score") or 0),
            })
    return results


def get_bullpen_stress(team_mlb_ids: set, target_date: date, data_dir: Path) -> dict:
    """Fetch 2-day bullpen usage via MLB boxscores.

    Returns {team_mlb_id: {"ip": float, "games": int, "label": str, "css": str}}.
    Caches to data_dir/bullpen_stress_{date}.json — written once per calendar date.
    """
    if not HAS_REQUESTS or not team_mlb_ids:
        return {}

    cache_path = data_dir / f"bullpen_stress_{target_date.isoformat()}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            return {int(k): v for k, v in cached.items()}
        except Exception:
            pass

    start = (target_date - timedelta(days=2)).isoformat()
    end   = (target_date - timedelta(days=1)).isoformat()

    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={"sportId": 1, "startDate": start, "endDate": end, "gameType": "R"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"Warning: bullpen stress fetch failed: {e}", file=sys.stderr)
        return {}

    ip_by_team: dict    = {}
    games_by_team: dict = {}

    for date_entry in r.json().get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            t_home = g.get("teams", {}).get("home", {}).get("team", {}).get("id")
            t_away = g.get("teams", {}).get("away", {}).get("team", {}).get("id")
            if t_home not in team_mlb_ids and t_away not in team_mlb_ids:
                continue
            pk = g.get("gamePk")
            try:
                rb = requests.get(f"{MLB_API}/game/{pk}/boxscore", timeout=10)
                rb.raise_for_status()
                bs = rb.json()
            except Exception:
                continue
            for side, team_id in [("home", t_home), ("away", t_away)]:
                if team_id is None:
                    continue
                t = bs.get("teams", {}).get(side, {})
                relief_ip = sum(
                    mlb_ip_to_real(str(
                        t.get("players", {}).get(f"ID{pid}", {})
                         .get("stats", {}).get("pitching", {}).get("inningsPitched", "0")
                    ))
                    for pid in t.get("pitchers", [])
                    if int(t.get("players", {}).get(f"ID{pid}", {})
                            .get("stats", {}).get("pitching", {}).get("gamesStarted", 0)) == 0
                )
                ip_by_team[team_id]    = ip_by_team.get(team_id, 0.0) + relief_ip
                games_by_team[team_id] = games_by_team.get(team_id, 0) + 1

    result: dict = {}
    for team_id in team_mlb_ids:
        ip     = ip_by_team.get(team_id, 0.0)
        games  = games_by_team.get(team_id, 0)
        label, css = stress_label_cls(ip, games)
        result[team_id] = {"ip": round(ip, 1), "games": games, "label": label, "css": css}

    try:
        cache_path.write_text(json.dumps({str(k): v for k, v in result.items()}))
    except Exception:
        pass

    return result


# ── Weather (Open-Meteo fallback) ─────────────────────────────────────────────

# (lat, lon, city, IANA timezone)
STADIUMS: dict[str, tuple] = {
    "ARI": (33.4453, -112.0667, "Phoenix",           "America/Phoenix"),
    "ATH": (38.5802, -121.4687, "Sacramento",         "America/Los_Angeles"),
    "OAK": (38.5802, -121.4687, "Sacramento",         "America/Los_Angeles"),
    "ATL": (33.8908,  -84.4677, "Atlanta",            "America/New_York"),
    "BAL": (39.2838,  -76.6218, "Baltimore",          "America/New_York"),
    "BOS": (42.3467,  -71.0972, "Boston",             "America/New_York"),
    "CHC": (41.9484,  -87.6553, "Chicago (Wrigley)",  "America/Chicago"),
    "CWS": (41.8300,  -87.6338, "Chicago (Sox)",      "America/Chicago"),
    "CHW": (41.8300,  -87.6338, "Chicago (Sox)",      "America/Chicago"),
    "CIN": (39.0978,  -84.5081, "Cincinnati",         "America/New_York"),
    "CLE": (41.4962,  -81.6852, "Cleveland",          "America/New_York"),
    "COL": (39.7559, -104.9942, "Denver",             "America/Denver"),
    "DET": (42.3390,  -83.0485, "Detroit",            "America/Detroit"),
    "HOU": (29.7573,  -95.3555, "Houston",            "America/Chicago"),
    "KC":  (39.0517,  -94.4803, "Kansas City",        "America/Chicago"),
    "KCR": (39.0517,  -94.4803, "Kansas City",        "America/Chicago"),
    "LAA": (33.8003, -117.8827, "Anaheim",            "America/Los_Angeles"),
    "LAD": (34.0739, -118.2400, "Los Angeles",        "America/Los_Angeles"),
    "MIA": (25.7781,  -80.2197, "Miami",              "America/New_York"),
    "MIL": (43.0280,  -87.9712, "Milwaukee",          "America/Chicago"),
    "MIN": (44.9817,  -93.2781, "Minneapolis",        "America/Chicago"),
    "NYM": (40.7571,  -73.8458, "New York (Mets)",    "America/New_York"),
    "NYY": (40.8296,  -73.9262, "New York (Yankees)", "America/New_York"),
    "PHI": (39.9061,  -75.1665, "Philadelphia",       "America/New_York"),
    "PIT": (40.4469,  -80.0058, "Pittsburgh",         "America/New_York"),
    "SD":  (32.7076, -117.1570, "San Diego",          "America/Los_Angeles"),
    "SDP": (32.7076, -117.1570, "San Diego",          "America/Los_Angeles"),
    "SEA": (47.5914, -122.3325, "Seattle",            "America/Los_Angeles"),
    "SF":  (37.7786, -122.3893, "San Francisco",      "America/Los_Angeles"),
    "SFG": (37.7786, -122.3893, "San Francisco",      "America/Los_Angeles"),
    "STL": (38.6226,  -90.1928, "St. Louis",          "America/Chicago"),
    "TB":  (27.7682,  -82.6534, "St. Petersburg",     "America/New_York"),
    "TBR": (27.7682,  -82.6534, "St. Petersburg",     "America/New_York"),
    "TEX": (32.7473,  -97.0824, "Arlington",          "America/Chicago"),
    "TOR": (43.6414,  -79.3894, "Toronto",            "America/Toronto"),
    "WSH": (38.8730,  -77.0074, "Washington",         "America/New_York"),
    "WSN": (38.8730,  -77.0074, "Washington",         "America/New_York"),
}


def get_weather(home_team: str, target_date: date) -> dict:
    """Fetch weather from Open-Meteo for the home team's stadium."""
    if not HAS_REQUESTS:
        return {}
    s = STADIUMS.get(home_team)
    if not s:
        return {}
    lat, lon, city, tz = s
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily":    "precipitation_probability_max,temperature_2m_max,windspeed_10m_max",
                "timezone": tz,
                "start_date": target_date.isoformat(),
                "end_date":   target_date.isoformat(),
                "wind_speed_unit":   "mph",
                "temperature_unit":  "fahrenheit",
            },
            timeout=10,
        )
        r.raise_for_status()
        daily  = r.json().get("daily", {})
        precip = (daily.get("precipitation_probability_max") or [None])[0]
        temp   = (daily.get("temperature_2m_max") or [None])[0]
        wind   = (daily.get("windspeed_10m_max") or [None])[0]
        return {
            "venue_name":              city,
            "roof_status":             "Open Air",
            "temperature":             temp,
            "precip_probability":      precip,
            "precip_risk_during_game": precip is not None and precip >= 50,
            "wind_speed":              wind,
            "wind_effect_label":       ("Out" if wind and wind > 15 else ""),
        }
    except Exception:
        return {}
