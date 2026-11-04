"""Pre-market snapshot for a watchlist: exact prices and levels for the daily report.

Pulls quotes straight from Yahoo Finance (via yfinance) so the report never relies on prices read
off web pages or search snippets. For every symbol it records:

  price now      pre-market price (4:00-9:30 ET), or the regular-session / after-hours price,
                 or the last close when the market is closed, plus the time of that price
  vs prior close $ and % change against the last completed session's close
  pre-market     high, low and volume; volume compared with the average of the previous 4
                 pre-markets (HIGH > 1.5x, LOW < 0.67x, else AVERAGE)
  levels         prior-day high/low/close, 20/50/200-day averages, 52-week high/low,
                 30-day average daily volume
  earnings       next earnings date from Yahoo, flagged if it falls in the session's week or the
                 week after (Monday-Friday)
plus a market backdrop (S&P 500 and Nasdaq futures, SPY, QQQ, VIX) and the session status.

Anything that can't be fetched is null in the JSON and "N/A" in the table, never estimated.

Usage (from the project root):
    uv run python scripts/premarket_snapshot.py
    uv run python scripts/premarket_snapshot.py --watchlist /path/to/Watchlist.csv --out-dir reports
Writes <out-dir>/YYYYMMDD_HHMM_premarket_snapshot.json and .md (default out-dir:
output/premarket) and prints both paths. The watchlist CSV needs a "symbol" column (any case);
without one, the first column is used.
"""
import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
_TICKER = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,11}$")
BACKDROP = [("ES=F", "S&P 500 futures"), ("NQ=F", "Nasdaq 100 futures"), ("SPY", "SPY"),
            ("QQQ", "QQQ"), ("^VIX", "VIX")]
WORKERS = 4
ATTEMPTS = 2  # Yahoo sometimes returns empty data on the first try

# NYSE full-day closures and 1:00 PM early closes, from https://www.nyse.com/trade/hours-calendars
# (checked 2026-11-01). Add the next year's dates when NYSE publishes them.
NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
    date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29), date(2028, 6, 19),
    date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23), date(2028, 12, 25),
}
NYSE_EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26), date(2028, 7, 3),
                    date(2028, 11, 24)}


def close_time(d: date) -> str:
    """Regular-session close (ET) as HH:MM."""
    return "13:00" if d in NYSE_EARLY_CLOSE else "16:00"


# ---------------------------------------------------------------------------
# Session calendar
# ---------------------------------------------------------------------------

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def session_info(now: datetime) -> dict:
    """Which session the report is for, and where the market is right now."""
    today, t = now.date(), now.time()
    if is_trading_day(today) and now.strftime("%H:%M") < close_time(today):
        session = today
        if t < datetime.strptime("04:00", "%H:%M").time():
            phase = "overnight (pre-market opens 4:00 AM ET)"
        elif t < datetime.strptime("09:30", "%H:%M").time():
            phase = "pre-market"
        else:
            phase = "regular session"
    else:
        session = next_trading_day(today)
        if is_trading_day(today) and t < datetime.strptime("20:00", "%H:%M").time():
            phase = "after-hours"
        else:
            phase = "market closed today" if not is_trading_day(today) else "closed (after 8:00 PM ET)"
    friday = session + timedelta(days=4 - session.weekday())         # end of the session's week
    week_start = session + timedelta(days=(7 - session.weekday()))  # Monday after the session's week
    return {
        "run_time_et": now.strftime("%Y-%m-%d %H:%M %Z"),
        "market_phase": phase,
        "market_open_today": is_trading_day(today),
        "next_session": session.isoformat(),
        "this_week": [session.isoformat(), friday.isoformat()],
        "next_week": [week_start.isoformat(), (week_start + timedelta(days=4)).isoformat()],
    }


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _num(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else round(x, nd)  # NaN -> None


def _et(ts) -> datetime:
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)


def _retry(fn):
    for i in range(ATTEMPTS):
        try:
            out = fn()
            if out is not None and not getattr(out, "empty", False):
                return out
        except Exception:  # noqa: BLE001 - network hiccups; try again, then give up
            pass
        if i < ATTEMPTS - 1:
            time.sleep(2)
    return None


def _volume_label(ratio):
    if ratio is None:
        return None
    return "HIGH" if ratio > 1.5 else "LOW" if ratio < 0.67 else "AVERAGE"


