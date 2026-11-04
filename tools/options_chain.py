"""Covered-call candidates and contract quotes for one ticker (Yahoo Finance via yfinance).

Yahoo does not publish greeks, so delta is computed with Black-Scholes from the
contract's implied volatility. When Yahoo's IV is missing or implausible (common
outside market hours), IV is solved from the option's mid price instead.
"""

import math
from datetime import date, datetime
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
DEFAULT_RATE = 0.04


class OptionsChainInput(BaseModel):
    symbol: str = Field(..., description="Underlying ticker, e.g. FCX")
    shares: int = Field(0, description="Shares held; used to size contracts (100 shares each). 0 = just screen")
    min_dte: int = Field(20, description="Minimum days to expiry to include")
    max_dte: int = Field(60, description="Maximum days to expiry to include")
    min_delta: float = Field(0.10, description="Lowest call delta to include")
    max_delta: float = Field(0.40, description="Highest call delta to include")
    strike: Optional[float] = Field(None, description="For an existing call: its strike (use with expiry)")
    expiry: Optional[str] = Field(None, description="For an existing call: its expiry YYYY-MM-DD (use with strike)")


# --- Black-Scholes ------------------------------------------------------------
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(s: float, k: float, t: float, r: float, q: float, sigma: float) -> float:
    if t <= 0 or sigma <= 0:
        return max(0.0, s - k)
    d1 = (math.log(s / k) + (r - q + sigma**2 / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return s * math.exp(-q * t) * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)


def call_delta(s: float, k: float, t: float, r: float, q: float, sigma: float) -> Optional[float]:
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return None
    d1 = (math.log(s / k) + (r - q + sigma**2 / 2) * t) / (sigma * math.sqrt(t))
    return math.exp(-q * t) * _ncdf(d1)


def implied_vol(price: float, s: float, k: float, t: float, r: float, q: float) -> Optional[float]:
    """Bisection; returns None if the price is outside no-arbitrage bounds."""
    if price <= 0 or t <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if not (bs_call(s, k, t, r, q, lo) <= price <= bs_call(s, k, t, r, q, hi)):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_call(s, k, t, r, q, mid) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# --- Data helpers -------------------------------------------------------------
def _risk_free() -> float:
    try:
        irx = yf.Ticker("^IRX").history(period="5d")["Close"].dropna()
        return float(irx.iloc[-1]) / 100 if len(irx) else DEFAULT_RATE
    except Exception:
        return DEFAULT_RATE


def _spot_and_yield(t: yf.Ticker) -> tuple[float, str, float]:
    hist = t.history(period="5d", auto_adjust=False)["Close"].dropna()
    spot = float(hist.iloc[-1])
    asof = hist.index[-1].strftime("%Y-%m-%d")
    q = 0.0
    try:
        rate = t.info.get("dividendRate") or 0
        q = float(rate) / spot if rate else 0.0
    except Exception:
        pass
    return spot, asof, q


def _next_earnings(t: yf.Ticker) -> Optional[date]:
    try:
        cal = t.calendar or {}
        dates = [d for d in cal.get("Earnings Date", []) if d >= date.today()]
        return min(dates) if dates else None
    except Exception:
        return None


def _price(row: pd.Series) -> tuple[Optional[float], str]:
    bid, ask, last = row.get("bid", 0) or 0, row.get("ask", 0) or 0, row.get("lastPrice", 0) or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2, "mid"
    if last > 0:
        return float(last), "last (stale)"
    return None, "none"


def enrich(chain: pd.DataFrame, spot: float, dte: int, r: float, q: float) -> pd.DataFrame:
    """Add mid, IV used, delta, yields. Pure computation; separated for testing."""
    t = dte / 365
    out = []
    for _, row in chain.iterrows():
        k = float(row["strike"])
        price, basis = _price(row)
        iv = row.get("impliedVolatility")
        iv = float(iv) if iv is not None and not pd.isna(iv) else None
        iv_src = "yahoo"
        if (iv is None or iv < 0.03 or iv > 4) and price:
            iv, iv_src = implied_vol(price, spot, k, t, r, q), "solved"
        delta = call_delta(spot, k, t, r, q, iv) if iv else None
        out.append({
            "strike": k, "bid": row.get("bid"), "ask": row.get("ask"), "price": price, "basis": basis,
            "iv": iv, "iv_src": iv_src, "delta": delta,
            "oi": int(row.get("openInterest") or 0), "volume": int(row.get("volume") or 0) if not pd.isna(row.get("volume")) else 0,
            "upside": k / spot - 1,
            "yield": (price / spot) if price else None,
            "annualized": (price / spot) * 365 / dte if price and dte > 0 else None,
        })
    return pd.DataFrame(out)


def _f(x, fmt: str) -> str:
    return "N/A" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, fmt)


