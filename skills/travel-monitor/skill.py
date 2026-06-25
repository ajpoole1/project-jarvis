"""
Travel monitor skill — flexible-date travel cost tracker.

Scans three independent lanes per watch:
  - Flights: Amadeus Flight Cheapest Date Search (triage) + Offers Search (confirm)
  - Hotels: Firecrawl scrape of named property by-provider + date-calendar view
  - Packages: Firecrawl scrape of operator flex-date search (costco/aircanada/westjet/expedia)

Reconciles package vs. DIY (flight + hotel); alerts on deals via Discord.
Alerts only — never books anything. Silent run unless a deal or health alert fires.

Stdlib only; no virtualenv needed.

Phase 0: scaffold — DB schema, command-surface stub, env wiring.
Phase 1: Amadeus flights lane — OAuth2, Cheapest Date Search (triage), Offers Search (confirm).
Phase 2: Hotels lane — Firecrawl JSON-extract of Google Hotels price calendar per named property.
Phase 3: Packages lane — Firecrawl JSON-extract of operator flex-date search (costco/aircanada/westjet/expedia).
Phase 4: Reconciliation + deal detection + Discord alert.
Phase 5: Health/degradation hardening (fail_count, degraded status) + cadence escalation (weekly→daily).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"
_LOCAL_TZ = ZoneInfo("America/Toronto")
_DISCORD_SCRIPT = Path(__file__).parents[2] / "scripts" / "discord_post.py"
_SKILL_ROOT = Path(__file__).parents[1]

# Manual .env parse — no dotenv dependency
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DEAL_DROP_PCT = float(os.environ.get("TRAVEL_MONITOR_DEAL_DROP_PCT", "10"))
N_MEDIAN = int(os.environ.get("TRAVEL_MONITOR_N_MEDIAN", "8"))
N_DAY_LOW = int(os.environ.get("TRAVEL_MONITOR_N_DAY_LOW", "30"))
ESCALATE_DAYS = int(os.environ.get("TRAVEL_MONITOR_ESCALATE_DAYS", "21"))
DEGRADE_AFTER = int(os.environ.get("TRAVEL_MONITOR_DEGRADE_AFTER", "3"))

VALID_OPERATORS = {"costco", "aircanada", "westjet", "expedia"}
VALID_CADENCES = {"weekly", "daily"}
VALID_RESOLVE_ACTIONS = {"booked", "passed"}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS travel_watches (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            destination        TEXT NOT NULL,
            origin_iata        TEXT NOT NULL,
            window_start       TEXT NOT NULL,
            window_end         TEXT NOT NULL,
            stay_nights        INTEGER NOT NULL,
            adults             INTEGER NOT NULL DEFAULT 2,
            children           INTEGER NOT NULL DEFAULT 0,
            hotel_targets      TEXT NOT NULL DEFAULT '[]',
            package_operators  TEXT NOT NULL DEFAULT '[]',
            target_price       REAL,
            currency           TEXT NOT NULL DEFAULT 'CAD',
            cadence            TEXT NOT NULL DEFAULT 'weekly',
            status             TEXT NOT NULL DEFAULT 'active',
            fail_count         INTEGER NOT NULL DEFAULT 0,
            active_from        TEXT,
            active_until       TEXT,
            created_at         TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS travel_price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id    INTEGER NOT NULL,
            checked_at  TEXT NOT NULL,
            lane        TEXT NOT NULL,
            best_price  REAL,
            currency    TEXT NOT NULL DEFAULT 'CAD',
            checkin     TEXT,
            checkout    TEXT,
            provider    TEXT,
            raw         TEXT,
            method      TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _discord_post(message: str) -> None:
    if not _DISCORD_SCRIPT.exists():
        print(f"[travel-monitor] discord_post.py not found at {_DISCORD_SCRIPT}", file=sys.stderr)
        return
    try:
        result = subprocess.run(
            ["python3", str(_DISCORD_SCRIPT)],
            input=message,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(f"[travel-monitor] discord_post failed: {result.stderr.strip()}", file=sys.stderr)
    except Exception as exc:
        print(f"[travel-monitor] discord_post exception: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Follow-ups integration
# ---------------------------------------------------------------------------


def _create_followup(name: str, note: str, trigger_at: str, window_until: str | None) -> None:
    followup_py = _SKILL_ROOT / "followups" / "skill.py"
    if not followup_py.exists():
        print(f"[travel-monitor] followups skill not found at {followup_py}", file=sys.stderr)
        return
    cmd = [
        "python3",
        str(followup_py),
        "add",
        "--subject",
        f"Travel check: {name}",
        "--prompt",
        note,
        "--policy",
        "periodic",
        "--trigger-at",
        trigger_at,
        "--cadence",
        "7",
        "--source",
        "open_loop",
    ]
    if window_until:
        cmd += ["--window-until", window_until]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            print(
                f"[travel-monitor] follow-up creation failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[travel-monitor] follow-up exception: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _today_local() -> str:
    return datetime.now(_LOCAL_TZ).date().isoformat()


def _parse_args(args: list[str]) -> dict[str, list[str] | str]:
    """
    Parse --key value pairs. Keys that appear multiple times accumulate as lists
    (hotel, operator). All other keys take the last value.
    """
    multi_keys = {"hotel", "operator"}
    params: dict[str, list[str] | str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            key = args[i][2:].replace("-", "_")
            val = args[i + 1]
            if key in multi_keys:
                existing = params.get(key, [])
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    existing = [existing, val]
                params[key] = existing
            else:
                params[key] = val
            i += 2
        else:
            i += 1
    return params


# ---------------------------------------------------------------------------
# Amadeus helpers (Phase 1)
# ---------------------------------------------------------------------------

# Module-level token cache — valid for one process lifetime (~30 min TTL is fine
# since a check run completes in well under that).
_amadeus_token_cache: dict[str, str | float] = {}

_AMADEUS_BASE = "https://api.amadeus.com"
_FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"


def _amadeus_token() -> str:
    """Fetch or return cached Amadeus OAuth2 bearer token."""
    cached = _amadeus_token_cache.get("token")
    if cached:
        return str(cached)

    client_id = os.environ.get("AMADEUS_CLIENT_ID", "")
    client_secret = os.environ.get("AMADEUS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET not set")

    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(
        f"{_AMADEUS_BASE}/v1/security/oauth2/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"Amadeus token response missing access_token: {data}")
    _amadeus_token_cache["token"] = token
    return token


def _amadeus_get(path: str, params: dict) -> dict:
    """GET against the Amadeus production API with bearer auth. Retries once on 401."""
    for attempt in range(2):
        token = _amadeus_token()
        url = f"{_AMADEUS_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                # Token expired or revoked mid-run — evict cache and retry once
                _amadeus_token_cache.clear()
                continue
            raise


def _amadeus_cheapest_dates(
    origin: str, destination: str, window_start: str, window_end: str
) -> dict[str, float]:
    """
    Flight Cheapest Date Search — one call across the full window.
    Returns {YYYY-MM-DD: round_trip_price_CAD} for departure dates in the window.
    """
    params = {
        "origin": origin,
        "destination": destination,
        "departureDate": f"{window_start},{window_end}",
        "currencyCode": "CAD",
        "oneWay": "false",
    }
    data = _amadeus_get("/v1/shopping/flight-dates", params)
    result: dict[str, float] = {}
    for item in data.get("data", []):
        dep_date = item.get("departureDate", "")
        price = item.get("price", {}).get("total")
        if dep_date and price is not None:
            try:
                result[dep_date] = float(price)
            except (TypeError, ValueError):
                pass
    return result


def _amadeus_flight_offers(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str,
    adults: int,
    children: int,
) -> list[dict]:
    """
    Flight Offers Search (live prices) for a single departure+return date pair.
    Returns list of {price: float, carrier: str} sorted cheapest-first; max 3.
    """
    params: dict = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date,
        "returnDate": return_date,
        "adults": adults,
        "currencyCode": "CAD",
        "max": 3,
    }
    if children:
        params["children"] = children
    data = _amadeus_get("/v2/shopping/flight-offers", params)
    offers = []
    for offer in data.get("data", []):
        try:
            price = float(offer["price"]["grandTotal"])
        except (KeyError, TypeError, ValueError):
            continue
        carrier_codes = set()
        for itin in offer.get("itineraries", []):
            for seg in itin.get("segments", []):
                c = seg.get("carrierCode", "")
                if c:
                    carrier_codes.add(c)
        carrier = "/".join(sorted(carrier_codes)) or "unknown"
        offers.append({"price": price, "carrier": carrier})
    return sorted(offers, key=lambda x: x["price"])[:3]


# ---------------------------------------------------------------------------
# Lane — flights (Phase 1)
# ---------------------------------------------------------------------------


def _lane_flights(watch: dict, conn: sqlite3.Connection | None = None) -> dict:
    """
    Amadeus flights lane.

    Phase A (triage): Flight Cheapest Date Search → {date: price} map across the window.
    Phase B (confirm): Flight Offers Search for the cheapest 1–3 candidate dates (live prices).

    Cost rule: one cheapest-dates call (server-side sweep), then ≤3 offers calls.
    Never loops all candidate dates with discrete calls.
    """
    origin = watch["origin_iata"]
    destination = watch["destination"]
    window_start = watch["window_start"]
    window_end = watch["window_end"]
    stay_nights = int(watch["stay_nights"])
    adults = int(watch.get("adults", 2))
    children = int(watch.get("children", 0))
    watch_id = watch.get("id")

    # Confirm window boundary for valid departure dates:
    # last valid departure = window_end - stay_nights (so return still falls in window)
    try:
        ws_date = date.fromisoformat(window_start)
        we_date = date.fromisoformat(window_end)
    except ValueError as exc:
        return {"lane": "flight", "status": "unavailable", "reason": f"bad_dates: {exc}"}

    last_dep = we_date - timedelta(days=stay_nights)
    if last_dep < ws_date:
        return {
            "lane": "flight",
            "status": "unavailable",
            "reason": "window_too_short_for_stay",
        }

    # Phase A — triage
    try:
        triage = _amadeus_cheapest_dates(origin, destination, window_start, last_dep.isoformat())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:300]
        except Exception:
            pass
        reason = f"amadeus_http_{exc.code}: {body}"
        print(f"[travel-monitor] flights triage HTTP error: {reason}", file=sys.stderr)
        return {"lane": "flight", "status": "unavailable", "reason": reason}
    except Exception as exc:
        reason = f"amadeus_error: {exc}"
        print(f"[travel-monitor] flights triage error: {exc}", file=sys.stderr)
        return {"lane": "flight", "status": "unavailable", "reason": reason}

    if not triage:
        return {"lane": "flight", "status": "unavailable", "reason": "no_triage_results"}

    # Pick up to 3 cheapest candidate departure dates
    candidates = sorted(triage.items(), key=lambda kv: kv[1])[:3]

    # Phase B — confirm each candidate with live offers
    checked_at = _now_utc().isoformat()
    results: list[dict] = []

    for dep_str, triage_price in candidates:
        try:
            dep_d = date.fromisoformat(dep_str)
        except ValueError:
            continue
        ret_d = dep_d + timedelta(days=stay_nights)
        try:
            offers = _amadeus_flight_offers(
                origin, destination, dep_str, ret_d.isoformat(), adults, children
            )
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode(errors="replace")[:200]
            except Exception:
                pass
            print(
                f"[travel-monitor] offers HTTP error for {dep_str}: {exc.code} {body}",
                file=sys.stderr,
            )
            continue
        except Exception as exc:
            print(f"[travel-monitor] offers error for {dep_str}: {exc}", file=sys.stderr)
            continue

        if not offers:
            continue

        best = offers[0]
        row = {
            "checkin": dep_str,
            "checkout": ret_d.isoformat(),
            "price": best["price"],
            "carrier": best["carrier"],
            "triage_price": triage_price,
        }
        results.append(row)

        # Persist to travel_price_history if conn provided (scheduled check run)
        if conn is not None and watch_id is not None:
            _record_price(
                conn,
                watch_id,
                checked_at,
                "flight",
                best["price"],
                dep_str,
                ret_d.isoformat(),
                best["carrier"],
                offers,
                "amadeus_offers",
            )

    if conn is not None:
        try:
            conn.commit()
        except Exception as exc:
            print(f"[travel-monitor] flights DB commit error: {exc}", file=sys.stderr)

    if not results:
        return {"lane": "flight", "status": "unavailable", "reason": "no_offers_returned"}

    return {"lane": "flight", "status": "ok", "results": results}


# ---------------------------------------------------------------------------
# Shared DB helper
# ---------------------------------------------------------------------------


def _record_price(
    conn: sqlite3.Connection,
    watch_id: int,
    checked_at: str,
    lane: str,
    price: float,
    checkin: str,
    checkout: str,
    provider: str,
    raw: object,
    method: str,
) -> None:
    conn.execute(
        """INSERT INTO travel_price_history
           (watch_id, checked_at, lane, best_price, currency, checkin, checkout,
            provider, raw, method)
           VALUES (?, ?, ?, ?, 'CAD', ?, ?, ?, ?, ?)""",
        (watch_id, checked_at, lane, price, checkin, checkout, provider, json.dumps(raw), method),
    )


# ---------------------------------------------------------------------------
# Firecrawl helper (Phase 2+)
# ---------------------------------------------------------------------------


def _firecrawl_extract(url: str, schema: dict, prompt: str) -> tuple[dict | None, str | None]:
    """
    Firecrawl JSON extraction (stealth + JS rendering + LLM schema parse).
    ~5–9 credits per call. Returns (extracted_dict, None) or (None, error_type).

    error_type values: 'no_key', 'quota', 'rate_limit', 'parse_error', 'error'
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return None, "no_key"

    payload = json.dumps(
        {
            "url": url,
            "formats": ["extract"],
            "extract": {"schema": schema, "prompt": prompt},
        }
    ).encode()
    req = urllib.request.Request(
        _FIRECRAWL_SCRAPE_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            if "credit" in body.lower() or "quota" in body.lower():
                return None, "quota"
        except Exception:
            pass
        if exc.code == 402:
            return None, "quota"
        if exc.code == 429:
            return None, "rate_limit"
        print(f"[travel-monitor] firecrawl HTTP {exc.code}: {body[:200]}", file=sys.stderr)
        return None, "error"
    except Exception as exc:
        print(f"[travel-monitor] firecrawl error: {exc}", file=sys.stderr)
        return None, "error"

    if not data.get("success"):
        err = data.get("error", "unknown")
        print(f"[travel-monitor] firecrawl success=false: {err}", file=sys.stderr)
        return None, "error"

    raw = data.get("data")
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    extracted = (raw or {}).get("extract") if isinstance(raw, dict) else None
    if not isinstance(extracted, dict):
        return None, "parse_error"

    return extracted, None


# ---------------------------------------------------------------------------
# Lane — hotels (Phase 2)
# ---------------------------------------------------------------------------

# Google Hotels URL: search for a named property with a date range.
# The page renders a price calendar covering the window server-side.
# One scrape per property per run (cost rule).
_GOOGLE_HOTELS_SEARCH = "https://www.google.com/travel/hotels"

_HOTEL_SCHEMA = {
    "type": "object",
    "properties": {
        "hotel_name": {"type": "string"},
        "stays": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "checkin": {"type": "string", "description": "YYYY-MM-DD"},
                    "checkout": {"type": "string", "description": "YYYY-MM-DD"},
                    "price_total": {
                        "type": "number",
                        "description": "Total price in CAD for the stay",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Booking provider name, e.g. Booking.com",
                    },
                },
                "required": ["checkin", "checkout", "price_total"],
            },
        },
    },
    "required": ["stays"],
}

