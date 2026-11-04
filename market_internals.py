"""Market internals for the market briefing: breadth, credit conditions and relative leadership.

Fetched once at run start by data_snapshot.py and appended to {data_market}. Every reading
(Broad/Narrow, Widening/Tightening, Improving/Deteriorating) is computed here by a stated rule,
so the agent reports numbers instead of impressions.

    breadth     equal-weight vs cap-weight (RSP/SPY, QQQE/QQQ), mega caps vs the average stock
                (MAGS/RSP), small vs large (IWM/SPY), sector ETFs above their 50/200-day averages,
                and the share of this run's symbols above their 50/200-day averages
    credit      ICE BofA high-yield and investment-grade option-adjusted spreads (FRED, no key),
                and the HYG/IEF and LQD/IEF price ratios as a market-priced cross-check
    leadership  relative strength vs SPY for each sector ETF and each holding/watchlist name over
                the last month, the month before, and 3 months; Improving or Deteriorating

Run on its own:  uv run python market_internals.py AAPL MSFT
"""
from __future__ import annotations

import csv
import io
import logging
import sys
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

import market_data as _md

log = logging.getLogger("portfolio_crew.data")

SECTORS = {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health care",
           "XLI": "Industrials", "XLY": "Consumer discretionary", "XLP": "Consumer staples",
           "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate", "XLC": "Communication services"}
BREADTH = ("SPY", "RSP", "QQQ", "QQQE", "MAGS", "IWM")
CREDIT = ("HYG", "LQD", "IEF")
SPREADS = {"BAMLH0A0HYM2": "High-yield spread (ICE BofA US HY OAS)",
           "BAMLC0A0CM": "Investment-grade spread (ICE BofA US Corporate OAS)"}
M1, M3 = 21, 63            # trading days in 1 and 3 months
TREND_BAND = 0.5           # percentage points: smaller changes in relative strength count as Steady
SPREAD_BAND = 0.20         # percentage points (20 bp) of high-yield spread change over 1 month
CACHE_TTL = 15 * 60
MAX_WATCH_ROWS = 10        # watchlist leaders and laggards shown (all holdings are shown)


def _ret(s: pd.Series, start: int, end: int = 0) -> Optional[float]:
    """Return from `start` trading days ago to `end` days ago, in percent."""
    s = s.dropna()
    if len(s) <= start:
        return None
    a, b = s.iloc[-1 - start], s.iloc[-1 - end]
    return (b / a - 1) * 100 if a else None


def _rs(s: pd.Series, bench: pd.Series, start: int, end: int = 0) -> Optional[float]:
    """Relative strength: the stock's return minus the benchmark's, in percentage points."""
    a, b = _ret(s, start, end), _ret(bench, start, end)
    return a - b if a is not None and b is not None else None


def _p(x: Optional[float], unit: str = "%") -> str:
    return "N/A" if x is None or pd.isna(x) else f"{x:+.1f}{unit}"


def _trend(last: Optional[float], prior: Optional[float]) -> str:
    if last is None or prior is None:
        return "N/A"
    return "Improving" if last - prior > TREND_BAND else "Deteriorating" if prior - last > TREND_BAND else "Steady"


def _above(s: pd.Series, n: int) -> Optional[bool]:
    s = s.dropna()
    return None if len(s) < n else bool(s.iloc[-1] > s.tail(n).mean())


def _fred(series: str) -> Optional[pd.Series]:
    start = (date.today() - timedelta(days=400)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "portfolio-crew"}),
                                    timeout=15) as r:
            rows = list(csv.reader(io.StringIO(r.read().decode())))
    except Exception as e:  # noqa: BLE001 - the ETF ratios still give a read
        log.warning("FRED %s unavailable: %s", series, e)
        return None
    vals = {d: float(v) for d, v in rows[1:] if v not in ("", ".")}
    return pd.Series(vals, dtype=float).sort_index() if vals else None