class OptionsChainTool(BaseTool):
    name: str = "options_chain"
    description: str = (
        "Covered-call screen for ONE ticker. Lists call contracts within a days-to-expiry "
        "and delta range with bid/ask/mid, IV, delta (Black-Scholes), open interest, "
        "upside to strike, premium yield and annualized yield, and flags expiries that "
        "span the next earnings date. Pass shares to size contracts. Pass strike and "
        "expiry to quote an existing short call (for hold/roll/close decisions)."
    )
    args_schema: type[BaseModel] = OptionsChainInput

    def _run(
        self,
        symbol: str,
        shares: int = 0,
        min_dte: int = 20,
        max_dte: int = 60,
        min_delta: float = 0.10,
        max_delta: float = 0.40,
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
    ) -> str:
        args = dict(symbol=symbol, shares=shares, min_dte=min_dte, max_dte=max_dte, min_delta=min_delta,
                    max_delta=max_delta, strike=strike, expiry=expiry)
        if _md is None:
            return self._fetch(**args)
        return _md.resilient("options_chain", args, 900, lambda: self._fetch(**args))

    def _fetch(
        self,
        symbol: str,
        shares: int = 0,
        min_dte: int = 20,
        max_dte: int = 60,
        min_delta: float = 0.10,
        max_delta: float = 0.40,
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
    ) -> str:
        symbol = symbol.strip().upper()
        try:
            t = yf.Ticker(symbol)
            spot, asof, q = _spot_and_yield(t)
            expiries = list(t.options)
        except Exception as e:
            return f"ERROR fetching options for {symbol}: {e}. Report figures as N/A (unavailable)."
        if not expiries:
            return f"{symbol} has no listed options on Yahoo Finance."

        r = _risk_free()
        today = date.today()
        earnings = _next_earnings(t)
        contracts = shares // 100 if shares else 0
        now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        parts = [
            f"## {symbol} covered-call screen",
            f"Spot {spot:,.2f} (close {asof}). Risk-free {r:.2%}, dividend yield {q:.2%}. "
            f"Next earnings: {earnings or 'N/A (unavailable)'}. "
            f"Shares {shares or 'not given'} -> {contracts} contract(s). Source: {SOURCE}, retrieved {now}.",
        ]

        # Existing contract lookup
        if strike is not None and expiry:
            if expiry not in expiries:
                parts.append(f"\nExisting call {expiry} {strike}: expiry not listed. Available: {', '.join(expiries[:8])}...")
            else:
                dte = (date.fromisoformat(expiry) - today).days
                calls = t.option_chain(expiry).calls
                row = calls[(calls["strike"] - strike).abs() < 1e-6]
                if row.empty:
                    parts.append(f"\nExisting call {expiry} {strike}: strike not found in chain.")
                else:
                    e = enrich(row, spot, max(dte, 1), r, q).iloc[0]
                    intrinsic = max(0.0, spot - strike)
                    extrinsic = (e["price"] - intrinsic) if e["price"] else None
                    parts.append(
                        "\n### Existing call\n| Expiry | DTE | Strike | Price | Basis | Delta | Intrinsic | Extrinsic | ITM? | Spans earnings? |\n|---|---|---|---|---|---|---|---|---|---|\n"
                        f"| {expiry} | {dte} | {strike:g} | {_f(e['price'], '.2f')} | {e['basis']} | {_f(e['delta'], '.2f')} | "
                        f"{intrinsic:.2f} | {_f(extrinsic, '.2f')} | {'Yes' if spot > strike else 'No'} | "
                        f"{'Yes' if earnings and today <= earnings <= date.fromisoformat(expiry) else 'No'} |"
                    )

        # Screen
        rows = []
        for exp in expiries:
            dte = (date.fromisoformat(exp) - today).days
            if not (min_dte <= dte <= max_dte):
                continue
            try:
                calls = t.option_chain(exp).calls
            except Exception:
                continue
            calls = calls[calls["strike"] > spot]  # covered calls: out of the money only
            if calls.empty:
                continue
            df = enrich(calls, spot, dte, r, q)
            df = df[df["delta"].between(min_delta, max_delta)]
            spans = bool(earnings and today <= earnings <= date.fromisoformat(exp))
            for _, e in df.iterrows():
                rows.append((exp, dte, e, spans))

        if not rows:
            parts.append(f"\nNo out-of-the-money calls with delta {min_delta}-{max_delta} and {min_dte}-{max_dte} DTE.")
            return "\n".join(parts)

        parts.append(
            "\n### Candidates\n| Expiry | DTE | Strike | Bid | Ask | Price | Basis | IV | IV src | Delta | OI | Upside to strike | Premium yield | Annualized | Premium total | Spans earnings? |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
        )
        for exp, dte, e, spans in rows:
            total = e["price"] * 100 * contracts if e["price"] and contracts else None
            parts.append(
                f"| {exp} | {dte} | {e['strike']:g} | {_f(e['bid'], '.2f')} | {_f(e['ask'], '.2f')} | {_f(e['price'], '.2f')} | "
                f"{e['basis']} | {_f(e['iv'], '.0%')} | {e['iv_src']} | {_f(e['delta'], '.2f')} | {e['oi']} | "
                f"{e['upside']:+.1%} | {_f(e['yield'], '.2%')} | {_f(e['annualized'], '.1%')} | "
                f"{_f(total, ',.0f')} | {'Yes' if spans else 'No'} |"
            )
        parts.append("\nNotes: Price uses mid when bid and ask are live, otherwise the last trade (stale). Low open interest means wider real-world fills.")
        return "\n".join(parts)
