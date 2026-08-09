"""Venue truth — where a game is *actually* played, and what that park is like.

The home team is not a reliable proxy for the ballpark. MLB plays a handful of games a
year at neutral sites (Field of Dreams, the Little League Classic in Williamsport,
Mexico City, the Tokyo and London series, Bristol Motor Speedway), and occasionally a
club spends a whole season somewhere else — Tampa Bay played all of 2025 at the Yankees'
spring park after hurricane damage. Scanning 2025+2026 finds 96 such games.

Anything keyed on the home team's *code* is therefore wrong for those games, silently:
you get the right venue name on the card and the wrong city's weather underneath it.

Detection is exact and needs no heuristics. Every game carries a `venue.id`, and every
club has a registered home `venue.id`; a mismatch IS a neutral site. Both of our feeds
already carry venue ids in MLB's namespace (verified: Handigraphs' `venue_id` resolves
against `/api/v1/teams` for every game checked), so this costs one cached call.

Coordinate sources are ranked by how much they can be trusted, because MLB's own venue
records degrade badly at exactly the one-off parks we care about — Estadio Alfredo Harp
Helú returns `latitude: null`, and Bristol Motor Speedway returns a latitude 5 degrees
off (31.5156 rather than 36.5156, ~350 miles into Georgia, while `state` correctly reads
Tennessee). We would rather show no weather than confidently wrong weather, so a venue
that fails the sanity check reports nothing.
"""

import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from season import ET, GAME_TYPES

MLB_API = "https://statsapi.mlb.com/api/v1"

# Parks with a fixed or retractable roof. Wind and rain readings are meaningless under
# a closed roof, and for retractables we cannot know the state without Handigraphs — so
# these are reported as roof-dependent rather than as open-air conditions.
FIXED_ROOF = {12}  # Tropicana Field
RETRACTABLE_ROOF = {
    15,    # Chase Field
    2392,  # Daikin Park
    4169,  # loanDepot park
    32,    # American Family Field
    680,   # T-Mobile Park
    5325,  # Globe Life Field
    14,    # Rogers Centre
}
ROOFED = FIXED_ROOF | RETRACTABLE_ROOF


def _cache_path(data_dir: Optional[Path], name: str) -> Optional[Path]:
    return None if data_dir is None else data_dir / name


def home_venue_ids(data_dir: Optional[Path] = None,
                   season: Optional[int] = None) -> dict:
    """{team_mlb_id: venue_id} — each club's registered home park for the season.

    Cached per season. This is what a game's venue is compared against, and it tracks
    relocations on its own: because MLB re-registers the club's home venue, the
    Athletics at Sutter Health Park read as a normal home game, while Tampa Bay's 2025
    season at Steinbrenner Field correctly reads as neutral throughout.
    """
    season = season or date.today().year
    cache = _cache_path(data_dir, f"home_venues_{season}.json")
    if cache is not None and cache.exists():
        try:
            return {int(k): v for k, v in json.loads(cache.read_text()).items()}
        except Exception:
            pass
    try:
        import requests
        r = requests.get(f"{MLB_API}/teams",
                         params={"sportId": 1, "season": season}, timeout=15)
        r.raise_for_status()
        out = {t["id"]: t["venue"]["id"]
               for t in r.json().get("teams", []) if t.get("venue", {}).get("id")}
    except Exception as e:
        print(f"[venues] home-venue lookup failed ({e})", file=sys.stderr)
        return {}
    if cache is not None and out:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({str(k): v for k, v in out.items()}))
        except Exception:
            pass
    return out


def coords_are_sane(lat, lon, venue_id=None) -> bool:
    """Reject coordinates we should not build a forecast on.

    Deliberately conservative: null island, out-of-range values, and the (0,0)-adjacent
    placeholders MLB uses for venues it has not geocoded. It cannot catch a plausible
    but wrong fix like Bristol's — for that, prefer a better source (see venue_geo).
    """
    if lat is None or lon is None:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    return abs(lat) > 0.01 or abs(lon) > 0.01


def venue_geo(venue_id: int, data_dir: Optional[Path] = None) -> dict:
    """{name, lat, lon, elevation_ft, azimuth, city, country} for a venue id.

    Cached per venue — these are static. Returns {} when the venue has no usable
    coordinates, which the caller must treat as "no weather" rather than falling back
    to the home team's usual park.
    """
    cache = _cache_path(data_dir, f"venue_{venue_id}.json")
    if cache is not None and cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    try:
        import requests
        r = requests.get(f"{MLB_API}/venues/{venue_id}",
                         params={"hydrate": "location"}, timeout=15)
        r.raise_for_status()
        v = (r.json().get("venues") or [{}])[0]
    except Exception as e:
        print(f"[venues] venue {venue_id} lookup failed ({e})", file=sys.stderr)
        return {}

    loc = v.get("location") or {}
    c = loc.get("defaultCoordinates") or {}
    lat, lon = c.get("latitude"), c.get("longitude")
    if not coords_are_sane(lat, lon, venue_id):
        return {}
    out = {
        "venue_id":     venue_id,
        "name":         v.get("name", ""),
        "lat":          float(lat),
        "lon":          float(lon),
        "elevation_ft": loc.get("elevation"),
        "azimuth":      loc.get("azimuthAngle"),
        "city":         loc.get("city", ""),
        "country":      loc.get("country", ""),
    }
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out))
        except Exception:
            pass
    return out


def roof_kind(venue_id: Optional[int]) -> str:
    """'fixed' | 'retractable' | 'open' — what kind of roof the park has."""
    if venue_id in FIXED_ROOF:
        return "fixed"
    if venue_id in RETRACTABLE_ROOF:
        return "retractable"
    return "open"
