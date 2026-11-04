"""Price history and technical indicators for one ticker (Yahoo Finance via yfinance).

Indicators are computed in plain pandas: SMA 20/50/200, RSI(14, Wilder), MACD(12,26,9),
ATR(14), 52-week range, support/resistance from recent highs/lows, volume trend,
weekly trend, and relative strength versus a benchmark and an optional sector ETF.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

try:  # retries, a short-lived cache and backup sources (market_data.py in the project root)
    import market_data as _md
except ImportError:  # the tool still works without it, just without those extras
    _md = None

SOURCE = "Yahoo Finance via yfinance"


class PriceIndicatorsInput(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, e.g. FCX")
    benchmark: str = Field("SPY", description="Benchmark for relative strength (default SPY)")
    sector_etf: Optional[str] = Field(None, description="Optional sector ETF for relative strength, e.g. XLB")


def _history(symbol: str, period: str = "2y") -> pd.DataFrame:
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    return df.dropna(subset=["Close"]) if df is not None else pd.DataFrame()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _ret(close: pd.Series, days: int) -> Optional[float]:
    return None if len(close) <= days else close.iloc[-1] / close.iloc[-1 - days] - 1


def _pct(x: Optional[float]) -> str:
    return "N/A" if x is None or pd.isna(x) else f"{x * 100:+.1f}%"


def _num(x: Optional[float], d: int = 2) -> str:
    return "N/A" if x is None or pd.isna(x) else f"{x:,.{d}f}"


def compute_indicators(df: pd.DataFrame) -> dict:
    """Pure computation on an OHLCV frame; separated for testing."""
    c = df["Close"]
    last = c.iloc[-1]
    out: dict = {"last": last, "date": df.index[-1].strftime("%Y-%m-%d")}
    for n in (20, 50, 200):
        sma = c.rolling(n).mean().iloc[-1] if len(c) >= n else None
        out[f"sma{n}"] = sma
        out[f"vs_sma{n}"] = None if sma is None or pd.isna(sma) else last / sma - 1
    out["sma50_slope_20d"] = None
    if len(c) >= 70:
        s50 = c.rolling(50).mean()
        out["sma50_slope_20d"] = s50.iloc[-1] / s50.iloc[-21] - 1
    out["rsi14"] = _rsi(c).iloc[-1]
    line, sig, hist = _macd(c)
    out["macd"], out["macd_signal"], out["macd_hist"] = line.iloc[-1], sig.iloc[-1], hist.iloc[-1]
    out["macd_cross"] = "bullish" if line.iloc[-1] > sig.iloc[-1] else "bearish"
    out["atr14"] = _atr(df).iloc[-1]
    yr = df.tail(252)
    out["high_52w"], out["low_52w"] = yr["High"].max(), yr["Low"].min()
    out["off_high"] = last / out["high_52w"] - 1
    out["support_20d"], out["resistance_20d"] = df["Low"].tail(20).min(), df["High"].tail(20).max()
    out["support_60d"], out["resistance_60d"] = df["Low"].tail(60).min(), df["High"].tail(60).max()
    v = df["Volume"]
    out["vol_20_vs_50"] = v.tail(20).mean() / v.tail(50).mean() - 1 if len(v) >= 50 and v.tail(50).mean() else None
    for label, d in (("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252)):
        out[f"ret_{label}"] = _ret(c, d)
    weekly = c.resample("W-FRI").last().dropna()
    if len(weekly) >= 30:
        w10, w30 = weekly.rolling(10).mean().iloc[-1], weekly.rolling(30).mean().iloc[-1]
        out["weekly_trend"] = "up" if weekly.iloc[-1] > w10 > w30 else "down" if weekly.iloc[-1] < w10 < w30 else "sideways"
    else:
        out["weekly_trend"] = "N/A"
    s50, s200 = out["sma50"], out["sma200"]
    if s50 is not None and s200 is not None and not pd.isna(s200):
        out["daily_trend"] = "up" if last > s50 > s200 else "down" if last < s50 < s200 else "sideways"
    else:
        out["daily_trend"] = "N/A"
    return out


class PriceIndicatorsTool(BaseTool):
    name: str = "price_indicators"
    description: str = (
        "Technical snapshot for ONE ticker: last close and date, daily and weekly trend, "
        "SMA 20/50/200 and distance from each, RSI(14), MACD, ATR(14), 52-week range, "
        "20- and 60-day support/resistance, volume trend, 1/3/6/12-month returns, and "
        "relative strength versus a benchmark (default SPY) and an optional sector ETF. "
        "Call once per symbol."
    )
    args_schema: type[BaseModel] = PriceIndicatorsInput

    def _run(self, symbol: str, benchmark: str = "SPY", sector_etf: Optional[str] = None) -> str:
        if _md is None:
            return self._fetch(symbol, benchmark, sector_etf)
        return _md.resilient("price_indicators", {"symbol": symbol.strip().upper(), "benchmark": benchmark, "sector_etf": sector_etf}, 900, lambda: self._fetch(symbol, benchmark, sector_etf))

    def _fetch(self, symbol: str, benchmark: str = "SPY", sector_etf: Optional[str] = None) -> str:
        symbol = symbol.strip().upper()
        try:
            df = _history(symbol)
        except Exception as e:  # network or Yahoo errors
            return f"ERROR fetching {symbol}: {e}. Report figures as N/A (unavailable)."
        if df.empty or len(df) < 30:
            return f"ERROR: not enough price history for {symbol}. Report figures as N/A (unavailable)."

        k = compute_indicators(df)
        rows = [
            ("Last close", f"{_num(k['last'])} on {k['date']}"),
            ("Daily trend (price vs 50/200 SMA)", k["daily_trend"]),
            ("Weekly trend (10/30-week)", k["weekly_trend"]),
            ("SMA 20 / 50 / 200", f"{_num(k['sma20'])} / {_num(k['sma50'])} / {_num(k['sma200'])}"),
            ("Price vs SMA 20 / 50 / 200", f"{_pct(k['vs_sma20'])} / {_pct(k['vs_sma50'])} / {_pct(k['vs_sma200'])}"),
            ("SMA 50 slope (20 days)", _pct(k["sma50_slope_20d"])),
            ("RSI(14)", _num(k["rsi14"], 1)),
            ("MACD / signal / histogram", f"{_num(k['macd'], 3)} / {_num(k['macd_signal'], 3)} / {_num(k['macd_hist'], 3)} ({k['macd_cross']})"),
            ("ATR(14)", f"{_num(k['atr14'])} ({k['atr14'] / k['last'] * 100:.1f}% of price)"),
            ("52-week high / low", f"{_num(k['high_52w'])} / {_num(k['low_52w'])} (now {_pct(k['off_high'])} from high)"),
            ("Support / resistance (20d)", f"{_num(k['support_20d'])} / {_num(k['resistance_20d'])}"),
            ("Support / resistance (60d)", f"{_num(k['support_60d'])} / {_num(k['resistance_60d'])}"),
            ("Volume 20d vs 50d avg", _pct(k["vol_20_vs_50"])),
            ("Return 1m / 3m / 6m / 12m", " / ".join(_pct(k[f"ret_{p}"]) for p in ("1m", "3m", "6m", "12m"))),
        ]

        for ref in [benchmark] + ([sector_etf] if sector_etf else []):
            ref = ref.strip().upper()
            try:
                rc = _history(ref)["Close"]
                rel = []
                for label, d in (("1m", 21), ("3m", 63), ("6m", 126)):
                    a, b = _ret(df["Close"], d), _ret(rc, d)
                    rel.append(f"{label} {_pct(None if a is None or b is None else a - b)}")
                rows.append((f"Relative strength vs {ref}", ", ".join(rel)))
            except Exception:
                rows.append((f"Relative strength vs {ref}", "N/A (unavailable)"))

        now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        table = "\n".join(f"| {a} | {b} |" for a, b in rows)
        return (
            f"## {symbol} technicals\nData as of {k['date']} close. Source: {SOURCE}, retrieved {now}.\n\n"
            f"| Metric | Value |\n|---|---|\n{table}"
        )
