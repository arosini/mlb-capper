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
                "series_game_number": g.get("seriesGameNumber"),
                "games_in_series":    g.get("gamesInSeries"),
            }
    # How many legs this matchup has today. A doubleheader's two games share a matchup
    # string ("ARI @ SFG"), which is the only game identity that reaches the AI card and
    # the pick log — so every consumer needs to know a second leg exists.
    legs: dict = {}
    for (pair, _gn) in games:
        legs[pair] = legs.get(pair, 0) + 1
    for (pair, _gn), info in games.items():
        info["games_today"] = legs[pair]
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


def get_bat_sides(player_ids) -> dict:
    """Batch-fetch batting side for a set of MLB player ids → {id_str: 'L'|'R'|'S'}.

    Same shape and same single-request budget as get_pitch_hands, against the same
    endpoint — the two are separate calls only because the id sets are disjoint and a
    slate's hitters do not fit in the probables chunk.
    """
    ids = sorted({str(p) for p in player_ids if p})
    if not HAS_REQUESTS or not ids:
        return {}
    sides: dict[str, str] = {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        try:
            r = requests.get(
                f"{MLB_API}/people",
                params={"personIds": ",".join(chunk)},
                timeout=10,
            )
            r.raise_for_status()
            for person in r.json().get("people", []):
                code = (person.get("batSide") or {}).get("code", "")
                if code:
                    sides[str(person.get("id"))] = code
        except Exception as e:
            print(f"Warning: bat-side lookup failed ({e})", file=sys.stderr)
    return sides


def get_lineups(target_date) -> dict:
    """Today's posted lineups → {(frozenset(codes), game_number): {"away": [...], ...}}.

    Every offense number on the card is a TEAM-level last-6 that is blind to who is
    actually in the box today, so a club missing its best two bats reads identically to
    one at full strength. This does not fix that — no per-player offense data reaches
    this project — but it does two things the card could not do at all before: it says
    whether a lineup is posted yet, so the model knows whether "no lineup" means
    anything, and it carries the batting-side composition, which is the one lineup fact
    the platoon splits everything else is built on can actually be checked against.

    MLB posts these an hour or two before first pitch, so the early runs legitimately get
    nothing back. Keyed to match get_mlb_schedule().
    """
    if not HAS_REQUESTS:
        return {}
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": target_date.strftime("%Y-%m-%d"),
                "hydrate": "lineups,team",
                "gameType": GAME_TYPES,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Warning: lineup lookup failed ({e})", file=sys.stderr)
        return {}

    out: dict = {}
    for day in data.get("dates", []):
        for game in day.get("games", []):
            lu = game.get("lineups") or {}
            if not lu.get("awayPlayers") and not lu.get("homePlayers"):
                continue
            teams = game.get("teams", {})
            away = (teams.get("away", {}).get("team") or {}).get("abbreviation", "")
            home = (teams.get("home", {}).get("team") or {}).get("abbreviation", "")
            if not away or not home:
                continue
            key = (frozenset([away, home]), game.get("gameNumber") or 1)
            out[key] = {
                "away": [{"id": str(p.get("id")), "name": p.get("fullName", ""),
                          "pos": (p.get("primaryPosition") or {}).get("abbreviation", "")}
                         for p in lu.get("awayPlayers", [])],
                "home": [{"id": str(p.get("id")), "name": p.get("fullName", ""),
                          "pos": (p.get("primaryPosition") or {}).get("abbreviation", "")}
                         for p in lu.get("homePlayers", [])],
            }
    return out


def get_pitch_hands(player_ids) -> dict:
    """Batch-fetch throwing hand for a set of MLB player ids → {id_str: 'L' | 'R'}.

    The schedule's probablePitcher hydrate carries only id/fullName/link, so a probable
    who has no Handigraphs row arrives with no hand at all — and a pitcher with no hand
    means the opposing club's offense card has no platoon split to render and comes up
    empty. One /people call covers the whole slate, so this costs a single request no
    matter how many games are missing a hand.
    """
    ids = sorted({str(p) for p in player_ids if p})
    if not HAS_REQUESTS or not ids:
        return {}
    hands: dict[str, str] = {}
    # /people caps the id list; 40 keeps a full slate's probables inside it comfortably.
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        try:
            r = requests.get(
                f"{MLB_API}/people",
                params={"personIds": ",".join(chunk)},
                timeout=10,
            )
            r.raise_for_status()
            for person in r.json().get("people", []):
                code = (person.get("pitchHand") or {}).get("code", "")
                if code:
                    hands[str(person.get("id"))] = code
        except Exception as e:
            print(f"Warning: pitch-hand lookup failed ({e})", file=sys.stderr)
    return hands


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
                "opp_abbr":     opp.get("team", {}).get("abbreviation", ""),
            })
    return results