_HOTEL_PROMPT = (
    "Extract all available hotel stay options shown on this page. "
    "For each available check-in date shown in the price calendar, extract: "
    "the check-in date (YYYY-MM-DD), checkout date (YYYY-MM-DD), "
    "the lowest total price shown in CAD, and the cheapest provider name. "
    "Include only dates that have a price shown. "
    "All prices must be in CAD — if shown in another currency, convert or skip."
)


def _google_hotels_url(
    hotel_name: str, window_start: str, window_end: str, stay_nights: int
) -> str:
    """Build a Google Hotels search URL for a named property with date range."""
    params = urllib.parse.urlencode(
        {
            "q": hotel_name,
            "dates": f"{window_start},{window_end}",
            "nights": stay_nights,
            "curr": "CAD",
        }
    )
    return f"{_GOOGLE_HOTELS_SEARCH}?{params}"


def _lane_hotels(watch: dict, conn: sqlite3.Connection | None = None) -> dict:
    """
    Hotels lane: Firecrawl JSON-extract of Google Hotels for each named property.

    One scrape call per hotel_target per run (server-side date calendar covers the
    full window — no per-date looping). One retry on parse_error.

    Returns {lane: 'hotel', status: 'ok', results: [{hotel, checkin, checkout, price, provider}]}
    or {status: 'unavailable', reason: '...'} on failure.
    """
    hotel_targets: list[str] = watch.get("hotel_targets", [])
    if not hotel_targets:
        return {"lane": "hotel", "status": "unavailable", "reason": "no_hotel_targets"}

    window_start = watch["window_start"]
    window_end = watch["window_end"]
    stay_nights = int(watch["stay_nights"])
    watch_id = watch.get("id")
    checked_at = _now_utc().isoformat()

    all_results: list[dict] = []
    any_failure = False

    for hotel_name in hotel_targets:
        url = _google_hotels_url(hotel_name, window_start, window_end, stay_nights)

        extracted, err = _firecrawl_extract(url, _HOTEL_SCHEMA, _HOTEL_PROMPT)

        # One retry on parse error (page may have rendered partially)
        if err == "parse_error":
            print(
                f"[travel-monitor] hotels parse_error for {hotel_name!r}, retrying",
                file=sys.stderr,
            )
            extracted, err = _firecrawl_extract(url, _HOTEL_SCHEMA, _HOTEL_PROMPT)

        if err or extracted is None:
            print(
                f"[travel-monitor] hotels lane failed for {hotel_name!r}: {err}",
                file=sys.stderr,
            )
            any_failure = True
            continue

        stays = extracted.get("stays", [])
        if not stays:
            print(
                f"[travel-monitor] hotels: no stays extracted for {hotel_name!r}",
                file=sys.stderr,
            )
            any_failure = True
            continue

        for stay in stays:
            checkin = stay.get("checkin", "")
            checkout = stay.get("checkout", "")
            price_raw = stay.get("price_total")
            provider = stay.get("provider", "")

            if not checkin or not checkout or price_raw is None:
                continue
            try:
                price = float(price_raw)
            except (TypeError, ValueError):
                continue

            row = {
                "hotel": hotel_name,
                "checkin": checkin,
                "checkout": checkout,
                "price": price,
                "provider": provider,
            }
            all_results.append(row)

            if conn is not None and watch_id is not None:
                _record_price(
                    conn,
                    watch_id,
                    checked_at,
                    "hotel",
                    price,
                    checkin,
                    checkout,
                    f"{hotel_name} / {provider}".strip(" /"),
                    stay,
                    "firecrawl_extract",
                )

    if conn is not None and watch_id is not None:
        try:
            conn.commit()
        except Exception as exc:
            print(f"[travel-monitor] hotels DB commit error: {exc}", file=sys.stderr)

    if not all_results:
        reason = "scrape_failed" if any_failure else "no_stays_extracted"
        return {"lane": "hotel", "status": "unavailable", "reason": reason}

    return {"lane": "hotel", "status": "ok", "results": all_results}


