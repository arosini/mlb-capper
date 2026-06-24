#!/usr/bin/env python3
"""
Download Handigraphs data for a given date.

Run directly:
  python download.py                  # today
  python download.py --date tomorrow  # tomorrow's starters
  python download.py --inspect        # show raw JSON field names (run once to map fields)

Or call download_all() from handicap.py via --refresh.
"""

import json
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' not installed.  Run: pip install requests")

try:
    import config
except ImportError:
    sys.exit("ERROR: config.py not found.")

# Output filenames — {date} = YYYY-MM-DD, {slot} = today|tomorrow
FILE_NAMES = {
    "starters":        "starters_last3g_{slot}_{date}.json",
    "team_rhp":        "team_stats_L12RHP_{date}.json",
    "team_lhp":        "team_stats_L12LHP_{date}.json",
    "bullpen":         "bullpen_stats_last12g_{date}.json",
    "ballpark_weather": "ballpark_weather_{date}.json",
}


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
    s.headers["Accept"] = "application/json"
    return s


def login(session: requests.Session) -> bool:
    """
    Login to Handigraphs.  Tries JSON body first (common for React SPAs),
    falls back to form data.  Handles both cookie-based and Bearer-token auth.
    """
    if not config.HANDIGRAPHS_EMAIL or not config.HANDIGRAPHS_PASSWORD:
        print("ERROR: Set HANDIGRAPHS_EMAIL and HANDIGRAPHS_PASSWORD in config.py")
        return False

    payload = {
        "email": config.HANDIGRAPHS_EMAIL,
        "password": config.HANDIGRAPHS_PASSWORD,
    }

    try:
        # Try JSON POST first (most React SPAs)
        r = session.post(config.HANDIGRAPHS_LOGIN_URL, json=payload, timeout=10)

        if r.status_code == 404:
            # Login URL might be different — common alternatives
            for alt in ["/api/login", "/api/user/login", "/auth/login"]:
                r = session.post(
                    config.HANDIGRAPHS_BASE_URL + alt,
                    json=payload,
                    timeout=10,
                )
                if r.status_code != 404:
                    break

        if not r.ok:
            print(f"  Login failed: HTTP {r.status_code}")
            print(f"  Response: {r.text[:300]}")
            return False

        # Extract Bearer token if returned in JSON body
        try:
            data = r.json()
            token = (
                data.get("token")
                or data.get("accessToken")
                or data.get("access_token")
                or (data.get("data") or {}).get("token")
            )
            if token:
                session.headers["Authorization"] = f"Bearer {token}"
                print(f"  Using Bearer token auth")
            else:
                print(f"  Using cookie-based auth")
        except ValueError:
            print(f"  Using cookie-based auth")

        return True

    except Exception as e:
        print(f"  Login error: {e}")
        return False


def _fetch(session: requests.Session, url: str) -> dict | list | None:
    try:
        r = session.get(url, timeout=30)
        if r.status_code == 401:
            print(f"  AUTH FAILED (401) for {url}")
            print(f"  The login endpoint may be wrong — check config.HANDIGRAPHS_LOGIN_URL")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Failed to fetch {url}: {e}")
        return None


