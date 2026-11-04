"""Scheduled company events for a list of tickers (Yahoo Finance via yfinance).

Covers earnings (date, before-open/after-close, consensus EPS and revenue, options-implied
move), ex-dividend and pay dates, and standard monthly/quarterly options expirations.
Macro events (FOMC, CPI, jobs...) are NOT included; use web search for those.
"""

import math
import warnings
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# yfinance 1.7.0's get_earnings_dates calls DataFrame.rename(..., inplace=True), which pandas 3.1
# warns about (it will stop working in pandas 4). Harmless for now; hide only that message.
# Remove this once a newer yfinance fixes it.
warnings.filterwarnings("ignore", message=r"The inplace keyword in DataFrame\.rename is deprecated")

try:  # retries, a short-lived cache and backup sources (market_data.py in the project root)
    import market_data as _md
except ImportError:  # the tool still works without it, just without those extras
    _md = None

SOURCE = "Yahoo Finance via yfinance"
ET = ZoneInfo("America/New_York")


class EventsCalendarInput(BaseModel):
    symbols: str = Field(..., description="Comma-separated tickers, e.g. 'AAPL, FCX, MSFT'")
    days_ahead: int = Field(45, description="How many days ahead to look (default 45)")


def _ok(x: Any) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _money(x: Any) -> str:
    if not _ok(x):
        return "N/A"
    x = float(x)
    for unit, div in (("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"{x / div:,.2f}{unit}"
    return f"{x:,.2f}"


def timing_from_timestamp(ts: pd.Timestamp) -> tuple[str, str]:
    """Return (BMO/AMC/During market/Unknown, 'HH:MM ET' or 'N/A')."""
    ts = ts.tz_convert(ET) if ts.tzinfo else ts.tz_localize(ET)
    if ts.hour == 0 and ts.minute == 0:
        return "Unknown", "N/A"
    hhmm = ts.strftime("%H:%M ET")
    if ts.hour < 9 or (ts.hour == 9 and ts.minute < 30):
        return "BMO", hhmm
    if ts.hour >= 16:
        return "AMC", hhmm
    return "During market", hhmm


def third_fridays(start: date, end: date) -> list[tuple[date, str]]:
    """Standard monthly options expirations (third Friday) between start and end."""
    out, y, m = [], start.year, start.month
    while True:
        first = date(y, m, 1)
        fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        if fri > end:
            break
        if fri >= start:
            out.append((fri, "Quarterly OpEx" if m in (3, 6, 9, 12) else "Monthly OpEx"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _implied_move(t: yf.Ticker, earnings_day: date) -> Optional[float]:
    """ATM straddle mid / spot for the first expiry on or after the earnings date."""
    try:
        exps = [e for e in t.options if date.fromisoformat(e) >= earnings_day]
        if not exps:
            return None
        spot = float(t.history(period="5d")["Close"].dropna().iloc[-1])
        chain = t.option_chain(exps[0])
        k = chain.calls.iloc[(chain.calls["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0]

        def mid(df: pd.DataFrame) -> Optional[float]:
            r = df[df["strike"] == k]
            if r.empty:
                return None
            b, a = r["bid"].iloc[0], r["ask"].iloc[0]
            return (b + a) / 2 if b > 0 and a > 0 else None

        c, p = mid(chain.calls), mid(chain.puts)
        return (c + p) / spot if c and p else None
    except Exception:
        return None


def _earnings(t: yf.Ticker, today: date, end: date) -> list[dict]:
    cal: dict = {}
    try:
        cal = t.calendar or {}
    except Exception:
        pass
    cal_dates = [d for d in cal.get("Earnings Date", []) if isinstance(d, date)]
    is_range = len(cal_dates) > 1

    rows: list[dict] = []
    try:
        ed = t.get_earnings_dates(limit=8)
        if ed is not None and not ed.empty:
            for ts, r in ed.iterrows():
                d = ts.tz_convert(ET).date() if ts.tzinfo else ts.date()
                if today <= d <= end:
                    timing, at = timing_from_timestamp(ts)
                    rows.append({"date": d, "timing": timing, "time": at, "eps": r.get("EPS Estimate")})
    except Exception:
        pass
    if not rows and cal_dates:
        d = min(cal_dates)
        if today <= d <= end:
            rows.append({"date": d, "timing": "Unknown", "time": "N/A", "eps": cal.get("Earnings Average")})
    for r in rows:
        r["status"] = "Estimated (date range)" if is_range else "Unverified - check IR page"
        r["rev"] = cal.get("Revenue Average")
        if not _ok(r["eps"]):
            r["eps"] = cal.get("Earnings Average")
    return rows


class EventsCalendarTool(BaseTool):
    name: str = "events_calendar"
    description: str = (
        "Upcoming scheduled events for a comma-separated list of tickers over the next N days "
        "(default 45): earnings date, before-open/after-close timing and time ET, consensus EPS and "
        "revenue, options-implied earnings move, ex-dividend and pay dates with last dividend amount, "
        "plus monthly/quarterly options expiration dates. Earnings status is marked Estimated or "
        "Unverified; confirm on the company's investor-relations page. Does not include macro events."
    )
    args_schema: type[BaseModel] = EventsCalendarInput

    def _run(self, symbols: str, days_ahead: int = 45) -> str:
        key = {"symbols": ",".join(sorted({s.strip().upper() for s in symbols.replace(";", ",").split(",") if s.strip()})),
               "days_ahead": days_ahead}
        if _md is None:
            return self._fetch(symbols, days_ahead)
        return _md.resilient("events_calendar", key, 21600, lambda: self._fetch(symbols, days_ahead))

    def _fetch(self, symbols: str, days_ahead: int = 45) -> str:
        syms = sorted({s.strip().upper() for s in symbols.replace(";", ",").split(",") if s.strip()})
        if not syms:
            return "ERROR: no symbols given."
        today = datetime.now(ET).date()
        end = today + timedelta(days=days_ahead)
        earnings, dividends, errors = [], [], []

        for s in syms:
            try:
                t = yf.Ticker(s)
                # Probe first: the event lookups below swallow errors, so without this a
                # network failure would look like "no events".
                probe = t.history(period="5d")
                if probe is None or probe.empty:
                    errors.append(f"{s}: no data returned (fetch failed or unknown symbol)")
                    continue
                for r in _earnings(t, today, end):
                    r["symbol"] = s
                    r["move"] = _implied_move(t, r["date"])
                    earnings.append(r)
                cal = {}
                try:
                    cal = t.calendar or {}
                except Exception:
                    pass
                ex, pay = cal.get("Ex-Dividend Date"), cal.get("Dividend Date")
                if isinstance(ex, date) and today - timedelta(days=3) <= ex <= end:
                    amt = None
                    try:
                        divs = t.dividends
                        amt = float(divs.iloc[-1]) if len(divs) else None
                    except Exception:
                        pass
                    dividends.append({"symbol": s, "ex": ex, "pay": pay if isinstance(pay, date) else None, "amt": amt})
            except Exception as e:
                errors.append(f"{s}: {e}")

        # Backup for earnings: Finnhub (needs FINNHUB_API_KEY), for symbols where Yahoo failed or
        # found no earnings. Its dates are treated as Estimated.
        backup_used = []
        if _md is not None:
            have = {r["symbol"] for r in earnings}
            for s in syms:
                if s in have:
                    continue
                rows = _md.finnhub_earnings(s, today, end)
                if rows is None:
                    continue
                for r in rows:
                    earnings.append({"symbol": s, "date": r["date"], "timing": r["timing"], "time": "N/A",
                                     "status": "Estimated (Finnhub)", "eps": r["eps"], "rev": r["rev"], "move": None})
                backup_used.append(s)
        if len(errors) == len(syms) and not backup_used:
            return ("ERROR: could not fetch event data for any symbol. Report events as N/A (unavailable), "
                    "not as 'none'.\n" + "\n".join(f"- {e}" for e in errors))
        now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        out = [f"## Events {today} to {end}", f"Source: {SOURCE}, retrieved {now}. All dates YYYY-MM-DD, times ET."]
        if backup_used:
            out.append(f"Earnings for {', '.join(backup_used)} checked with the backup source Finnhub "
                       "(Yahoo failed or had none); those dates are marked Estimated (Finnhub).")
        if errors:
            out.append(f"WARNING: Yahoo returned no data for {', '.join(e.split(':')[0] for e in errors)}; their dividends, "
                       "and their earnings unless listed from Finnhub, are N/A (unavailable), not 'none'.")

        out.append("\n### Earnings")
        if earnings:
            out.append("| Date | Timing | Time ET | Symbol | Status | Consensus EPS | Consensus revenue | Implied move |\n|---|---|---|---|---|---|---|---|")
            for r in sorted(earnings, key=lambda x: (x["date"], x["symbol"])):
                eps = f"{float(r['eps']):.2f}" if _ok(r["eps"]) else "N/A"
                mv = f"±{r['move']:.1%}" if r["move"] else "N/A"
                out.append(f"| {r['date']} | {r['timing']} | {r['time']} | {r['symbol']} | {r['status']} | {eps} | {_money(r['rev'])} | {mv} |")
        else:
            out.append("No earnings dates found in the window.")

        out.append("\n### Dividends")
        if dividends:
            out.append("| Symbol | Ex-dividend | Pay date | Last amount |\n|---|---|---|---|")
            for d in sorted(dividends, key=lambda x: x["ex"]):
                amt = f"{d['amt']:.4f}" if d["amt"] else "N/A"
                out.append(f"| {d['symbol']} | {d['ex']} | {d['pay'] or 'N/A'} | {amt} |")
        else:
            out.append("No ex-dividend dates found in the window.")

        out.append("\n### Standard options expirations")
        out.append("| Date | Type |\n|---|---|")
        out += [f"| {d} | {k} |" for d, k in third_fridays(today, end)]
        out.append("Note: if a third Friday is a market holiday, expiration moves to Thursday.")

        if errors:
            out.append("\n### Errors\n" + "\n".join(f"- {e}" for e in errors))
        out.append("\nNot included: macro events (FOMC, CPI, PPI, jobs, GDP, PCE) and record dates. Use web search for these.")
        return "\n".join(out)
