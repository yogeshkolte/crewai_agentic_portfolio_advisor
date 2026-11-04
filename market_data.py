"""Shared data plumbing for the tools: retries, a short-lived disk cache, and backup sources.

Used by tools/*.py (each tool imports it if present and works without it).

RETRIES
    Yahoo sometimes rate-limits or times out. resilient() retries a fetch that fails with a
    network-style error, waiting a little longer each time (default 3 attempts, 2s then 6s).
    Set MARKET_DATA_RETRIES=1 to switch retries off.

CACHE
    Successful tool results are kept in .cache/market_data/ for a short time, so a re-run or a
    retried task doesn't download everything again: prices 15 min, events 6 h, fundamentals 12 h.
    Results are never reused from a previous day. Errors are never cached. Delete the folder
    to clear it. Set MARKET_DATA_CACHE=off to disable.

BACKUP SOURCES (used only when Yahoo fails or has nothing)
    Finnhub   earnings dates and estimates. Needs a free key: FINNHUB_API_KEY in .env.
    SEC EDGAR annual financial statements. Free, no key, but the SEC requires a User-Agent
              that identifies you: SEC_USER_AGENT="Your Name your@email.com" in .env.
    Without these settings the backups are skipped and the tools report N/A as before.
"""
import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache" / "market_data"
log = logging.getLogger("portfolio_crew.data")

# Error texts from the tools that are worth retrying (network or provider trouble).
# "not enough price history" is included because when Yahoo is unreachable, yfinance often
# returns empty data instead of raising, which the price tool reports that way.
# Input errors such as "no symbols given" are not retried.
_RETRYABLE = ("ERROR fetching", "ERROR downloading", "price data unavailable", "could not fetch",
              "no data returned", "not enough price history", "Too Many Requests", "Rate limit", "timed out")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _cache_path(namespace: str, key: dict) -> Path:
    digest = hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return CACHE_DIR / namespace / f"{digest}.json"


def cache_get(namespace: str, key: dict, ttl_seconds: int) -> Optional[str]:
    if os.getenv("MARKET_DATA_CACHE", "on").lower() == "off":
        return None
    p = _cache_path(namespace, key)
    try:
        rec = json.loads(p.read_text())
    except Exception:
        return None
    if rec.get("day") != date.today().isoformat() or time.time() - rec.get("t", 0) > ttl_seconds:
        return None
    return rec.get("value")


def cache_put(namespace: str, key: dict, value: str) -> None:
    if os.getenv("MARKET_DATA_CACHE", "on").lower() == "off":
        return
    try:
        p = _cache_path(namespace, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"t": time.time(), "day": date.today().isoformat(), "value": value}))
    except Exception:
        pass  # a cache that can't be written must never break a tool


# ---------------------------------------------------------------------------
# Retry + cache around a whole tool call
# ---------------------------------------------------------------------------
def resilient(namespace: str, key: dict, ttl_seconds: int, fetch: Callable[[], str]) -> str:
    """Return a cached result, or call fetch() with retries and cache a good result."""
    hit = cache_get(namespace, key, ttl_seconds)
    if hit is not None:
        return hit
    attempts = max(1, int(os.getenv("MARKET_DATA_RETRIES", "3")))
    waits = [2, 6, 15]
    out = ""
    for i in range(attempts):
        try:
            out = fetch()
        except Exception as e:  # tools normally catch their own errors; this is a safety net
            out = f"ERROR fetching {namespace}: {e}. Report figures as N/A (unavailable)."
        retryable = isinstance(out, str) and out.startswith("ERROR") and any(r in out for r in _RETRYABLE)
        if not retryable:
            break
        if i < attempts - 1:
            log.warning("%s %s failed (attempt %d/%d), retrying: %s", namespace, key, i + 1, attempts, out[:120])
            time.sleep(waits[min(i, len(waits) - 1)])
    if isinstance(out, str) and not out.startswith("ERROR"):
        cache_put(namespace, key, out)
    return out