def snapshot(symbol: str, now: datetime, session: dict, levels: bool = True) -> dict:
    import yfinance as yf

    out = {"symbol": symbol, "source": "Yahoo Finance (yfinance)", "error": None}
    t = yf.Ticker(symbol)
    daily = _retry(lambda: t.history(period="1y", interval="1d", auto_adjust=False))
    if daily is None:
        out["error"] = "no price data (unknown symbol, or Yahoo unreachable)"
        return out

    today = now.date()
    days = [_et(i).date() for i in daily.index]
    # Completed sessions: before today, plus today once the close (4:00 PM, or 1:00 PM on early-close days) has passed.
    closed_today = now.strftime("%H:%M") >= close_time(today)
    done = [i for i, d in enumerate(days) if d < today or (closed_today and d == today)]
    if not done:
        out["error"] = "no completed session in the price history"
        return out
    prior = daily.iloc[done[-1]]
    prior_close = _num(prior["Close"])
    out["prior_session"] = days[done[-1]].isoformat()
    out["prior_close"] = prior_close
    out["prior_high"] = _num(prior["High"])
    out["prior_low"] = _num(prior["Low"])

    # Latest price: today's minute bars including pre/after-hours.
    price, price_time, basis = prior_close, None, "last close (no trading yet today)"
    pre = {"high": None, "low": None, "volume": None, "volume_vs_avg": None, "volume_label": None}
    intraday = _retry(lambda: t.history(period="5d", interval="1m", prepost=True))
    if intraday is not None:
        bars = intraday.copy()
        bars["et"] = [_et(i) for i in bars.index]
        today_bars = bars[[x.date() == today for x in bars["et"]]]
        if not today_bars.empty:
            last = today_bars.iloc[-1]
            price, price_time = _num(last["Close"]), last["et"]
            hm = price_time.strftime("%H:%M")
            basis = ("pre-market" if hm < "09:30" else "regular session" if hm < close_time(today) else "after-hours")
        # Pre-market (4:00-9:30 ET) today vs the previous pre-markets in the 5-day window.
        def premarket(d):
            return bars[[x.date() == d and "04:00" <= x.strftime("%H:%M") < "09:30" for x in bars["et"]]]
        pm = premarket(today)
        if not pm.empty:
            pre["high"], pre["low"] = _num(pm["High"].max()), _num(pm["Low"].min())
            pre["volume"] = int(pm["Volume"].sum())
            past = [premarket(d)["Volume"].sum() for d in sorted({x.date() for x in bars["et"]}) if d < today]
            past = [v for v in past if v > 0][-4:]
            if past and session["market_phase"] == "pre-market":
                # Compare with the same stretch of earlier pre-markets (4:00 AM up to now).
                cutoff = now.strftime("%H:%M")
                past = [bars[[x.date() == d and "04:00" <= x.strftime("%H:%M") < min(cutoff, "09:30")
                              for x in bars["et"]]]["Volume"].sum()
                        for d in sorted({x.date() for x in bars["et"]}) if d < today]
                past = [v for v in past if v > 0][-4:]
            if past:
                ratio = pre["volume"] / (sum(past) / len(past))
                pre["volume_vs_avg"] = round(ratio, 2)
                pre["volume_label"] = _volume_label(ratio)

    out["price"] = price
    out["price_basis"] = basis
    out["price_time_et"] = price_time.strftime("%Y-%m-%d %H:%M") if price_time else out["prior_session"] + " close"
    if price is not None and prior_close and price_time is not None:
        out["change"] = round(price - prior_close, 2)
        out["change_pct"] = round((price - prior_close) / prior_close * 100, 2)
    else:
        out["change"] = out["change_pct"] = None
    out["premarket"] = pre

    if not levels:
        return out

    closes = daily["Close"].iloc[: done[-1] + 1]
    year = daily.iloc[: done[-1] + 1]
    out["levels"] = {
        "sma20": _num(closes.tail(20).mean()) if len(closes) >= 20 else None,
        "sma50": _num(closes.tail(50).mean()) if len(closes) >= 50 else None,
        "sma200": _num(closes.tail(200).mean()) if len(closes) >= 200 else None,
        "high_52w": _num(year["High"].max()),
        "low_52w": _num(year["Low"].min()),
        "avg_volume_30d": int(year["Volume"].tail(30).mean()) if len(year) else None,
    }

    info = {}
    try:
        info = t.get_info() or {}
    except Exception:  # noqa: BLE001
        pass
    out["name"] = info.get("shortName") or info.get("longName")
    out["type"] = info.get("quoteType")  # EQUITY, ETF, ...

    out["earnings"] = {"next_date": None, "this_week": False, "next_week": False}
    if out["type"] != "ETF":
        try:
            cal = t.calendar or {}
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                nxt = min(d for d in dates if d >= today) if any(d >= today for d in dates) else None
                if nxt:
                    def within(key):
                        lo, hi = (date.fromisoformat(x) for x in session[key])
                        return lo <= nxt <= hi
                    out["earnings"] = {"next_date": nxt.isoformat(), "this_week": within("this_week"),
                                       "next_week": within("next_week"),
                                       "note": "Yahoo's date; confirm with the company before relying on it"}
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# Watchlist and output
# ---------------------------------------------------------------------------