# ---------------------------------------------------------------------------
# Lane — packages (Phase 3)
# ---------------------------------------------------------------------------

_PACKAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "packages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "checkin": {"type": "string", "description": "YYYY-MM-DD departure date"},
                    "checkout": {"type": "string", "description": "YYYY-MM-DD return date"},
                    "price_total": {
                        "type": "number",
                        "description": "Total package price in CAD for all travellers",
                    },
                    "description": {
                        "type": "string",
                        "description": "Brief description, e.g. hotel name or package title",
                    },
                },
                "required": ["checkin", "checkout", "price_total"],
            },
        },
    },
    "required": ["packages"],
}

_PACKAGE_PROMPT = (
    "Extract all vacation package options shown on this page for the destination and dates. "
    "For each package listing extract: the departure/check-in date (YYYY-MM-DD), "
    "return/check-out date (YYYY-MM-DD), the total package price in CAD for all travellers, "
    "and a brief description (hotel name or package title). "
    "Include only packages that have a price shown. "
    "All prices must be in CAD — if shown in USD or another currency, convert or skip."
)


def _operator_url(
    operator: str,
    destination: str,
    origin: str,
    window_start: str,
    window_end: str,
    stay_nights: int,
    adults: int,
    children: int,
) -> str | None:
    """Return a flex-date search URL for the given operator, or None if unknown."""
    dest_lc = destination.lower()
    origin_lc = origin.lower()

    if operator == "costco":
        # Costco Travel CA vacation packages flex search
        params = urllib.parse.urlencode(
            {
                "destinationCode": destination,
                "originCode": origin,
                "departureDate": window_start,
                "returnDate": window_end,
                "nights": stay_nights,
                "adults": adults,
                "children": children,
                "flexible": "true",
            }
        )
        return f"https://www.costcotravel.ca/vacation-packages/search?{params}"

    if operator == "aircanada":
        params = urllib.parse.urlencode(
            {
                "origin": origin_lc,
                "destination": dest_lc,
                "departureDate": window_start,
                "returnDate": window_end,
                "duration": stay_nights,
                "adults": adults,
                "children": children,
                "flexible": "true",
                "currency": "CAD",
            }
        )
        return f"https://www.aircanadavacations.com/search?{params}"

    if operator == "westjet":
        params = urllib.parse.urlencode(
            {
                "from": origin_lc,
                "to": dest_lc,
                "departureDate": window_start,
                "returnDate": window_end,
                "duration": stay_nights,
                "adults": adults,
                "children": children,
                "flexible": "true",
                "currency": "CAD",
            }
        )
        return f"https://www.westjetvacations.com/search?{params}"

    if operator == "expedia":
        params = urllib.parse.urlencode(
            {
                "destination": dest_lc,
                "origin": origin_lc,
                "startDate": window_start,
                "endDate": window_end,
                "duration": stay_nights,
                "adults": adults,
                "children": children,
                "sort": "PRICE_LOW_TO_HIGH",
                "currency": "CAD",
            }
        )
        return f"https://www.expedia.ca/Vacation-Packages?{params}"

    return None


