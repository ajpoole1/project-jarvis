"""
Price monitor skill — watches product URLs for price drops.

Fetch layer: Firecrawl HTTP scrape API (bypasses anti-bot / JS rendering), falling back
to direct urllib GET when FIRECRAWL_API_KEY is absent.

Parse cascade (in order): Shopify JSON endpoint → JSON-LD → Open Graph / meta → regex.
If the HTML cascade fails, Firecrawl LLM extraction is tried as a last resort (extra credits).
Unreadable URLs degrade to follow_ups reminders rather than silent dead watches.
Stdlib only; no virtualenv needed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"
_LOCAL_TZ = ZoneInfo("America/Toronto")
_SKILL_ROOT = Path(__file__).parents[1]
_DISCORD_SCRIPT = Path(__file__).parents[2] / "scripts" / "discord_post.py"

# Manual .env parse — no dotenv dependency
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

FAIL_THRESHOLD = int(os.environ.get("PRICE_MONITOR_FAIL_THRESHOLD", "3"))
DEAL_DROP_PCT = float(os.environ.get("PRICE_MONITOR_DEAL_DROP_PCT", "15"))
N_MEDIAN = int(os.environ.get("PRICE_MONITOR_N_MEDIAN", "10"))
N_DAY_LOW = int(os.environ.get("PRICE_MONITOR_N_DAY_LOW", "30"))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_watches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            url           TEXT NOT NULL,
            vendor        TEXT,
            target_price  REAL,
            currency      TEXT,
            last_price    REAL,
            last_checked  TEXT,
            parse_method  TEXT,
            status        TEXT NOT NULL DEFAULT 'active',
            fail_count    INTEGER NOT NULL DEFAULT 0,
            active_from   TEXT,
            active_until  TEXT,
            created_at    TEXT NOT NULL,
            deal_drop_pct REAL
        )
    """)
    try:
        conn.execute("ALTER TABLE price_watches ADD COLUMN deal_drop_pct REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id    INTEGER NOT NULL,
            price       REAL NOT NULL,
            checked_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# HTTP fetch (direct urllib — used for Shopify .json endpoint and key-absent fallback)
# ---------------------------------------------------------------------------


def _fetch(url: str) -> tuple[int, str]:
    """Return (status_code, body_text). Never raises; returns (0, '') on any error."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "en-CA,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            charset = "utf-8"
            ct = resp.headers.get("Content-Type", "")
            m = re.search(r"charset=([\w-]+)", ct)
            if m:
                charset = m.group(1)
            try:
                return resp.status, raw.decode(charset, errors="replace")
            except Exception:
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


# ---------------------------------------------------------------------------
# Firecrawl fetch layer
# ---------------------------------------------------------------------------


def _firecrawl_scrape(url: str) -> tuple[str | None, str | None]:
    """
    Fetch rendered HTML via Firecrawl scrape API (1 credit per page).
    Bypasses anti-bot JS rendering that blocks direct urllib GET.

    Returns (html, None) on success.
    Returns (None, error_type) on failure:
        'no_key'    — FIRECRAWL_API_KEY not set
        'quota'     — 402 or credit-exhaustion response
        'rate_limit'— 429
        'error'     — any other failure
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return None, "no_key"

    payload = json.dumps({"url": url, "formats": ["rawHtml"]}).encode()
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not data.get("success"):
                return None, "error"
            page = data.get("data") or {}
            html = page.get("rawHtml") or page.get("html")
            if not html:
                return None, "error"
            credits = page.get("metadata", {}).get("creditsUsed")
            if credits is not None:
                print(f"[price-monitor] firecrawl credits_used={credits}", file=sys.stderr)
            return html, None
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
            body_data = json.loads(body_text)
            if "credit" in str(body_data).lower() or "quota" in str(body_data).lower():
                return None, "quota"
        except Exception:
            pass
        if e.code == 402:
            return None, "quota"
        if e.code == 429:
            return None, "rate_limit"
        return None, "error"
    except Exception:
        return None, "error"


def _firecrawl_extract(url: str) -> tuple[float | None, str | None]:
    """
    Firecrawl LLM-based JSON extraction — extra credits, last resort only.
    Used when the cheap rawHtml parse cascade comes up empty.

    Returns (price, currency) or (None, None) on any failure.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return None, None

    schema = {
        "type": "object",
        "properties": {
            "price": {"type": "number"},
            "currency": {"type": "string"},
        },
        "required": ["price"],
    }
    payload = json.dumps(
        {
            "url": url,
            "formats": ["extract"],
            "extract": {
                "schema": schema,
                "prompt": "Extract the current product price as a number and the currency code (e.g. CAD, USD).",
            },
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not data.get("success"):
                return None, None
            extracted = (data.get("data") or {}).get("extract")
            if not isinstance(extracted, dict):
                return None, None
            price_raw = extracted.get("price")
            currency = extracted.get("currency")
            if price_raw is None:
                return None, None
            return float(price_raw), currency or None
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Parse cascade
# ---------------------------------------------------------------------------


def _try_shopify(url: str) -> tuple[float | None, str, str | None]:
    """Shopify product JSON endpoint: <product-url>.json"""
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    m = re.match(r"^(.*?/products/[^/?#]+)", path)
    if not m:
        return None, "shopify", None

    product_path = m.group(1)
    if product_path.endswith(".json"):
        json_url = url
    else:
        json_url = urlunparse(parsed._replace(path=product_path + ".json", query="", fragment=""))

    status, body = _fetch(json_url)
    if status != 200 or not body:
        return None, "shopify", None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, "shopify", None

    product = data.get("product", {})
    variants = product.get("variants", [])
    if not variants:
        return None, "shopify", None

    price_str = variants[0].get("price")
    if price_str is None:
        return None, "shopify", None

    try:
        return float(price_str), "shopify", product.get("currency") or None
    except (ValueError, TypeError):
        return None, "shopify", None


def _try_jsonld(html: str) -> tuple[float | None, str, str | None]:
    """JSON-LD structured data (schema.org Product / Offer)."""
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    for raw in scripts:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue

        candidates: list[dict] = []
        if isinstance(data, dict):
            candidates.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)
        elif isinstance(data, list):
            candidates.extend(data)

        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(str(t) for t in item_type)

            if "Product" in item_type:
                offers = item.get("offers") or item.get("Offers")
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    price_raw = offers.get("price") or offers.get("lowPrice")
                    currency = offers.get("priceCurrency")
                    if price_raw is not None:
                        try:
                            return float(str(price_raw).replace(",", "")), "jsonld", currency
                        except (ValueError, TypeError):
                            pass
                # price directly on the Product node
                price_raw = item.get("price")
                if price_raw is not None:
                    try:
                        return float(str(price_raw).replace(",", "")), "jsonld", None
                    except (ValueError, TypeError):
                        pass

            elif "Offer" in item_type:
                price_raw = item.get("price") or item.get("lowPrice")
                currency = item.get("priceCurrency")
                if price_raw is not None:
                    try:
                        return float(str(price_raw).replace(",", "")), "jsonld", currency
                    except (ValueError, TypeError):
                        pass

    return None, "jsonld", None


def _try_og(html: str) -> tuple[float | None, str, str | None]:
    """Open Graph / itemprop price meta tags."""
    price_pats = [
        r'<meta[^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)["\']',
        r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+itemprop=["\']price["\']',
        r'<span[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    currency_pats = [
        r'<meta[^>]+(?:property|name|itemprop)=["\'](?:product:price:currency|og:price:currency|priceCurrency)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\'](?:product:price:currency|og:price:currency|priceCurrency)["\']',
    ]

    for pat in price_pats:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            try:
                price = float(m.group(1).replace(",", "").strip())
            except (ValueError, TypeError):
                continue
            currency: str | None = None
            for cpat in currency_pats:
                cm = re.search(cpat, html, re.IGNORECASE)
                if cm:
                    currency = cm.group(1).strip()
                    break
            return price, "og", currency

    return None, "og", None


def _try_regex(html: str) -> tuple[float | None, str, str | None]:
    """Last-resort: currency pattern anchored near a 'price' keyword."""
    pats = [
        r'["\']price["\'][^{}\[\]]{0,200}?\$\s*([\d,]+\.?\d*)',
        r"price[^<]{0,100}\$\s*([\d,]+\.?\d*)",
        r"\$\s*([\d]{1,5}\.\d{2})\b",
    ]
    for pat in pats:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
                if 0.01 <= price <= 100_000:
                    return price, "regex", None
            except (ValueError, TypeError):
                continue
    return None, "regex", None


def _probe(url: str) -> tuple[float | None, str, str | None]:
    """
    Run the full parse cascade.
    Returns (price, method, currency) or (None, 'blocked', None).
    method: shopify | jsonld | og | regex | firecrawl_extract | blocked

    Fetch order:
      1. Shopify .json endpoint (direct urllib — JSON API, no bot-block)
      2. Firecrawl rawHtml scrape (bypasses anti-bot / JS rendering)
         Fallback to direct urllib GET when FIRECRAWL_API_KEY is absent
      3. HTML parse cascade: JSON-LD → Open Graph → regex
      4. Firecrawl LLM extraction (extra credits, only when cascade fails)
    """
    # Step 1: Shopify JSON endpoint (server-side JSON API, no rendering needed)
    price, method, currency = _try_shopify(url)
    if price is not None:
        return price, method, currency

    # Step 2: Fetch rendered HTML
    html: str | None = None
    quota_exhausted = False

    if os.environ.get("FIRECRAWL_API_KEY", ""):
        fc_html, fc_err = _firecrawl_scrape(url)
        if fc_html:
            html = fc_html
        elif fc_err == "quota":
            # Credit exhaustion — degrade immediately, no point trying extraction
            quota_exhausted = True

    if html is None and not quota_exhausted:
        # No Firecrawl key, or Firecrawl errored (non-quota) — fall back to urllib
        status, raw = _fetch(url)
        if status and status < 400 and raw:
            snippet = raw[:3000].lower()
            if not any(
                kw in snippet
                for kw in ("captcha", "access denied", "bot detection", "cf-browser-verification")
            ):
                html = raw

    # Step 3: HTML parse cascade (JSON-LD → OG → regex)
    if html:
        price, method, currency = _try_jsonld(html)
        if price is not None:
            return price, method, currency

        price, method, currency = _try_og(html)
        if price is not None:
            return price, method, currency

        price, method, currency = _try_regex(html)
        if price is not None:
            return price, "regex", currency

    # Step 4: Firecrawl LLM extraction — last resort when HTML parse came up empty
    if not quota_exhausted and os.environ.get("FIRECRAWL_API_KEY", ""):
        price, currency = _firecrawl_extract(url)
        if price is not None:
            return price, "firecrawl_extract", currency

    return None, "blocked", None


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------


def _discord_post(message: str) -> None:
    if not _DISCORD_SCRIPT.exists():
        print(f"[price-monitor] discord_post.py not found at {_DISCORD_SCRIPT}", file=sys.stderr)
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
            print(f"[price-monitor] discord_post failed: {result.stderr.strip()}", file=sys.stderr)
    except Exception as exc:
        print(f"[price-monitor] discord_post exception: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Follow-ups integration
# ---------------------------------------------------------------------------


def _create_followup(name: str, url: str, trigger_at: str, window_until: str | None) -> None:
    followup_py = _SKILL_ROOT / "followups" / "skill.py"
    if not followup_py.exists():
        print(f"[price-monitor] followups skill not found at {followup_py}", file=sys.stderr)
        return

    cmd = [
        "python3",
        str(followup_py),
        "add",
        "--subject",
        f"Price check: {name}",
        "--prompt",
        f"Time to manually check the price for **{name}**: {url}",
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
                f"[price-monitor] follow-up creation failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[price-monitor] follow-up exception: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _today_local() -> str:
    return datetime.now(_LOCAL_TZ).date().isoformat()


def _is_in_window(active_from: str | None, active_until: str | None) -> bool:
    today = _today_local()
    if active_from and today < active_from:
        return False
    if active_until and today > active_until:
        return False
    return True


def _extract_vendor(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else ""


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
    deal_drop_pct: float | None = None,
) -> str | None:
    """Return a human-readable deal reason, or None.

    deal_drop_pct: per-watch override; falls back to global DEAL_DROP_PCT when None.
    """
    effective_drop_pct = deal_drop_pct if deal_drop_pct is not None else DEAL_DROP_PCT

    # 1. Target price hit
    if target_price is not None and current_price <= target_price:
        return f"at or below target ${target_price:.2f}"

    # Fetch prior readings (exclude the just-inserted row by timestamp)
    prior_rows = conn.execute(
        """SELECT price FROM price_history
           WHERE watch_id = ? AND checked_at < ?
           ORDER BY checked_at DESC
           LIMIT ?""",
        (watch_id, inserted_at, N_MEDIAN),
    ).fetchall()
    prior_prices = [row[0] for row in prior_rows]

    if len(prior_prices) < 2:
        return None  # not enough history for statistical checks

    # 2. Drop ≥ effective_drop_pct% from trailing median
    median_price = _median(prior_prices[:N_MEDIAN])
    if median_price > 0:
        drop_pct = (median_price - current_price) / median_price * 100
        if drop_pct >= effective_drop_pct:
            return f"{drop_pct:.0f}% below {len(prior_prices)}-reading median ${median_price:.2f}"

    # 3. N-day low — is current price below every prior reading in the window?
    cutoff = (datetime.now(UTC) - timedelta(days=N_DAY_LOW)).isoformat()
    period_rows = conn.execute(
        """SELECT MIN(price) FROM price_history
           WHERE watch_id = ? AND checked_at >= ? AND checked_at < ?""",
        (watch_id, cutoff, inserted_at),
    ).fetchone()
    if period_rows and period_rows[0] is not None:
        prior_min = period_rows[0]
        if current_price < prior_min:
            return f"{N_DAY_LOW}-day low (previous min ${prior_min:.2f})"

    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args: list[str]) -> None:
    params: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            params[args[i][2:].replace("-", "_")] = args[i + 1]
            i += 2
        else:
            i += 1

    name = params.get("name", "").strip()
    url = params.get("url", "").strip()
    if not name or not url:
        print("Error: --name and --url are required", file=sys.stderr)
        sys.exit(1)

    target_price: float | None = None
    if "target_price" in params:
        try:
            target_price = float(params["target_price"])
        except ValueError:
            print("Error: --target-price must be a number", file=sys.stderr)
            sys.exit(1)

    deal_drop_pct: float | None = None
    if "deal_drop_pct" in params:
        try:
            deal_drop_pct = float(params["deal_drop_pct"])
            if deal_drop_pct <= 0:
                raise ValueError
        except ValueError:
            print("Error: --deal-drop-pct must be a positive number", file=sys.stderr)
            sys.exit(1)

    active_from = params.get("active_from") or None
    active_until = params.get("active_until") or None

    # Add-time probe — test readability before committing to a watch
    price, method, currency = _probe(url)

    if price is None:
        print(
            json.dumps(
                {
                    "status": "needs_reminder",
                    "name": name,
                    "url": url,
                    "parse_method": method,
                    "message": (
                        f"Can't read this vendor automatically ({method}) — "
                        "want me to just remind you to check it instead?"
                    ),
                },
                indent=2,
            )
        )
        return

    conn = _init_db()
    try:
        now_iso = _now_utc().isoformat()
        cur = conn.execute(
            """INSERT INTO price_watches
               (name, url, vendor, target_price, currency, last_price, last_checked,
                parse_method, status, fail_count, active_from, active_until, created_at,
                deal_drop_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, ?)""",
            (
                name,
                url,
                _extract_vendor(url),
                target_price,
                currency,
                price,
                now_iso,
                method,
                active_from,
                active_until,
                now_iso,
                deal_drop_pct,
            ),
        )
        watch_id = cur.lastrowid
        conn.execute(
            "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
            (watch_id, price, now_iso),
        )
        conn.commit()

        currency_str = f" {currency}" if currency else ""
        msg = f"Tracking — ${price:.2f}{currency_str} via {method}, I'll alert on drops."
        if target_price is not None:
            msg += f" Target: ${target_price:.2f}{currency_str}."
        if deal_drop_pct is not None:
            msg += f" Deal threshold: {deal_drop_pct:.0f}% (overrides global {DEAL_DROP_PCT:.0f}%)."

        print(
            json.dumps(
                {
                    "id": watch_id,
                    "status": "active",
                    "name": name,
                    "price": price,
                    "currency": currency,
                    "parse_method": method,
                    "deal_drop_pct": deal_drop_pct,
                    "message": msg,
                },
                indent=2,
            )
        )
    finally:
        conn.close()


def cmd_list(args: list[str]) -> None:
    conn = _init_db()
    try:
        rows = conn.execute(
            """SELECT id, name, last_price, currency, last_checked,
                      status, parse_method, active_from, active_until, target_price,
                      deal_drop_pct
               FROM price_watches
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, id ASC"""
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No price watches.")
        return

    headers = [
        "ID",
        "Name",
        "Price",
        "Cur",
        "Last Checked",
        "Status",
        "Method",
        "Date Range",
        "Target",
        "Drop%",
    ]
    col_widths = [len(h) for h in headers]

    table_rows: list[list[str]] = []
    for row in rows:
        (
            row_id,
            name,
            last_price,
            currency,
            last_checked,
            status,
            method,
            afrom,
            auntil,
            target,
            ddp,
        ) = row

        price_str = f"${last_price:.2f}" if last_price is not None else "—"
        cur_str = currency or "—"

        if last_checked:
            dt = datetime.fromisoformat(last_checked)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            checked_str = dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        else:
            checked_str = "—"

        if afrom or auntil:
            date_range = f"{afrom or '...'} → {auntil or '...'}"
        else:
            date_range = "—"

        target_str = f"${target:.2f}" if target is not None else "—"
        ddp_str = f"{ddp:.0f}%" if ddp is not None else f"({DEAL_DROP_PCT:.0f}%)"
        name_trunc = name[:32] if len(name) > 32 else name

        r = [
            str(row_id),
            name_trunc,
            price_str,
            cur_str,
            checked_str,
            status,
            method or "—",
            date_range,
            target_str,
            ddp_str,
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
    params: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            params[args[i][2:].replace("-", "_")] = args[i + 1]
            i += 2
        else:
            i += 1

    if "id" not in params:
        print("Error: --id is required", file=sys.stderr)
        sys.exit(1)
    action = params.get("action", "")
    if action not in ("bought", "passed"):
        print("Error: --action must be 'bought' or 'passed'", file=sys.stderr)
        sys.exit(1)

    watch_id = int(params["id"])
    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT name FROM price_watches WHERE id = ? AND status != 'resolved'",
            (watch_id,),
        ).fetchone()
        if not row:
            print(f"No active watch with id={watch_id}", file=sys.stderr)
            sys.exit(1)

        conn.execute(
            "UPDATE price_watches SET status = 'resolved' WHERE id = ?",
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


def cmd_set(args: list[str]) -> None:
    """Adjust per-watch settings on an existing watch. Currently supports --deal-drop-pct."""
    params: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            params[args[i][2:].replace("-", "_")] = args[i + 1]
            i += 2
        else:
            i += 1

    if "id" not in params:
        print("Error: --id is required", file=sys.stderr)
        sys.exit(1)

    watch_id = int(params["id"])

    if "deal_drop_pct" not in params:
        print("Error: nothing to set — supported options: --deal-drop-pct N", file=sys.stderr)
        sys.exit(1)

    raw = params["deal_drop_pct"]
    if raw.lower() in ("none", "null", "reset"):
        deal_drop_pct: float | None = None
        label = f"global ({DEAL_DROP_PCT:.0f}%)"
    else:
        try:
            deal_drop_pct = float(raw)
            if deal_drop_pct <= 0:
                raise ValueError
            label = f"{deal_drop_pct:.0f}%"
        except ValueError:
            print(
                "Error: --deal-drop-pct must be a positive number, or 'reset' to restore global",
                file=sys.stderr,
            )
            sys.exit(1)

    conn = _init_db()
    try:
        row = conn.execute("SELECT name FROM price_watches WHERE id = ?", (watch_id,)).fetchone()
        if not row:
            print(f"No watch with id={watch_id}", file=sys.stderr)
            sys.exit(1)
        conn.execute(
            "UPDATE price_watches SET deal_drop_pct = ? WHERE id = ?",
            (deal_drop_pct, watch_id),
        )
        conn.commit()
        print(
            json.dumps(
                {
                    "id": watch_id,
                    "name": row[0],
                    "deal_drop_pct": deal_drop_pct,
                    "message": f"Watch #{watch_id} ({row[0]}): deal threshold set to {label}.",
                },
                indent=2,
            )
        )
    finally:
        conn.close()


def cmd_check(args: list[str]) -> None:
    """Scheduled daily run: probe all active watches, detect deals, post to Discord."""
    conn = _init_db()
    try:
        watches = conn.execute(
            """SELECT id, name, url, target_price, currency, parse_method,
                      fail_count, active_from, active_until, deal_drop_pct
               FROM price_watches WHERE status = 'active'"""
        ).fetchall()
    finally:
        conn.close()

    if not watches:
        return

    deals: list[str] = []

    for (
        watch_id,
        name,
        url,
        target_price,
        currency,
        _parse_method,
        fail_count,
        active_from,
        active_until,
        watch_deal_drop_pct,
    ) in watches:
        if not _is_in_window(active_from, active_until):
            continue

        price, method, detected_currency = _probe(url)
        now_iso = _now_utc().isoformat()

        conn = _init_db()
        try:
            if price is None:
                new_fail = fail_count + 1
                if new_fail >= FAIL_THRESHOLD:
                    conn.execute(
                        "UPDATE price_watches SET status = 'degraded', fail_count = ?, last_checked = ? WHERE id = ?",
                        (new_fail, now_iso, watch_id),
                    )
                    conn.commit()

                    trigger = (datetime.now(_LOCAL_TZ) + timedelta(days=1)).date().isoformat()
                    _create_followup(name, url, trigger, active_until)

                    _discord_post(
                        f"**Price Monitor — Auto-degraded**\n"
                        f"Lost the ability to read **{name}**'s price automatically "
                        f"after {new_fail} consecutive failures — switched it to a weekly reminder."
                    )
                    print(
                        f"[price-monitor] watch={watch_id} name={name!r} degraded after {new_fail} failures",
                        file=sys.stderr,
                    )
                else:
                    conn.execute(
                        "UPDATE price_watches SET fail_count = ?, last_checked = ? WHERE id = ?",
                        (new_fail, now_iso, watch_id),
                    )
                    conn.commit()
                    print(
                        f"[price-monitor] watch={watch_id} name={name!r} unreadable fail_count={new_fail}",
                        file=sys.stderr,
                    )
                continue

            # Successful read — reset fail count, record history, update watch
            stored_currency = detected_currency or currency
            conn.execute(
                """UPDATE price_watches
                   SET last_price = ?, last_checked = ?, fail_count = 0,
                       currency = COALESCE(?, currency), parse_method = ?
                   WHERE id = ?""",
                (price, now_iso, detected_currency, method, watch_id),
            )
            conn.execute(
                "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
                (watch_id, price, now_iso),
            )
            conn.commit()

            deal_reason = _detect_deal(
                conn, watch_id, price, target_price, now_iso, watch_deal_drop_pct
            )
            currency_label = f" {stored_currency}" if stored_currency else ""

            if deal_reason:
                deals.append(f"**{name}** — ${price:.2f}{currency_label} ({deal_reason})\n<{url}>")
                print(
                    f"[price-monitor] watch={watch_id} name={name!r} DEAL price={price} reason={deal_reason}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[price-monitor] watch={watch_id} name={name!r} ok price={price}",
                    file=sys.stderr,
                )
        finally:
            conn.close()

    if deals:
        body = "\n\n".join(deals)
        _discord_post(f"**Price Monitor — Deals Found**\n\n{body}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <command> [args...]\n"
            "Commands:\n"
            "  add --name N --url U [--target-price P] [--deal-drop-pct N]\n"
            "      [--active-from YYYY-MM-DD] [--active-until YYYY-MM-DD]\n"
            "  list\n"
            "  resolve --id N --action bought|passed\n"
            "  set --id N --deal-drop-pct N|reset\n"
            "  check",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "resolve": cmd_resolve,
        "set": cmd_set,
        "check": cmd_check,
    }

    if cmd not in dispatch:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)

    dispatch[cmd](args)


if __name__ == "__main__":
    main()
