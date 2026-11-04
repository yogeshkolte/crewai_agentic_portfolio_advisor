"""The two tools the stock chat agent can call: today's price and today's news for one ticker.

Both return plain JSON text for the model. Results are cached briefly (market_data.py in the
project root), so asking twice about the same ticker doesn't hit Yahoo again.

    get_quote("AAPL")   -> last price, change vs previous close, day range, volume, as-of time
    get_news("AAPL")    -> today's headlines (Yahoo, then Finnhub, then Serper news search)
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import market_data as _md  # noqa: E402  retries + short same-day cache

ET = ZoneInfo("America/New_York")
QUOTE_TTL = 60          # seconds: a quote older than this is fetched again
NEWS_TTL = 10 * 60      # seconds
MAX_NEWS = 8
# Tickers like AAPL, BRK-B, BRK.B, ^GSPC, EURUSD=X. Anything else is refused before any request.
_TICKER = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,11}$")


def clean_ticker(symbol: str) -> str | None:
    s = (symbol or "").strip().upper().lstrip("$")
    return s if _TICKER.match(s) else None


def _num(x):
    try:
        x = float(x)
        return None if x != x else x  # NaN -> None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------

def _fetch_quote(sym: str) -> str:
    import yfinance as yf

    t = yf.Ticker(sym)
    bars = t.history(period="5d", interval="1d", auto_adjust=False)
    if bars is None or bars.empty:
        return f"ERROR: no price data for {sym} (unknown ticker, or Yahoo is unreachable: not enough price history)."
    last_bar = bars.iloc[-1]
    prev_close = _num(bars["Close"].iloc[-2]) if len(bars) > 1 else None

    price, price_time = _num(last_bar["Close"]), bars.index[-1]
    try:  # latest regular-session minute, for a live price during market hours
        intraday = t.history(period="1d", interval="1m", prepost=False)
        if intraday is not None and not intraday.empty:
            price, price_time = _num(intraday["Close"].iloc[-1]), intraday.index[-1]
    except Exception:  # noqa: BLE001 - the daily bar is good enough
        pass

    info = {}
    try:
        fi = t.fast_info
        info = {"currency": fi.get("currency"), "exchange": fi.get("exchange"),
                "market_cap": _num(fi.get("marketCap"))}
    except Exception:  # noqa: BLE001
        pass

    as_of = price_time.to_pydatetime()
    as_of = as_of.astimezone(ET) if as_of.tzinfo else as_of.replace(tzinfo=ET)
    now = datetime.now(ET)
    change = (price - prev_close) if price is not None and prev_close else None
    out = {
        "symbol": sym,
        "price": round(price, 4) if price is not None else None,
        "previous_close": round(prev_close, 4) if prev_close else None,
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change / prev_close * 100, 2) if change is not None else None,
        "day_open": _num(last_bar["Open"]),
        "day_high": _num(last_bar["High"]),
        "day_low": _num(last_bar["Low"]),
        "volume": int(last_bar["Volume"]) if _num(last_bar["Volume"]) is not None else None,
        "as_of_et": as_of.strftime("%Y-%m-%d %H:%M %Z"),
        "is_today": as_of.date() == now.date(),
        "note": ("Live price from today's regular session (may be ~15 min delayed)."
                 if as_of.date() == now.date() else
                 "Market has not traded today yet (weekend, holiday or before the open): this is the last close."),
        "source": "Yahoo Finance",
        **{k: v for k, v in info.items() if v is not None},
    }
    return json.dumps(out)


def get_quote(symbol: str) -> str:
    sym = clean_ticker(symbol)
    if not sym:
        return json.dumps({"error": f"'{symbol}' is not a valid ticker symbol."})
    return _md.resilient("chat_quote", {"symbol": sym}, QUOTE_TTL, lambda: _fetch_quote(sym))


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def _item(title, publisher, url, published: datetime | None, summary=""):
    return {"title": (title or "").strip(), "publisher": publisher or "",
            "url": url or "", "published_et": published.astimezone(ET).strftime("%Y-%m-%d %H:%M") if published else "",
            "summary": (summary or "").strip()[:300]}


def _yahoo_news(sym: str) -> list[dict]:
    import yfinance as yf
    items = []
    for n in (yf.Ticker(sym).news or [])[:30]:
        c = n.get("content") or n  # yfinance >= 0.2.50 nests the fields under "content"
        published = None
        if c.get("pubDate"):
            published = datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00"))
        elif n.get("providerPublishTime"):
            published = datetime.fromtimestamp(n["providerPublishTime"], tz=timezone.utc)
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
               or n.get("link"))
        publisher = (c.get("provider") or {}).get("displayName") or n.get("publisher")
        items.append(_item(c.get("title"), publisher, url, published, c.get("summary", "")))
    return items


def _finnhub_news(sym: str, day: str) -> list[dict] | None:
    token = os.getenv("FINNHUB_API_KEY")
    if not token:
        return None  # not configured
    data = _md._get_json(f"https://finnhub.io/api/v1/company-news?symbol={sym}&from={day}&to={day}&token={token}")
    return [_item(d.get("headline"), d.get("source"), d.get("url"),
                  datetime.fromtimestamp(d["datetime"], tz=timezone.utc) if d.get("datetime") else None,
                  d.get("summary", "")) for d in (data or [])[:30]]


def _serper_news(sym: str) -> list[dict] | None:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return None  # not configured
    req = urllib.request.Request(
        "https://google.serper.dev/news",
        data=json.dumps({"q": f"{sym} stock", "tbs": "qdr:d", "num": 10}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    # Serper gives relative dates ("3 hours ago"); qdr:d already limits results to the past day.
    return [{**_item(d.get("title"), d.get("source"), d.get("link"), None, d.get("snippet", "")),
             "published_et": d.get("date", "")} for d in data.get("news", [])]


def _fetch_news(sym: str) -> str:
    today = datetime.now(ET).date()
    tried, latest = [], []
    for name, fn in (("Yahoo Finance", lambda: _yahoo_news(sym)),
                     ("Finnhub", lambda: _finnhub_news(sym, today.isoformat())),
                     ("Serper news search (past 24 hours)", lambda: _serper_news(sym))):
        try:
            items = fn()
        except Exception as e:  # noqa: BLE001 - try the next source
            tried.append(f"{name}: failed ({str(e)[:80]})")
            continue
        if items is None:
            tried.append(f"{name}: no API key set")
            continue
        if name.startswith("Serper"):
            todays = items
        else:
            todays = [i for i in items if i["published_et"].startswith(today.isoformat())]
            latest = latest or items
        if todays:
            return json.dumps({"symbol": sym, "date_et": today.isoformat(), "source": name,
                               "articles": todays[:MAX_NEWS]})
        tried.append(f"{name}: no articles dated today")
    return json.dumps({"symbol": sym, "date_et": today.isoformat(), "articles": [],
                       "note": "No news dated today. " + "; ".join(tried),
                       "most_recent_articles": latest[:5]})


def get_news(symbol: str) -> str:
    sym = clean_ticker(symbol)
    if not sym:
        return json.dumps({"error": f"'{symbol}' is not a valid ticker symbol."})
    return _md.resilient("chat_news", {"symbol": sym}, NEWS_TTL, lambda: _fetch_news(sym))


# ---------------------------------------------------------------------------
# Tool schemas for the model (OpenAI function-calling format, used by LiteLLM)
# ---------------------------------------------------------------------------

_SYMBOL = {"type": "object", "required": ["symbol"], "properties": {
    "symbol": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL, MSFT, BRK-B"}}}

TOOLS = [
    {"type": "function", "function": {
        "name": "get_quote",
        "description": "Current (or latest) daily price for a stock ticker: price, change vs previous close, "
                       "day open/high/low, volume and the time of the price in US Eastern time.",
        "parameters": _SYMBOL}},
    {"type": "function", "function": {
        "name": "get_news",
        "description": "Today's news headlines about the company behind a stock ticker, with publisher, time and link.",
        "parameters": _SYMBOL}},
]
RUN = {"get_quote": get_quote, "get_news": get_news}