def _lane_packages(watch: dict, operator: str, conn: sqlite3.Connection | None = None) -> dict:
    """
    Packages lane: Firecrawl JSON-extract of each operator's flex-date vacation search.

    One scrape per operator per run (operator search page returns results across the
    full window server-side — cost rule maintained). One retry on parse_error.

    Returns {lane: 'package', operator: str, status: 'ok', results: [...]}
    or {status: 'unavailable', reason: '...'} on failure.
    """
    destination = watch["destination"]
    origin = watch["origin_iata"]
    window_start = watch["window_start"]
    window_end = watch["window_end"]
    stay_nights = int(watch["stay_nights"])
    adults = int(watch.get("adults", 2))
    children = int(watch.get("children", 0))
    watch_id = watch.get("id")
    checked_at = _now_utc().isoformat()

    url = _operator_url(
        operator, destination, origin, window_start, window_end, stay_nights, adults, children
    )
    if url is None:
        return {
            "lane": "package",
            "operator": operator,
            "status": "unavailable",
            "reason": f"unknown_operator: {operator}",
        }

    extracted, err = _firecrawl_extract(url, _PACKAGE_SCHEMA, _PACKAGE_PROMPT)

    # One retry on parse error
    if err == "parse_error":
        print(
            f"[travel-monitor] packages parse_error for {operator!r}, retrying",
            file=sys.stderr,
        )
        extracted, err = _firecrawl_extract(url, _PACKAGE_SCHEMA, _PACKAGE_PROMPT)

    if err or extracted is None:
        print(
            f"[travel-monitor] packages lane failed for {operator!r}: {err}",
            file=sys.stderr,
        )
        return {
            "lane": "package",
            "operator": operator,
            "status": "unavailable",
            "reason": err or "scrape_failed",
        }

    packages = extracted.get("packages", [])
    if not packages:
        print(
            f"[travel-monitor] packages: no results extracted for {operator!r}",
            file=sys.stderr,
        )
        return {
            "lane": "package",
            "operator": operator,
            "status": "unavailable",
            "reason": "no_packages_extracted",
        }

    results: list[dict] = []
    for pkg in packages:
        checkin = pkg.get("checkin", "")
        checkout = pkg.get("checkout", "")
        price_raw = pkg.get("price_total")
        description = pkg.get("description", "")

        if not checkin or not checkout or price_raw is None:
            continue
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue

        row = {
            "operator": operator,
            "checkin": checkin,
            "checkout": checkout,
            "price": price,
            "description": description,
        }
        results.append(row)

        if conn is not None and watch_id is not None:
            _record_price(
                conn,
                watch_id,
                checked_at,
                "package",
                price,
                checkin,
                checkout,
                operator,
                pkg,
                "firecrawl_extract",
            )

    if conn is not None and watch_id is not None:
        try:
            conn.commit()
        except Exception as exc:
            print(f"[travel-monitor] packages DB commit error: {exc}", file=sys.stderr)

    if not results:
        return {
            "lane": "package",
            "operator": operator,
            "status": "unavailable",
            "reason": "no_valid_packages",
        }

    return {"lane": "package", "operator": operator, "status": "ok", "results": results}