def _breadth(c: pd.DataFrame, universe: list[str]) -> list[str]:
    out = ["### Market breadth", "| Measure | 1 month | 3 months |", "|---|---|---|"]
    pairs = (("Equal-weight S&P 500 (RSP) minus SPY", "RSP", "SPY"),
             ("Equal-weight Nasdaq-100 (QQQE) minus QQQ", "QQQE", "QQQ"),
             ("Magnificent Seven (MAGS) minus RSP", "MAGS", "RSP"),
             ("Small caps (IWM) minus SPY", "IWM", "SPY"))
    gaps = {}
    for label, a, b in pairs:
        if a in c and b in c:
            gaps[a + b] = (_rs(c[a], c[b], M1), _rs(c[a], c[b], M3))
            out.append(f"| {label} | {_p(gaps[a + b][0], ' pts')} | {_p(gaps[a + b][1], ' pts')} |")
    sec = [s for s in SECTORS if s in c]
    a50 = sum(bool(_above(c[s], 50)) for s in sec)
    a200 = sum(bool(_above(c[s], 200)) for s in sec)
    out.append(f"| Sector ETFs above their 50-day / 200-day average | {a50} of {len(sec)} | {a200} of {len(sec)} |")
    names = [s for s in universe if s in c]
    if names:
        u50 = [_above(c[s], 50) for s in names]
        u200 = [_above(c[s], 200) for s in names]
        pct = lambda xs: f"{sum(bool(x) for x in xs) / max(1, sum(x is not None for x in xs)) * 100:.0f}%"
        out.append(f"| This run's {len(names)} holdings and watchlist names above their 50-day / 200-day average "
                   f"(not the whole market) | {pct(u50)} | {pct(u200)} |")

    rsp3 = (gaps.get("RSPSPY") or (None, None))[1]
    mags3 = (gaps.get("MAGSRSP") or (None, None))[1]
    if rsp3 is None:
        read = "N/A (unavailable)"
    elif rsp3 < -1 and (mags3 or 0) > 3:
        read = "Narrow: mega caps are carrying the index while the average stock lags"
    elif rsp3 >= -1 and a50 >= 7:
        read = "Broad: the average stock is keeping up and most sectors are above their 50-day average"
    else:
        read = "Mixed"
    out.append(f"\nBreadth read: **{read}**. Rule: Narrow when RSP trails SPY by more than 1 pt over 3 months "
               "and MAGS beats RSP by more than 3 pts; Broad when RSP is within 1 pt of SPY or ahead and at least "
               "7 of 11 sector ETFs are above their 50-day average; otherwise Mixed.")
    return out


def _credit(c: pd.DataFrame) -> tuple[list[str], bool]:
    out = ["### Credit conditions", "| Measure | Latest | 1 month ago | 3 months ago | 1-month change | 52-week range |",
           "|---|---|---|---|---|---|"]
    hy_change, fred_ok = None, True
    for sid, label in SPREADS.items():
        s = _fred(sid)
        if s is None or len(s) < M3 + 1:
            fred_ok = False
            out.append(f"| {label} | N/A (FRED unavailable) | | | | |")
            continue
        last, m1, m3 = s.iloc[-1], s.iloc[-1 - M1], s.iloc[-1 - M3]
        yr = s.tail(252)
        if sid == "BAMLH0A0HYM2":
            hy_change = last - m1
        out.append(f"| {label}, as of {s.index[-1]} | {last:.2f}% | {m1:.2f}% | {m3:.2f}% | "
                   f"{(last - m1) * 100:+.0f} bp | {yr.min():.2f}% – {yr.max():.2f}% |")
    for label, a in (("High-yield bonds vs Treasuries (HYG/IEF), price ratio", "HYG"),
                     ("Investment-grade bonds vs Treasuries (LQD/IEF), price ratio", "LQD")):
        if a in c and "IEF" in c:
            out.append(f"| {label} | 1m {_p(_rs(c[a], c['IEF'], M1), ' pts')} | | 3m {_p(_rs(c[a], c['IEF'], M3), ' pts')} "
                       "| falling = spreads widening | |")
    if hy_change is not None:
        read = ("Widening: risk appetite is weakening" if hy_change > SPREAD_BAND
                else "Tightening: risk appetite is improving" if hy_change < -SPREAD_BAND else "Stable")
    else:
        proxy = _rs(c["HYG"], c["IEF"], M1) if "HYG" in c and "IEF" in c else None
        read = ("N/A (unavailable)" if proxy is None else
                "Widening (from HYG/IEF; FRED unavailable)" if proxy < -1 else
                "Tightening (from HYG/IEF; FRED unavailable)" if proxy > 1 else "Stable (from HYG/IEF; FRED unavailable)")
    out.append(f"\nCredit read: **{read}**. Rule: high-yield spread up more than {SPREAD_BAND * 100:.0f} bp in a month "
               f"= Widening, down more than {SPREAD_BAND * 100:.0f} bp = Tightening. Spreads that widen while stocks "
               "hold up are an early warning; say so if you see it.")
    return out, fred_ok


