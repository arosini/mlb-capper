"""Season window — Eastern-time clock, MLB game types, and in-season detection.

No project dependencies; every other module imports ET from here.

Two things live here because getting either wrong breaks the site silently:

1. **ET is a real timezone, not a fixed -4 offset.** Every module used to define
   `_ET = timezone(timedelta(hours=-4))`, which is EDT. That is correct from mid-March
   to early November and wrong the rest of the year — including the back half of the
   World Series, when ET is UTC-5. A fixed offset shifts every displayed start time by
   an hour and, between midnight and 1 AM EST, resolves "today" to the wrong date.

2. **The MLB schedule API filters by gameType, and "R" means regular season only.**
   `handicap.py` treats the MLB schedule as the authoritative game list and skips any
   game missing from it, so a regular-season-only filter would have emptied the entire
   page for all of October. Postseason types are F (wild card), D (division series),
   L (LCS) and W (World Series). Spring training (S), exhibition (E) and the All-Star
   game (A) stay excluded on purpose — the site is for regular and postseason only.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import json
import sys

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - only if tzdata is missing
    ET = timezone(timedelta(hours=-4))

# Regular season + postseason. Deliberately excludes S (spring), E (exhibition),
# A (all-star) — see module docstring.
GAME_TYPES = "R,F,D,L,W"

MLB_API = "https://statsapi.mlb.com/api/v1"

# Outside this window MLB plays no regular or postseason games in any year, so we can
# answer "off-season" without a network call. Inside it we always ask the API — the
# real boundaries (opening day, the end of the World Series) move by a week or more
# from year to year, and guessing them is how you end up with a blank page on opening
# day or a live API bill in February.
_SEASON_FIRST_MONTH_DAY = (3, 15)   # earliest plausible opening day
_SEASON_LAST_MONTH_DAY  = (11, 15)  # latest plausible World Series game


def today_et() -> date:
    """Current date in Eastern time — the calendar the whole site runs on."""
    return datetime.now(ET).date()


def _plausibly_in_season(d: date) -> bool:
    return _SEASON_FIRST_MONTH_DAY <= (d.month, d.day) <= _SEASON_LAST_MONTH_DAY


def game_count(target_date: date, data_dir: Optional[Path] = None) -> int:
    """Number of regular/postseason MLB games scheduled on target_date.

    Cached to data/season_{date}.json so the three handicap.py invocations per run
    (today HTML, tomorrow HTML, suggestions) share one lookup. Returns 0 outside the
    plausible season window without touching the network.

    On a network failure this returns -1 ("unknown"), NOT 0. Callers must treat
    unknown as in-season: a transient statsapi outage should degrade to a normal run,
    never to a silent off-season skip that stops publishing picks mid-August.
    """
    if not _plausibly_in_season(target_date):
        return 0

    cache = None
    if data_dir is not None:
        cache = data_dir / f"season_{target_date.isoformat()}.json"
        if cache.exists():
            try:
                return int(json.loads(cache.read_text())["games"])
            except Exception:
                pass

    try:
        import requests
        r = requests.get(
            f"{MLB_API}/schedule",
            params={"sportId": 1, "date": target_date.isoformat(),
                    "gameType": GAME_TYPES},
            timeout=10,
        )
        r.raise_for_status()
        n = int(r.json().get("totalGames") or 0)
    except Exception as e:
        print(f"[season] schedule lookup failed ({e}) — assuming in-season",
              file=sys.stderr)
        return -1

    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"games": n}))
        except Exception:
            pass
    return n


def has_games(target_date: date, data_dir: Optional[Path] = None) -> bool:
    """True when MLB is playing on target_date, or when we could not find out.

    Fails open by design — see game_count().
    """
    return game_count(target_date, data_dir) != 0


def season_year(d: date) -> int:
    """The season a date belongs to. MLB never crosses New Year, so this is the year."""
    return d.year


def season_start(d: date) -> date:
    """First date that can belong to d's season — the floor for same-season windows.

    Used to keep last-season's games out of this-season's trend records. Deliberately
    earlier than any real opening day: it only has to separate seasons, not name the
    exact first game.
    """
    return date(d.year, *_SEASON_FIRST_MONTH_DAY)