# ---------------------------------------------------------------------------
# Reconciliation (Phase 4)
# ---------------------------------------------------------------------------


def _reconcile(lane_results: list[dict], stay_nights: int) -> dict | None:
    """
    Join flight + hotel results on matching checkin date → best DIY total.
    Compare against best package total across all operators.
    Return the winning option dict, or None if insufficient data to reconcile.

    Returned dict keys:
      winner        — 'diy' or 'package'
      headline      — winning total price (CAD)
      checkin       — winning checkin date
      checkout      — winning checkout date
      diy_total     — best DIY total (or None)
      diy_flight    — flight component of best DIY
      diy_hotel     — hotel component of best DIY
      diy_carrier   — carrier for best DIY flight
      diy_provider  — hotel provider for best DIY
      pkg_total     — best package total (or None)
      pkg_operator  — operator for best package
      pkg_checkin   — checkin for best package
      pkg_checkout  — checkout for best package
      pkg_desc      — package description
      delta         — abs(diy_total - pkg_total) if both available, else None
    """
    # Separate results by lane
    flight_result = next(
        (r for r in lane_results if r.get("lane") == "flight" and r.get("status") == "ok"), None
    )
    hotel_result = next(
        (r for r in lane_results if r.get("lane") == "hotel" and r.get("status") == "ok"), None
    )
    pkg_results = [
        r for r in lane_results if r.get("lane") == "package" and r.get("status") == "ok"
    ]

    # Best DIY: join flights + hotels on matching checkin date
    best_diy: dict | None = None
    if flight_result and hotel_result:
        # Index hotel results by checkin date → cheapest price for that date
        hotel_by_date: dict[str, dict] = {}
        for stay in hotel_result.get("results", []):
            ci = stay.get("checkin", "")
            price = stay.get("price")
            if ci and price is not None:
                if ci not in hotel_by_date or price < hotel_by_date[ci]["price"]:
                    hotel_by_date[ci] = stay

        for flight in flight_result.get("results", []):
            ci = flight.get("checkin", "")
            if ci not in hotel_by_date:
                continue
            hotel = hotel_by_date[ci]
            diy_total = flight["price"] + hotel["price"]
            if best_diy is None or diy_total < best_diy["diy_total"]:
                best_diy = {
                    "diy_total": diy_total,
                    "checkin": ci,
                    "checkout": flight.get("checkout", ""),
                    "diy_flight": flight["price"],
                    "diy_hotel": hotel["price"],
                    "diy_carrier": flight.get("carrier", ""),
                    "diy_provider": hotel.get("provider", ""),
                }
    elif flight_result and not hotel_result:
        # No hotel data — flight-only DIY (partial)
        for flight in flight_result.get("results", []):
            if best_diy is None or flight["price"] < best_diy["diy_total"]:
                best_diy = {
                    "diy_total": flight["price"],
                    "checkin": flight.get("checkin", ""),
                    "checkout": flight.get("checkout", ""),
                    "diy_flight": flight["price"],
                    "diy_hotel": None,
                    "diy_carrier": flight.get("carrier", ""),
                    "diy_provider": None,
                }

    # Best package: cheapest across all operators
    best_pkg: dict | None = None
    for pkg_lane in pkg_results:
        for pkg in pkg_lane.get("results", []):
            price = pkg.get("price")
            if price is None:
                continue
            if best_pkg is None or price < best_pkg["pkg_total"]:
                best_pkg = {
                    "pkg_total": price,
                    "pkg_operator": pkg_lane.get("operator", ""),
                    "pkg_checkin": pkg.get("checkin", ""),
                    "pkg_checkout": pkg.get("checkout", ""),
                    "pkg_desc": pkg.get("description", ""),
                }

    if best_diy is None and best_pkg is None:
        return None

    diy_total = best_diy["diy_total"] if best_diy else None
    pkg_total = best_pkg["pkg_total"] if best_pkg else None

    if diy_total is not None and pkg_total is not None:
        delta = abs(diy_total - pkg_total)
        if diy_total <= pkg_total:
            winner = "diy"
            headline = diy_total
            checkin = best_diy["checkin"]
            checkout = best_diy["checkout"]
        else:
            winner = "package"
            headline = pkg_total
            checkin = best_pkg["pkg_checkin"]
            checkout = best_pkg["pkg_checkout"]
    elif diy_total is not None:
        winner = "diy"
        headline = diy_total
        checkin = best_diy["checkin"]
        checkout = best_diy["checkout"]
        delta = None
    else:
        winner = "package"
        headline = pkg_total
        checkin = best_pkg["pkg_checkin"]
        checkout = best_pkg["pkg_checkout"]
        delta = None

    return {
        "winner": winner,
        "headline": headline,
        "checkin": checkin,
        "checkout": checkout,
        "diy_total": diy_total,
        "diy_flight": best_diy["diy_flight"] if best_diy else None,
        "diy_hotel": best_diy["diy_hotel"] if best_diy else None,
        "diy_carrier": best_diy["diy_carrier"] if best_diy else None,
        "diy_provider": best_diy["diy_provider"] if best_diy else None,
        "pkg_total": pkg_total,
        "pkg_operator": best_pkg["pkg_operator"] if best_pkg else None,
        "pkg_checkin": best_pkg["pkg_checkin"] if best_pkg else None,
        "pkg_checkout": best_pkg["pkg_checkout"] if best_pkg else None,
        "pkg_desc": best_pkg["pkg_desc"] if best_pkg else None,
        "delta": delta,
    }