def read_watchlist(path: Path) -> tuple[list, list]:
    """Symbols from the CSV (deduplicated, in order) and the entries that aren't valid tickers."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    header = [h.strip().lower() for h in rows[0]]
    col = header.index("symbol") if "symbol" in header else 0
    body = rows[1:] if "symbol" in header or not _TICKER.match(rows[0][0].strip().upper()) else rows
    symbols, invalid = [], []
    for r in body:
        if len(r) <= col or not r[col].strip():
            continue
        s = r[col].strip().upper().lstrip("$")
        if not _TICKER.match(s):
            invalid.append(r[col].strip())
        elif s not in symbols:
            symbols.append(s)
    return symbols, invalid


def _f(x, money=False, pct=False):
    if x is None:
        return "N/A"
    if pct:
        return f"{x:+.2f}%"
    if money:
        return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"
    return f"{x:,}"


def to_markdown(data: dict) -> str:
    s = data["session"]
    lines = [f"# Pre-market snapshot — {s['run_time_et']}", "",
             f"Market: {s['market_phase']}. Report session: {s['next_session']}. "
             f"Prices from {data['source']}; N/A = not available, never estimated.", "",
             "## Market backdrop", "", "| Instrument | Price | vs prior close | Price time (ET) |", "|---|---|---|---|"]
    for b in data["backdrop"]:
        index = b["symbol"].startswith("^")  # an index level (VIX), not a dollar price
        lines.append(f"| {b['label']} ({b['symbol']}) | {_f(b.get('price'), money=not index)} | "
                     f"{_f(b.get('change_pct'), pct=True)} | {b.get('price_time_et', 'N/A')} |")
    lines += ["", "## Watchlist", "",
              "| Symbol | Price | Basis | Time (ET) | Chg | Chg % | Prior close | Prior H / L | Pre-mkt H / L | "
              "Pre-mkt vol (vs avg) | SMA20 / 50 / 200 | 52w H / L | Next earnings |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for q in data["tickers"]:
        if q.get("error"):
            lines.append(f"| {q['symbol']} | ⚠ N/A — verify live | {q['error']} |" + " |" * 10)
            continue
        pm, lv, er = q["premarket"], q.get("levels", {}), q.get("earnings", {})
        vol = "N/A" if pm["volume"] is None else (
            f"{pm['volume']:,}" + (f" ({pm['volume_label']}, {pm['volume_vs_avg']}x)" if pm["volume_label"] else ""))
        earn = "ETF" if q.get("type") == "ETF" else (er.get("next_date") or "N/A") + (" **this week**" if er.get("this_week") else " **next week**" if er.get("next_week") else "")
        lines.append(
            f"| {q['symbol']} | {_f(q['price'], money=True)} | {q['price_basis']} | {q['price_time_et']} | "
            f"{_f(q['change'], money=True)} | {_f(q['change_pct'], pct=True)} | {_f(q['prior_close'], money=True)} | "
            f"{_f(q['prior_high'], money=True)} / {_f(q['prior_low'], money=True)} | "
            f"{_f(pm['high'], money=True)} / {_f(pm['low'], money=True)} | {vol} | "
            f"{_f(lv.get('sma20'), money=True)} / {_f(lv.get('sma50'), money=True)} / {_f(lv.get('sma200'), money=True)} | "
            f"{_f(lv.get('high_52w'), money=True)} / {_f(lv.get('low_52w'), money=True)} | {earn} |")
    if data["invalid_entries"]:
        lines += ["", "Not valid ticker symbols (skipped): " + ", ".join(data["invalid_entries"])]
    failed = [q["symbol"] for q in data["tickers"] if q.get("error")]
    lines += ["", "Data gaps: " + (", ".join(failed) if failed else "none")]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watchlist", default=str(ROOT / "data" / "watchlist.csv"), help="CSV with a 'symbol' column")
    ap.add_argument("--out-dir", default=str(ROOT / "output" / "premarket"), help="folder for the JSON and .md files")
    ap.add_argument("--now", help=argparse.SUPPRESS)  # testing only: pretend the run time is this ISO time (ET)
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).replace(tzinfo=ET) if args.now else datetime.now(ET)
    wl = Path(args.watchlist)
    if not wl.exists():
        sys.exit(f"Watchlist not found: {wl}")
    symbols, invalid = read_watchlist(wl)
    if not symbols:
        sys.exit(f"No ticker symbols found in {wl}")
    session = session_info(now)

    with ThreadPoolExecutor(WORKERS) as pool:
        tickers = list(pool.map(lambda s: snapshot(s, now, session), symbols))
        backdrop = list(pool.map(lambda s: snapshot(s[0], now, session, levels=False), BACKDROP))
    for b, (sym, label) in zip(backdrop, BACKDROP):
        b["label"] = label

    data = {"session": session, "source": "Yahoo Finance via yfinance (may be delayed up to 15 min)",
            "watchlist_file": str(wl), "invalid_entries": invalid, "backdrop": backdrop, "tickers": tickers}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%Y%m%d_%H%M") + "_premarket_snapshot"
    (out_dir / f"{stem}.json").write_text(json.dumps(data, indent=2, default=str))
    (out_dir / f"{stem}.md").write_text(to_markdown(data))
    ok = sum(1 for q in tickers if not q.get("error"))
    print(f"{ok}/{len(tickers)} symbols fetched; market: {session['market_phase']}; session {session['next_session']}")
    print(out_dir / f"{stem}.json")
    print(out_dir / f"{stem}.md")


if __name__ == "__main__":
    main()