# ---------------------------------------------------------------------------
# HTTP helper for the backup sources
# ---------------------------------------------------------------------------
def _get_json(url: str, headers: Optional[dict] = None, timeout: int = 15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # honours HTTPS_PROXY if set
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# Finnhub: earnings calendar
# ---------------------------------------------------------------------------
def finnhub_earnings(symbol: str, start: date, end: date) -> Optional[list[dict]]:
    """Earnings events for one symbol from Finnhub, or None if Finnhub isn't configured or fails.

    Each item: {"date": date, "timing": "BMO"/"AMC"/"During market"/"Unknown",
                "eps": float|None, "rev": float|None}
    """
    token = os.getenv("FINNHUB_API_KEY")
    if not token:
        return None
    key = {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()}
    cached = cache_get("finnhub_earnings", key, 6 * 3600)
    if cached is not None:
        raw = json.loads(cached)
    else:
        q = urllib.parse.urlencode({"from": start.isoformat(), "to": end.isoformat(), "symbol": symbol, "token": token})
        try:
            raw = _get_json(f"https://finnhub.io/api/v1/calendar/earnings?{q}").get("earningsCalendar") or []
        except Exception as e:
            log.warning("Finnhub earnings for %s failed: %s", symbol, e)
            return None
        cache_put("finnhub_earnings", key, json.dumps(raw))
    timing = {"bmo": "BMO", "amc": "AMC", "dmh": "During market"}
    out = []
    for e in raw:
        try:
            d = date.fromisoformat(e["date"])
        except Exception:
            continue
        out.append({"date": d, "timing": timing.get((e.get("hour") or "").lower(), "Unknown"),
                    "eps": e.get("epsEstimate"), "rev": e.get("revenueEstimate")})
    return out


# ---------------------------------------------------------------------------
# SEC EDGAR: annual financial statements
# ---------------------------------------------------------------------------
_REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
_CONCEPTS = {
    "revenue": _REVENUE,
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
}
_INSTANT = {"cash", "long_term_debt"}


def _sec_headers() -> Optional[dict]:
    ua = os.getenv("SEC_USER_AGENT")
    return {"User-Agent": ua, "Accept-Encoding": "identity"} if ua else None


def _sec_cik(symbol: str) -> Optional[int]:
    headers = _sec_headers()
    cached = cache_get("sec_tickers", {"v": 1}, 7 * 24 * 3600)
    if cached is not None:
        table = json.loads(cached)
    else:
        table = _get_json("https://www.sec.gov/files/company_tickers.json", headers)
        cache_put("sec_tickers", {"v": 1}, json.dumps(table))
    for row in table.values():
        if str(row.get("ticker", "")).upper() == symbol.upper():
            return int(row["cik_str"])
    return None


def annual_series(facts: dict, names: list[str], instant: bool = False) -> dict[str, float]:
    """{fiscal-year-end date: value} from 10-K filings, taking the first concept name that has data.

    Pure computation on a companyfacts JSON; separated for testing.
    """
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for name in names:
        units = (gaap.get(name) or {}).get("units") or {}
        entries = units.get("USD") or units.get("USD/shares") or []
        best: dict[str, tuple[str, float]] = {}
        for e in entries:
            if e.get("form") != "10-K" or e.get("fp") != "FY" or "end" not in e:
                continue
            if not instant:
                try:  # flows must cover about a year, not a quarter reported inside the 10-K
                    days = (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days
                except Exception:
                    continue
                if days < 300:
                    continue
            filed = e.get("filed", "")
            if e["end"] not in best or filed > best[e["end"]][0]:
                best[e["end"]] = (filed, float(e["val"]))
        if best:
            return {k: v for k, (_, v) in best.items()}
    return {}


def _money(x: Optional[float]) -> str:
    if x is None:
        return "N/A"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"{x / div:,.2f}{unit}"
    return f"{x:,.2f}"


def edgar_fundamentals_table(symbol: str, facts: dict) -> str:
    """Markdown summary of the last 4 fiscal years from a companyfacts JSON (pure; testable)."""
    s = {k: annual_series(facts, v, instant=k in _INSTANT) for k, v in _CONCEPTS.items()}
    years = sorted(s["revenue"] or s["net_income"], reverse=True)[:4]
    if not years:
        return f"SEC EDGAR has no annual (10-K) figures for {symbol}."
    lines = ["| Fiscal year end | Revenue | Rev growth | Operating margin | Net income | Free cash flow | EPS (diluted) |",
             "|---|---|---|---|---|---|---|"]
    for i, y in enumerate(years):
        rev = s["revenue"].get(y)
        prev = s["revenue"].get(years[i + 1]) if i + 1 < len(years) else None
        growth = f"{(rev / prev - 1) * 100:+.1f}%" if rev and prev else "N/A"
        op = s["operating_income"].get(y)
        margin = f"{op / rev * 100:.1f}%" if op is not None and rev else "N/A"
        ocf, capex = s["op_cash_flow"].get(y), s["capex"].get(y)
        fcf = ocf - capex if ocf is not None and capex is not None else None
        eps = s["eps_diluted"].get(y)
        lines.append(f"| {y} | {_money(rev)} | {growth} | {margin} | {_money(s['net_income'].get(y))} | "
                     f"{_money(fcf)} | {f'{eps:.2f}' if eps is not None else 'N/A'} |")
    last_cash = s["cash"][max(s["cash"])] if s["cash"] else None
    last_debt = s["long_term_debt"][max(s["long_term_debt"])] if s["long_term_debt"] else None
    return ("\n".join(lines) + f"\n\nLatest annual balance sheet: cash {_money(last_cash)}, long-term debt {_money(last_debt)}. "
            "Valuation ratios need a current price: combine with the price data. Quarterly figures are not included.")


def edgar_fundamentals(symbol: str) -> Optional[str]:
    """Annual statements for one company from SEC EDGAR, or None if not configured or not found."""
    headers = _sec_headers()
    if headers is None:
        return None
    try:
        cik = _sec_cik(symbol)
        if cik is None:
            return f"SEC EDGAR has no company filed under {symbol} (ETFs and funds are not covered)."
        key = {"cik": cik}
        cached = cache_get("sec_facts", key, 12 * 3600)
        if cached is not None:
            facts = json.loads(cached)
        else:
            facts = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers)
            cache_put("sec_facts", key, json.dumps(facts))
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return (f"## {symbol} fundamentals (backup source: SEC EDGAR 10-K filings, retrieved {now})\n"
                f"Yahoo Finance was unavailable, so these come from the company's annual filings.\n\n"
                + edgar_fundamentals_table(symbol, facts))
    except Exception as e:
        log.warning("SEC EDGAR fundamentals for %s failed: %s", symbol, e)
        return None