def _format_deal_alert(
    watch_name: str, stay_nights: int, adults: int, children: int, rec: dict, reason: str
) -> str:
    """Format the Discord deal alert message."""
    pax = f"{adults}+{children} pax" if children else f"{adults} pax"
    header = f":airplane: **{watch_name}** · {stay_nights}n · {pax}"

    winner = rec["winner"]
    headline = rec["headline"]
    checkin = rec["checkin"]
    checkout = rec["checkout"]

    lines = [
        header,
        f"**Best this scan: ${headline:,.0f} CAD** ({checkin} – {checkout}) — {reason}",
    ]

    if winner == "package" and rec.get("pkg_operator"):
        op = rec["pkg_operator"].title()
        lines.append(f"  Package: **{op}** ${rec['pkg_total']:,.0f}")
        if rec.get("pkg_desc"):
            lines.append(f"  ({rec['pkg_desc']})")
    elif winner == "diy":
        parts = []
        if rec.get("diy_flight") is not None:
            carrier = f" via {rec['diy_carrier']}" if rec.get("diy_carrier") else ""
            parts.append(f"flight ${rec['diy_flight']:,.0f}{carrier}")
        if rec.get("diy_hotel") is not None:
            prov = f" ({rec['diy_provider']})" if rec.get("diy_provider") else ""
            parts.append(f"hotel ${rec['diy_hotel']:,.0f}{prov}")
        if parts:
            lines.append("  DIY: " + " + ".join(parts))

    if rec.get("diy_total") is not None and rec.get("pkg_total") is not None:
        delta = rec["delta"]
        cheaper = "Package" if rec["pkg_total"] < rec["diy_total"] else "DIY"
        lines.append(
            f"  {cheaper} wins by ${delta:,.0f} vs {'DIY' if cheaper == 'Package' else 'package'} ${max(rec['diy_total'], rec['pkg_total']):,.0f}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deal detection
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _detect_deal(
    conn: sqlite3.Connection,
    watch_id: int,
    current_price: float,
    target_price: float | None,
    inserted_at: str,
) -> str | None:
    if target_price is not None and current_price <= target_price:
        return f"at or below target ${target_price:.2f} CAD"

    prior_rows = conn.execute(
        """SELECT best_price FROM travel_price_history
           WHERE watch_id = ? AND lane = 'reconciled' AND checked_at < ? AND best_price IS NOT NULL
           ORDER BY checked_at DESC
           LIMIT ?""",
        (watch_id, inserted_at, N_MEDIAN),
    ).fetchall()
    prior_prices = [row[0] for row in prior_rows]

    if len(prior_prices) < 2:
        return None

    median_price = _median(prior_prices[:N_MEDIAN])
    if median_price > 0:
        drop_pct = (median_price - current_price) / median_price * 100
        if drop_pct >= DEAL_DROP_PCT:
            return f"{drop_pct:.0f}% below {len(prior_prices)}-reading median ${median_price:.2f}"

    cutoff = (datetime.now(UTC) - timedelta(days=N_DAY_LOW)).isoformat()
    period_row = conn.execute(
        """SELECT MIN(best_price) FROM travel_price_history
           WHERE watch_id = ? AND lane = 'reconciled'
             AND checked_at >= ? AND checked_at < ?
             AND best_price IS NOT NULL""",
        (watch_id, cutoff, inserted_at),
    ).fetchone()
    if period_row and period_row[0] is not None:
        prior_min = period_row[0]
        if current_price < prior_min:
            return f"{N_DAY_LOW}-day low (previous min ${prior_min:.2f})"

    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args: list[str]) -> None:
    params = _parse_args(args)

    name = str(params.get("name", "")).strip()
    dest = str(params.get("dest", "")).strip().upper()
    origin = str(params.get("origin", "")).strip().upper()
    window_start = str(params.get("window_start", "")).strip()
    window_end = str(params.get("window_end", "")).strip()
    nights_raw = params.get("nights", "")

    errors = []
    if not name:
        errors.append("--name is required")
    if not dest:
        errors.append("--dest is required (IATA city/airport code, e.g. PUJ)")
    if not origin:
        errors.append("--origin is required (IATA code, e.g. YUL)")
    if not window_start:
        errors.append("--window-start is required (YYYY-MM-DD)")
    if not window_end:
        errors.append("--window-end is required (YYYY-MM-DD)")
    if not nights_raw:
        errors.append("--nights is required")

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        stay_nights = int(str(nights_raw))
        if stay_nights < 1:
            raise ValueError
    except ValueError:
        print("Error: --nights must be a positive integer", file=sys.stderr)
        sys.exit(1)

    try:
        ws = datetime.fromisoformat(window_start).date()
        we = datetime.fromisoformat(window_end).date()
        if we <= ws:
            print("Error: --window-end must be after --window-start", file=sys.stderr)
            sys.exit(1)
        if (we - ws).days < stay_nights:
            print(
                f"Error: window is only {(we - ws).days} days but --nights is {stay_nights}",
                file=sys.stderr,
            )
            sys.exit(1)
    except ValueError:
        print("Error: dates must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    adults = 2
    if "adults" in params:
        try:
            adults = int(str(params["adults"]))
        except ValueError:
            print("Error: --adults must be an integer", file=sys.stderr)
            sys.exit(1)

    children = 0
    if "children" in params:
        try:
            children = int(str(params["children"]))
        except ValueError:
            print("Error: --children must be an integer", file=sys.stderr)
            sys.exit(1)

    hotels_raw = params.get("hotel", [])
    hotel_targets: list[str] = hotels_raw if isinstance(hotels_raw, list) else [hotels_raw]

    operators_raw = params.get("operator", [])
    operators: list[str] = operators_raw if isinstance(operators_raw, list) else [operators_raw]
    invalid_ops = [o for o in operators if o not in VALID_OPERATORS]
    if invalid_ops:
        print(
            f"Error: unknown operator(s): {invalid_ops}. Valid: {sorted(VALID_OPERATORS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    target_price: float | None = None
    if "target_price" in params:
        try:
            target_price = float(str(params["target_price"]))
        except ValueError:
            print("Error: --target-price must be a number", file=sys.stderr)
            sys.exit(1)

    cadence = str(params.get("cadence", "weekly"))
    if cadence not in VALID_CADENCES:
        print(f"Error: --cadence must be one of {sorted(VALID_CADENCES)}", file=sys.stderr)
        sys.exit(1)

    # Add-time probe: run each enabled lane once, surface failures
    watch_stub = {
        "destination": dest,
        "origin_iata": origin,
        "window_start": window_start,
        "window_end": window_end,
        "stay_nights": stay_nights,
        "adults": adults,
        "children": children,
        "hotel_targets": hotel_targets,
        "package_operators": operators,
    }

    lane_results: list[dict] = [_lane_flights(watch_stub)]  # flights always probed
    if hotel_targets:
        lane_results.append(_lane_hotels(watch_stub))
    for op in operators:
        lane_results.append(_lane_packages(watch_stub, op))

    # Distinguish real runtime failures from not-yet-implemented stubs.
    # Stubs (reason='not_implemented') are expected in Phases 0–2; they never block
    # watch creation. A real failure (reason != 'not_implemented') means the lane
    # actually tried and couldn't reach the provider.
    runtime_failures = [
        r
        for r in lane_results
        if r.get("status") == "unavailable" and r.get("reason") != "not_implemented"
    ]
    stubs = [r for r in lane_results if r.get("reason") == "not_implemented"]
    available = [r for r in lane_results if r.get("status") != "unavailable"]

    if not available and runtime_failures:
        note = (
            f"All lanes failed for **{name}** ({dest}, {window_start}–{window_end}) — "
            "want me to set a weekly reminder to check manually instead?"
        )
        print(json.dumps({"status": "needs_reminder", "name": name, "message": note}, indent=2))
        return

    now_iso = _now_utc().isoformat()
    conn = _init_db()
    try:
        cur = conn.execute(
            """INSERT INTO travel_watches
               (name, destination, origin_iata, window_start, window_end, stay_nights,
                adults, children, hotel_targets, package_operators, target_price,
                currency, cadence, status, fail_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CAD', ?, 'active', 0, ?)""",
            (
                name,
                dest,
                origin,
                window_start,
                window_end,
                stay_nights,
                adults,
                children,
                json.dumps(hotel_targets),
                json.dumps(operators),
                target_price,
                cadence,
                now_iso,
            ),
        )
        watch_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    msg_parts = [
        f"Watching **{name}** — {origin}→{dest}, {adults}+{children} pax, "
        f"{stay_nights} nights in {window_start}–{window_end}. Cadence: {cadence}."
    ]
    if target_price is not None:
        msg_parts.append(f"Alert target: ${target_price:.0f} CAD.")
    if stubs:
        stub_names = [r.get("operator", r.get("lane", "?")) for r in stubs]
        msg_parts.append(
            f"Note: {len(stubs)} lane(s) not yet implemented "
            f"({', '.join(str(n) for n in stub_names)}) — will activate in later phases."
        )
    if runtime_failures:
        fail_names = [r.get("operator", r.get("lane", "?")) for r in runtime_failures]
        msg_parts.append(
            f"Warning: {len(runtime_failures)} lane(s) unreachable at add-time "
            f"({', '.join(str(n) for n in fail_names)})."
        )

    print(
        json.dumps(
            {
                "id": watch_id,
                "status": "active",
                "name": name,
                "destination": dest,
                "origin": origin,
                "window": f"{window_start}–{window_end}",
                "nights": stay_nights,
                "cadence": cadence,
                "lanes_probed": len(lane_results),
                "lanes_unavailable": len(runtime_failures) + len(stubs),
                "message": " ".join(msg_parts),
            },
            indent=2,
        )
    )


def cmd_list(args: list[str]) -> None:
    conn = _init_db()
    try:
        rows = conn.execute(
            """SELECT id, name, destination, origin_iata, window_start, window_end,
                      stay_nights, adults, children, cadence, status, target_price,
                      created_at
               FROM travel_watches
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END,
                        window_start ASC"""
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No travel watches.")
        return

    headers = ["ID", "Name", "Route", "Window", "Nights", "Pax", "Cadence", "Status", "Target"]
    col_widths = [len(h) for h in headers]

    table_rows: list[list[str]] = []
    for row in rows:
        (
            row_id,
            name,
            dest,
            origin,
            ws,
            we,
            nights,
            adults,
            children,
            cadence,
            status,
            target,
            _,
        ) = row
        name_trunc = name[:28] if len(name) > 28 else name
        route = f"{origin}→{dest}"
        window = f"{ws} – {we}"
        pax = f"{adults}+{children}"
        target_str = f"${target:.0f}" if target is not None else "—"
        r = [
            str(row_id),
            name_trunc,
            route,
            window,
            str(nights),
            pax,
            cadence,
            status,
            target_str,
        ]
        table_rows.append(r)
        for i, cell in enumerate(r):
            col_widths[i] = max(col_widths[i], len(cell))

    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"

    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(cells)) + " |"

    lines = [sep, fmt_row(headers), sep] + [fmt_row(r) for r in table_rows] + [sep]
    print("\n".join(lines))


def cmd_resolve(args: list[str]) -> None:
    params = _parse_args(args)

    if "id" not in params:
        print("Error: --id is required", file=sys.stderr)
        sys.exit(1)
    action = str(params.get("action", ""))
    if action not in VALID_RESOLVE_ACTIONS:
        print(f"Error: --action must be one of {sorted(VALID_RESOLVE_ACTIONS)}", file=sys.stderr)
        sys.exit(1)

    try:
        watch_id = int(str(params["id"]))
    except ValueError:
        print("Error: --id must be an integer", file=sys.stderr)
        sys.exit(1)

    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT name FROM travel_watches WHERE id = ? AND status != 'resolved'",
            (watch_id,),
        ).fetchone()
        if not row:
            print(f"No active watch with id={watch_id}", file=sys.stderr)
            sys.exit(1)

        conn.execute(
            "UPDATE travel_watches SET status = 'resolved' WHERE id = ?",
            (watch_id,),
        )
        conn.commit()
        print(
            json.dumps(
                {"id": watch_id, "name": row[0], "action": action, "status": "resolved"},
                indent=2,
            )
        )
    finally:
        conn.close()


def cmd_check(args: list[str]) -> None:
    """
    Scheduled run: scan active watches, run lanes, reconcile, detect deals.
    Silent unless a deal or health alert fires.

    Phase 4+5: reconcile, persist, deal detect, Discord alert.
    Health hardening: fail_count tracking, degraded status, cadence escalation.
    """
    conn = _init_db()
    try:
        watches = conn.execute(
            """SELECT id, name, destination, origin_iata, window_start, window_end,
                      stay_nights, adults, children, hotel_targets, package_operators,
                      target_price, cadence, fail_count
               FROM travel_watches WHERE status IN ('active', 'degraded')"""
        ).fetchall()

        if not watches:
            return

        today = _today_local()

        for row in watches:
            (
                watch_id,
                name,
                dest,
                origin,
                window_start,
                window_end,
                stay_nights,
                adults,
                children,
                hotel_targets_json,
                operators_json,
                target_price,
                cadence,
                fail_count,
            ) = row

            # Skip if watch window has passed
            if today > window_end:
                print(
                    f"[travel-monitor] watch={watch_id} window expired, skipping",
                    file=sys.stderr,
                )
                continue

            hotel_targets: list[str] = json.loads(hotel_targets_json or "[]")
            operators: list[str] = json.loads(operators_json or "[]")

            watch_stub = {
                "id": watch_id,
                "name": name,
                "destination": dest,
                "origin_iata": origin,
                "window_start": window_start,
                "window_end": window_end,
                "stay_nights": stay_nights,
                "adults": adults,
                "children": children,
                "hotel_targets": hotel_targets,
                "package_operators": operators,
            }

            lane_results: list[dict] = [_lane_flights(watch_stub, conn)]
            if hotel_targets:
                lane_results.append(_lane_hotels(watch_stub, conn))
            for op in operators:
                lane_results.append(_lane_packages(watch_stub, op, conn))

            # Phase 5: health accounting — count real failures (not stubs)
            real_failures = [
                r
                for r in lane_results
                if r.get("status") == "unavailable" and r.get("reason") != "not_implemented"
            ]
            ok_lanes = [r for r in lane_results if r.get("status") == "ok"]

            for result in lane_results:
                lane = result.get("lane", "unknown")
                status = result.get("status", "unknown")
                reason = result.get("reason", "")
                if status == "unavailable" and reason != "not_implemented":
                    op_tag = f"/{result['operator']}" if result.get("operator") else ""
                    alert = (
                        f":warning: **travel-monitor health** | watch={watch_id} `{name}` | "
                        f"lane={lane}{op_tag} unavailable — {reason}"
                    )
                    _discord_post(alert)
                print(
                    f"[travel-monitor] watch={watch_id} lane={lane} status={status}"
                    + (f" reason={reason}" if reason else ""),
                    file=sys.stderr,
                )

            # Update fail_count and status
            if ok_lanes:
                # At least one lane succeeded — reset fail counter if needed
                if fail_count != 0:
                    conn.execute(
                        "UPDATE travel_watches SET fail_count = 0, status = 'active' WHERE id = ?",
                        (watch_id,),
                    )
            elif real_failures:
                # All lanes failed this run
                new_fail_count = fail_count + 1
                new_status = "degraded" if new_fail_count >= DEGRADE_AFTER else "active"
                conn.execute(
                    "UPDATE travel_watches SET fail_count = ?, status = ? WHERE id = ?",
                    (new_fail_count, new_status, watch_id),
                )
                if new_fail_count == DEGRADE_AFTER:
                    _discord_post(
                        f":x: **travel-monitor degraded** | watch={watch_id} `{name}` | "
                        f"all lanes failed {new_fail_count} consecutive runs — "
                        "manual check recommended"
                    )
                    print(
                        f"[travel-monitor] watch={watch_id} DEGRADED after "
                        f"{new_fail_count} consecutive failures",
                        file=sys.stderr,
                    )
            conn.commit()

            # Phase 5: cadence escalation — suggest daily when within ESCALATE_DAYS.
            # We post a one-time Discord suggestion; the user approves via `schedules propose`.
            # The schedules dispatcher fires based on the schedules table, not travel_watches.cadence,
            # so only a human-approved schedule change actually increases firing frequency.
            try:
                days_until_window = (
                    date.fromisoformat(window_start) - date.fromisoformat(today)
                ).days
            except ValueError:
                days_until_window = None

            if (
                days_until_window is not None
                and 0 <= days_until_window <= ESCALATE_DAYS
                and cadence == "weekly"
            ):
                conn.execute(
                    "UPDATE travel_watches SET cadence = 'daily' WHERE id = ?",
                    (watch_id,),
                )
                conn.commit()
                _discord_post(
                    f":alarm_clock: **travel-monitor** | watch={watch_id} `{name}` | "
                    f"window starts in {days_until_window}d — consider switching to daily scans: "
                    f"`schedules propose --skill travel-monitor --schedule daily@09:00`"
                )
                print(
                    f"[travel-monitor] watch={watch_id} escalation suggested "
                    f"({days_until_window}d until window_start)",
                    file=sys.stderr,
                )

            # Reconcile, persist headline, detect deal
            rec = _reconcile(lane_results, stay_nights)
            if rec is not None:
                headline = rec["headline"]
                checked_at = _now_utc().isoformat()
                _record_price(
                    conn,
                    watch_id,
                    checked_at,
                    "reconciled",
                    headline,
                    rec["checkin"],
                    rec["checkout"],
                    rec.get("pkg_operator") or rec.get("diy_carrier") or "",
                    rec,
                    "reconcile",
                )
                conn.commit()

                deal_reason = _detect_deal(conn, watch_id, headline, target_price, checked_at)
                if deal_reason:
                    alert = _format_deal_alert(
                        name, stay_nights, adults, children, rec, deal_reason
                    )
                    _discord_post(alert)
                    print(
                        f"[travel-monitor] watch={watch_id} DEAL: {deal_reason} "
                        f"headline=${headline:.0f}",
                        file=sys.stderr,
                    )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <command> [args...]\n"
            "Commands:\n"
            "  add --name N --dest IATA --origin IATA\n"
            "      --window-start YYYY-MM-DD --window-end YYYY-MM-DD --nights N\n"
            "      --adults N [--children N] [--hotel NAME]... [--operator NAME]...\n"
            "      [--target-price N] [--cadence weekly|daily]\n"
            "  list\n"
            "  resolve --id N --action booked|passed\n"
            "  check",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    rest = sys.argv[2:]

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "resolve": cmd_resolve,
        "check": cmd_check,
    }

    if cmd not in dispatch:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)

    dispatch[cmd](rest)


if __name__ == "__main__":
    main()