def _leadership_rows(c: pd.DataFrame, names: list[str], label=lambda s: s) -> list[tuple]:
    rows = []
    for s in names:
        if s not in c or "SPY" not in c:
            continue
        last, prior, m3 = _rs(c[s], c["SPY"], M1), _rs(c[s], c["SPY"], 2 * M1, M1), _rs(c[s], c["SPY"], M3)
        rows.append((label(s), last, prior, m3, _trend(last, prior)))
    return sorted(rows, key=lambda r: -1e9 if r[3] is None else r[3], reverse=True)


def _table(rows: list[tuple], first: str) -> list[str]:
    out = [f"| {first} | RS last month | RS month before | RS 3 months | Leadership |", "|---|---|---|---|---|"]
    return out + [f"| {r[0]} | {_p(r[1], ' pts')} | {_p(r[2], ' pts')} | {_p(r[3], ' pts')} | {r[4]} |" for r in rows]


def _leadership(c: pd.DataFrame, held: list[str], watched: list[str]) -> list[str]:
    out = ["### Relative leadership vs SPY",
           f"RS = return minus SPY's return, in percentage points. Leadership compares the last month with the month "
           f"before: Improving or Deteriorating when they differ by more than {TREND_BAND} pts, else Steady.",
           "", "**Sectors** (sorted by 3-month RS)"]
    out += _table(_leadership_rows(c, list(SECTORS), lambda s: f"{s} {SECTORS[s]}"), "Sector ETF")
    out += ["", "**Holdings**"] + _table(_leadership_rows(c, held), "Symbol")
    w = _leadership_rows(c, watched)
    if not w:
        return out + ["", "**Watchlist**: None."]
    if len(w) > 2 * MAX_WATCH_ROWS:
        out += ["", f"**Watchlist: top and bottom {MAX_WATCH_ROWS} of {len(w)} by 3-month RS**"]
        w = w[:MAX_WATCH_ROWS] + w[-MAX_WATCH_ROWS:]
    else:
        out += ["", "**Watchlist**"]
    return out + _table(w, "Symbol")


def _fetch(held: list[str], watched: list[str]) -> str:
    import yfinance as yf
    tickers = sorted(set(BREADTH + CREDIT + tuple(SECTORS) + tuple(held) + tuple(watched)))
    df = yf.download(tickers, period="14mo", interval="1d", auto_adjust=True, progress=False, threads=True)
    if df is None or df.empty:
        return "ERROR fetching market internals: price data unavailable from Yahoo. Report breadth, credit and leadership as N/A (unavailable)."
    c = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": tickers[0]})
    c = c.dropna(axis=1, how="all")
    if "SPY" not in c:
        return "ERROR fetching market internals: price data unavailable for SPY. Report breadth, credit and leadership as N/A (unavailable)."
    credit, fred_ok = _credit(c)
    lines = ["## Market internals",
             f"Prices as of {c.index[-1]:%Y-%m-%d} (intraday if the market is open). Sources: Yahoo Finance via yfinance; credit spreads from "
             f"FRED (ICE BofA indices). Retrieved {datetime.now():%Y-%m-%d %H:%M}.", ""]
    lines += _breadth(c, held + watched) + [""] + credit + [""] + _leadership(c, held, watched)
    missing = [t for t in tickers if t not in c]
    if missing or not fred_ok:
        lines.append("\nWARNING: " + "; ".join(
            ([f"no prices for {', '.join(missing[:10])}"] if missing else [])
            + ([] if fred_ok else ["credit spreads unavailable from FRED; the credit read uses HYG/IEF"])) + ".")
    return "\n".join(lines)


def build(held: list[str], watched: list[str]) -> str:
    held, watched = [s.upper() for s in held], [s.upper() for s in watched if s.upper() not in held]
    return _md.resilient("market_internals", {"held": held, "watched": watched}, CACHE_TTL,
                         lambda: _fetch(held, watched))


if __name__ == "__main__":
    print(build(sys.argv[1:] or ["AAPL", "MSFT"], []))