def _relief_ip_by_team(game_pk, t_home, t_away) -> dict:
    """Relief innings by team id for one completed game, from its boxscore.

    Returns {} if the boxscore cannot be read, so the caller neither caches nor
    counts the game rather than recording it as zero relief innings.
    """
    try:
        rb = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        rb.raise_for_status()
        bs = rb.json()
    except Exception:
        return {}

    out: dict = {}
    for side, team_id in [("home", t_home), ("away", t_away)]:
        if team_id is None:
            continue
        t = bs.get("teams", {}).get(side, {})
        out[str(team_id)] = round(sum(
            mlb_ip_to_real(str(
                t.get("players", {}).get(f"ID{pid}", {})
                 .get("stats", {}).get("pitching", {}).get("inningsPitched", "0")
            ))
            for pid in t.get("pitchers", [])
            if int(t.get("players", {}).get(f"ID{pid}", {})
                    .get("stats", {}).get("pitching", {}).get("gamesStarted", 0)) == 0
        ), 2)
    return out


def get_bullpen_stress(team_mlb_ids: set, target_date: date, data_dir: Path) -> dict:
    """Fetch 2-day bullpen usage via MLB boxscores.

    Returns {team_mlb_id: {"ip": float, "games": int, "label": str, "css": str}}.

    The cache in data_dir/bullpen_stress_{date}.json holds relief innings PER GAME,
    and only games that have reached Final are ever written to it. The aggregate is
    recomputed on every call from the games the window currently contains.

    That structure is load-bearing, and a whole-window cache written once per date
    was silently wrong. `/tomorrow/` is rendered on every deploy, including the 3:30
    AM ET run, and its target date is tomorrow — so it wrote bullpen_stress_{D+1}
    with the window [D-1, D] at a moment when none of day D's games had been played.
    The workflow's cleanup step preserves tomorrow-dated files, so that file survived
    into day D+1 as *today's* file and was served straight from cache: every club's
    "2d stress" on D+1 described one game from D-1 and omitted D entirely.
    Caching per game means an in-progress day simply completes on the next run.
    """
    if not HAS_REQUESTS or not team_mlb_ids:
        return {}

    start = (target_date - timedelta(days=2)).isoformat()
    end   = (target_date - timedelta(days=1)).isoformat()

    cache_path = data_dir / f"bullpen_stress_{target_date.isoformat()}.json"
    by_game: dict = {}
    if cache_path.exists():
        try:
            blob = json.loads(cache_path.read_text())
            # Pre-2026-08-30 files hold a team-keyed aggregate with no "by_game";
            # they read as empty and are simply refetched.
            for pk, rec in (blob.get("by_game") or {}).items():
                if isinstance(rec, dict) and isinstance(rec.get("teams"), dict):
                    by_game[str(pk)] = rec
        except Exception:
            pass

    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={"sportId": 1, "startDate": start, "endDate": end, "gameType": GAME_TYPES},
            timeout=10,
        )
        r.raise_for_status()
        dates = r.json().get("dates", [])
    except Exception as e:
        # Fall through on whatever the cache already holds rather than blanking the
        # stress line on every card for a transient statsapi failure.
        print(f"Warning: bullpen stress fetch failed: {e}", file=sys.stderr)
        dates = []

    for date_entry in dates:
        game_date = date_entry.get("date", "")
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            pk = str(g.get("gamePk"))
            if pk in by_game:
                continue
            teams  = g.get("teams", {})
            t_home = teams.get("home", {}).get("team", {}).get("id")
            t_away = teams.get("away", {}).get("team", {}).get("id")
            relief = _relief_ip_by_team(pk, t_home, t_away)
            if relief:
                by_game[pk] = {"date": game_date, "teams": relief}

    # Aggregate fresh every call: the cache is per game, so it serves any team set
    # and any window without being rewritten for each of them.
    in_window = {
        pk: rec for pk, rec in by_game.items()
        if start <= str(rec.get("date", "")) <= end
    }
    ip_by_team: dict    = {}
    games_by_team: dict = {}
    for rec in in_window.values():
        for tid_s, ip in rec["teams"].items():
            try:
                tid = int(tid_s)
            except (TypeError, ValueError):
                continue
            ip_by_team[tid]    = ip_by_team.get(tid, 0.0) + float(ip or 0.0)
            games_by_team[tid] = games_by_team.get(tid, 0) + 1

    result: dict = {}
    for team_id in team_mlb_ids:
        ip     = ip_by_team.get(team_id, 0.0)
        games  = games_by_team.get(team_id, 0)
        label, css = stress_label_cls(ip, games)
        result[team_id] = {"ip": round(ip, 1), "games": games, "label": label, "css": css}

    try:
        # Pruned to the window so the file does not grow across a season of reruns.
        cache_path.write_text(json.dumps({"by_game": in_window}))
    except Exception:
        pass

    return result


# ── Weather (Open-Meteo) ──────────────────────────────────────────────────────

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
