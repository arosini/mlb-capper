"""MLB Stats API + Open-Meteo weather fetches."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from season import ET as _ET, GAME_TYPES

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

    gameType spans regular season AND postseason (season.GAME_TYPES). This list is
    load-bearing: handicap.py treats the schedule as the authoritative game list and
    drops any game missing from it, so filtering to "R" alone renders an empty page
    for every day of October.
    """
    if not HAS_REQUESTS:
        return {}
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": target_date.isoformat(),
                # venue(location) returns coordinates/azimuth inline, so the venue a
                # game is actually played at costs no extra call — see venues.py.
                "hydrate": "probablePitcher,venue(location),team",
                "gameType": GAME_TYPES,
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
            ven   = g.get("venue", {}) or {}
            vloc  = ven.get("location") or {}
            vc    = vloc.get("defaultCoordinates") or {}
            games[(frozenset([ha, aa]), gn)] = {
                "home": ha, "away": aa,
                "home_mlb_id": home.get("team", {}).get("id"),
                "away_mlb_id": away.get("team", {}).get("id"),
                "venue":       ven.get("name", ""),
                "venue_id":    ven.get("id"),
                "venue_lat":   vc.get("latitude"),
                "venue_lon":   vc.get("longitude"),
                "venue_azimuth":   vloc.get("azimuthAngle"),
                "venue_elevation": vloc.get("elevation"),
                "venue_city":      vloc.get("city", ""),
                "venue_country":   vloc.get("country", ""),
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
            params={"teamId": team_id, "season": season, "sportId": 1, "gameType": GAME_TYPES},
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
            params={"sportId": 1, "startDate": start, "endDate": end, "gameType": GAME_TYPES},
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


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_point(deg) -> str:
    if deg is None:
        return ""
    return _COMPASS[int((float(deg) + 11.25) % 360 // 22.5)]


def wind_effect(wind_from_deg, azimuth_deg, speed_mph) -> tuple:
    """Classify wind as blowing Out / In / Cross relative to the park's orientation.

    `azimuth_deg` is the bearing from home plate to center field; `wind_from_deg` is the
    meteorological convention (the direction the wind blows FROM). Wind blowing out to
    center therefore originates behind home plate, at azimuth + 180.

    Sanity-checked against the best-known case in baseball: Wrigley's azimuth is 37, so
    this returns "Out" for a wind from 217 (SW) — which is exactly the south/southwest
    wind that famously blows out at Wrigley.

    The previous implementation labelled *any* wind over 15 mph as "Out" without ever
    looking at direction, which was backwards roughly half the time and fed straight
    into the model's totals reasoning.
    """
    if speed_mph is None or speed_mph < 5:
        return "Calm", None
    if wind_from_deg is None or azimuth_deg is None:
        return "", None
    out_dir = (float(azimuth_deg) + 180.0) % 360.0
    diff = ((float(wind_from_deg) - out_dir + 180.0) % 360.0) - 180.0
    if abs(diff) <= 45:
        return "Out", diff
    if abs(diff) >= 135:
        return "In", diff
    return "Cross", diff


def get_weather(lat: float, lon: float, target_date: date,
                first_pitch_utc: str = "", azimuth=None,
                venue_name: str = "", roof: str = "open",
                elevation_ft=None) -> dict:
    """Game-time weather from Open-Meteo for an explicit set of coordinates.

    Takes coordinates rather than a team code so neutral-site games get the park they
    are actually played in. Samples the HOURLY forecast across the hours the game
    occupies instead of the daily aggregate.

    That distinction is the whole point. The previous version read
    `temperature_2m_max`, `precipitation_probability_max` and `windspeed_10m_max` —
    the day's high, the 24-hour peak rain chance, and the day's strongest gust — and
    reported them as conditions for a 7 PM first pitch. Measured against Coors on
    2026-08-09 that was 98.4F vs 81.2F actual (+17.2F) and 19.5 mph vs 6.2 mph actual
    (+13.3 mph), and because the wind number cleared the old `> 15` rule the model was
    told the wind was blowing out during what was really a 5 mph breeze. It also let a
    4 AM rain band set `precip_risk_during_game` for a dry evening game, which trips
    the prompt's disqualifier on pitcher overs.
    """
    if not HAS_REQUESTS:
        return {}
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": ("temperature_2m,precipitation_probability,"
                           "wind_speed_10m,wind_direction_10m,weather_code,"
                           "relative_humidity_2m"),
                "timezone": "UTC",
                "start_date": (target_date - timedelta(days=1)).isoformat(),
                "end_date":   (target_date + timedelta(days=1)).isoformat(),
                "wind_speed_unit":  "mph",
                "temperature_unit": "fahrenheit",
            },
            timeout=10,
        )
        r.raise_for_status()
        h = r.json().get("hourly", {})
        times = h.get("time") or []
        if not times:
            return {}
    except Exception:
        return {}

    # Window: first pitch through roughly the end of a 3-hour game. Without a start
    # time, fall back to a 7-10 PM local evening slot, which is when most games run.
    start = None
    if first_pitch_utc:
        try:
            start = datetime.fromisoformat(first_pitch_utc.replace("Z", "+00:00")) \
                            .astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            start = None
    if start is None:
        start = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=23)

    want = [(start + timedelta(hours=k)).strftime("%Y-%m-%dT%H:00") for k in range(4)]
    idx = [times.index(w) for w in want if w in times]
    if not idx:
        return {}

    def _pick(key, fn):
        vals = [h.get(key, [])[i] for i in idx
                if i < len(h.get(key, [])) and h.get(key, [])[i] is not None]
        return fn(vals) if vals else None

    temp   = _pick("temperature_2m", lambda v: sum(v) / len(v))
    precip = _pick("precipitation_probability", max)
    wind   = _pick("wind_speed_10m", lambda v: sum(v) / len(v))
    wdir   = _pick("wind_direction_10m", lambda v: v[0])
    hum    = _pick("relative_humidity_2m", lambda v: sum(v) / len(v))

    indoor = roof == "fixed"
    label, _diff = ("Indoor", None) if indoor else wind_effect(wdir, azimuth, wind)

    return {
        "venue_name":              venue_name,
        "roof_status":             {"fixed": "Dome", "retractable": "Retractable",
                                    "open": "Open Air"}[roof],
        "temperature":             round(temp, 1) if temp is not None else None,
        "humidity":                round(hum) if hum is not None else None,
        "elevation_ft":            elevation_ft,
        # Indoors nothing outside matters; reporting a rain chance under a fixed roof
        # is how a dry dome game ends up disqualifying its own pitcher overs.
        "precip_probability":      None if indoor else precip,
        "precip_risk_during_game": (not indoor) and precip is not None and precip >= 50,
        "wind_speed":              None if indoor else (round(wind, 1) if wind is not None else None),
        "wind_direction":          None if indoor else wdir,
        "wind_direction_label":    "" if indoor else compass_point(wdir),
        "wind_effect_label":       label,
        "forecast_source":         "open-meteo hourly",
    }