def _parse_utc(ts: str) -> datetime:
    """Parse ISO timestamp to UTC datetime; returns epoch on failure."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _odds_age_minutes(data_dir: Path, date_str: str) -> float:
    """Return minutes since last odds fetch, or infinity if never fetched."""
    from datetime import timezone
    meta_path = data_dir / f"odds_meta_{date_str}.json"
    if not meta_path.exists():
        return float("inf")
    try:
        meta = json.loads(meta_path.read_text())
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
        return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60
    except Exception:
        return float("inf")


def download_odds(data_dir: Path, date_str: str, max_age_minutes: int = 180) -> None:
    """Fetch full-game odds from The Odds API (DK, FanDuel, Fanatics). No auth needed.
    Skips if odds were fetched within max_age_minutes (default 3 hours)."""
    key = config.ODDS_API_KEY
    if not key:
        print("  [odds] ODDS_API_KEY not set — skipping odds download")
        return
    age = _odds_age_minutes(data_dir, date_str)
    if age < max_age_minutes:
        print(f"  [odds] Fetched {age:.0f}m ago — skipping (refresh every {max_age_minutes}m)")
        return
    odds_path = data_dir / f"odds_{date_str}.json"
    url = (
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
        f"?apiKey={key}"
        "&regions=us"
        "&markets=h2h,spreads,totals"
        "&bookmakers=draftkings,fanduel,fanatics"
        "&oddsFormat=american"
        "&dateFormat=iso"
    )
    try:
        r = requests.get(url, timeout=15)
        remaining = r.headers.get("x-requests-remaining", "?")
        if not r.ok:
            print(f"  [odds] API error {r.status_code}: {r.text[:200]}")
            return
        new_data = r.json()
        # Merge: API drops started games, so preserve their odds from the old file
        now = datetime.now(timezone.utc)
        old_games = {}
        if odds_path.exists():
            try:
                for g in json.loads(odds_path.read_text()):
                    old_games[g["id"]] = g
            except Exception:
                pass
        new_ids = {g["id"] for g in new_data}
        started = [g for gid, g in old_games.items() if gid not in new_ids
                   and _parse_utc(g.get("commence_time", "")) < now]
        data = new_data + started
        odds_path.write_text(json.dumps(data, indent=2))
        meta = {"fetched_at": now.isoformat()}
        (data_dir / f"odds_meta_{date_str}.json").write_text(json.dumps(meta))
        print(f"  ✓  odds_{date_str}.json  ({len(new_data)} upcoming + {len(started)} started, {remaining} API calls remaining)")
    except Exception as e:
        print(f"  [odds] Failed: {e}")


def download_pitcher_props(data_dir: Path, date_str: str, max_age_minutes: int = 180) -> None:
    """Fetch pitcher K and outs props from the per-event endpoint (requires Starter plan+).
    Reads event IDs from the already-saved bulk odds file. Skips if fetched within max_age_minutes."""
    key = config.ODDS_API_KEY
    if not key:
        return
    age = _odds_age_minutes(data_dir, date_str)
    if age < max_age_minutes:
        print(f"  [props] Fetched {age:.0f}m ago — skipping")
        return
    props_path = data_dir / f"props_{date_str}.json"
    odds_path = data_dir / f"odds_{date_str}.json"
    if not odds_path.exists():
        print(f"  [props] No odds file — skipping pitcher props")
        return
    try:
        games = json.loads(odds_path.read_text())
    except Exception as e:
        print(f"  [props] Failed to read odds file: {e}")
        return

    now = datetime.now(timezone.utc)
    all_props: dict = {}
    for game in games:
        event_id = game.get("id")
        if not event_id:
            continue
        away, home = game.get("away_team", "?"), game.get("home_team", "?")
        if _parse_utc(game.get("commence_time", "")) < now:
            print(f"  [props] {away}@{home}: already started — skipping")
            continue
        url = (
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
            f"?apiKey={key}"
            "&regions=us"
            "&markets=pitcher_strikeouts,pitcher_outs"
            ",h2h_1st_5_innings,spreads_1st_5_innings,totals_1st_5_innings"
            "&bookmakers=draftkings,fanduel,fanatics"
            "&oddsFormat=american"
        )
        try:
            r = requests.get(url, timeout=15)
            remaining = r.headers.get("x-requests-remaining", "?")
            if r.status_code == 401 or r.status_code == 403:
                print(f"  [props] Auth error {r.status_code} — pitcher props may require Starter plan")
                return
            if not r.ok:
                print(f"  [props] {away}@{home}: API error {r.status_code}: {r.text[:120]}")
                continue
            all_props[event_id] = r.json()
            print(f"  [props] {away}@{home}: OK ({remaining} remaining)")
        except Exception as e:
            print(f"  [props] {away}@{home}: {e}")

    props_path.write_text(json.dumps(all_props, indent=2))
    print(f"  ✓  props_{date_str}.json ({len(all_props)} games)")


def download_all(target_date: date, data_dir: Path, slot: str = "today") -> bool:
    """Fetch all endpoints and save as JSON files.  Returns True if all succeeded."""
    date_str = target_date.strftime("%Y-%m-%d")
    data_dir.mkdir(parents=True, exist_ok=True)

    session = _build_session()
    print(f"Logging in as {config.HANDIGRAPHS_EMAIL}...")
    if not login(session):
        return False
    print("  Login OK")

    ok = True
    for key, url_tmpl in config.API_URLS.items():
        url = url_tmpl.format(slot=slot)
        fname = FILE_NAMES[key].format(date=date_str, slot=slot)
        dest = data_dir / fname
        print(f"  Fetching {key}...")
        data = _fetch(session, url)
        if data is None:
            ok = False
            continue
        dest.write_text(json.dumps(data, indent=2))
        # Count rows — starters uses {"starters": [...]}, others use list or {"data": [...]}
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("data") or data.get("starters") or []
        else:
            rows = []
        count = len(rows) if rows else "?"
        print(f"  ✓  {fname}  ({count} rows)")

    print("  Fetching odds...")
    download_odds(data_dir, date_str)

    print("  Fetching pitcher props...")
    download_pitcher_props(data_dir, date_str)

    return ok


def inspect_fields(data_dir: Path, target_date: date) -> None:
    """Print the field names from each saved JSON file so we can map them."""
    date_str = target_date.strftime("%Y-%m-%d")
    for key, fname_tmpl in FILE_NAMES.items():
        # Try both slots
        for slot in ("today", "tomorrow"):
            fname = fname_tmpl.format(date=date_str, slot=slot)
            p = data_dir / fname
            if p.exists():
                break
        else:
            print(f"\n{key}: no file found for {date_str}")
            continue

        raw = json.loads(p.read_text())
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            rows = raw.get("data") or raw.get("starters") or []
        else:
            rows = []
        if rows:
            first = rows[0]
            stats = first.get("stats")
            print(f"\n── {key}  ({p.name}, {len(rows)} rows) ──")
            for field, val in first.items():
                if field == "stats" and isinstance(val, dict):
                    print(f"  {'stats (nested):':<35}")
                    for sf, sv in val.items():
                        print(f"    {sf!r:33s}  {repr(sv)[:55]}")
                else:
                    print(f"  {field!r:35s}  {repr(val)[:60]}")
        else:
            print(f"\n{key}: unexpected structure — {type(raw)}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download Handigraphs data")
    ap.add_argument("--date", default="today", help="today, tomorrow, or YYYY-MM-DD")
    ap.add_argument("--data-dir", default="./data", help="Where to save files")
    ap.add_argument(
        "--inspect",
        action="store_true",
        help="Show field names from saved JSON files (run after first download)",
    )
    args = ap.parse_args()

    today = date.today()
    if args.date == "today":
        target, slot = today, "today"
    elif args.date == "tomorrow":
        target, slot = today + timedelta(days=1), "tomorrow"
    else:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        slot = "today"

    data_dir = Path(args.data_dir)

    if args.inspect:
        inspect_fields(data_dir, target)
    else:
        success = download_all(target, data_dir, slot)
        if success:
            print("\nRun with --inspect to see field names for handicap.py mapping.")
        sys.exit(0 if success else 1)
