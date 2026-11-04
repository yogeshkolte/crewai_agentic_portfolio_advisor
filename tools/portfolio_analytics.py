"""Portfolio risk analytics (Yahoo Finance prices via yfinance).

Position and sector weights with limit status, per-position and portfolio beta vs SPY,
volatility, 1-year max drawdown of the current mix, and highly correlated clusters.
"""

import math
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

try:  # retries, a short-lived cache and backup sources (market_data.py in the project root)
    import market_data as _md
except ImportError:  # the tool still works without it, just without those extras
    _md = None

SOURCE = "Yahoo Finance via yfinance"
BENCH = "SPY"


class PortfolioAnalyticsInput(BaseModel):
    holdings: str = Field(..., description="Comma-separated SYMBOL:SHARES pairs, e.g. 'AAPL:200, FCX:300'")
    cash: float = Field(0.0, description="Cash balance in dollars")
    sectors: Optional[str] = Field(None, description="Optional SYMBOL:Sector pairs; looked up if omitted")
    max_position_pct: float = Field(10.0, description="Position limit, % of total portfolio")
    max_sector_pct: float = Field(30.0, description="Sector limit, % of total portfolio")
    min_cash_pct: float = Field(5.0, description="Minimum cash, % of total portfolio")
    corr_threshold: float = Field(0.70, description="Correlation level that groups positions into a cluster")


def _pairs(text: str) -> dict[str, str]:
    out = {}
    for part in text.replace(";", ",").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _status(pct: float, limit: float) -> str:
    return "Breach" if pct > limit else "Warn" if pct > 0.8 * limit else "OK"


def analyze(
    prices: pd.DataFrame,
    shares: dict[str, float],
    cash: float,
    sectors: dict[str, str],
    max_position_pct: float,
    max_sector_pct: float,
    min_cash_pct: float,
    corr_threshold: float = 0.70,
) -> dict:
    """Pure computation on a daily close-price frame that includes SPY; separated for testing."""
    syms = [s for s in shares if s in prices.columns and prices[s].notna().sum() > 20]
    missing = [s for s in shares if s not in syms]
    last = prices[syms].ffill().iloc[-1]
    mv = pd.Series({s: shares[s] * last[s] for s in syms})
    total = mv.sum() + cash
    w = mv / total

    rets = prices[syms + [BENCH]].pct_change().dropna(how="all")
    spy = rets[BENCH]
    pos = []
    for s in syms:
        r = rets[s].dropna()
        aligned = pd.concat([r, spy], axis=1, join="inner").dropna()
        beta = aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / aligned.iloc[:, 1].var() if len(aligned) > 20 else float("nan")
        vol = r.std() * math.sqrt(252)
        pct = w[s] * 100
        pos.append({"symbol": s, "shares": shares[s], "price": last[s], "value": mv[s], "pct": pct,
                    "sector": sectors.get(s, "Unknown"), "beta": beta, "vol": vol,
                    "status": _status(pct, max_position_pct)})
    pos_df = pd.DataFrame(pos).sort_values("value", ascending=False)

    sec = pos_df.groupby("sector")["pct"].sum().sort_values(ascending=False)
    sec_rows = [{"sector": k, "pct": v, "status": _status(v, max_sector_pct)} for k, v in sec.items()]

    port_beta = float((pos_df["pct"] / 100 * pos_df["beta"].fillna(1.0)).sum())
    eq_w = (mv / mv.sum()).reindex(syms)
    cov = rets[syms].cov() * 252
    eq_vol = math.sqrt(float(eq_w.values @ cov.values @ eq_w.values))
    port_vol = eq_vol * mv.sum() / total

    # Max drawdown of today's mix over the last year (equity sleeve, buy-and-hold weights)
    px = prices[syms].ffill().dropna()
    if not px.empty:
        sleeve = (px * pd.Series(shares)[syms]).sum(axis=1)
        dd = sleeve / sleeve.cummax() - 1
        max_dd = float(dd.min())
    else:
        max_dd = float("nan")

    # Correlation clusters over the last 90 trading days
    corr = rets[syms].tail(90).corr()
    parent = {s: s for s in syms}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    high_pairs = []
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            c = corr.loc[a, b]
            if not np.isnan(c) and c >= corr_threshold:
                high_pairs.append((a, b, c))
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for s in syms:
        groups.setdefault(find(s), []).append(s)
    clusters = [g for g in groups.values() if len(g) > 1]

    cash_pct = cash / total * 100 if total else 0.0
    return {
        "asof": prices.index[-1].strftime("%Y-%m-%d"), "total": total, "cash": cash, "cash_pct": cash_pct,
        "cash_status": "Breach" if cash_pct < min_cash_pct else "OK", "positions": pos_df, "sectors": sec_rows,
        "port_beta": port_beta, "port_vol": port_vol, "max_dd": max_dd, "high_pairs": sorted(high_pairs, key=lambda x: -x[2]),
        "clusters": [(g, float(pos_df[pos_df["symbol"].isin(g)]["pct"].sum())) for g in clusters], "missing": missing,
    }


def _lookup_sector(sym: str) -> str:
    try:
        i = yf.Ticker(sym).info or {}
        if i.get("quoteType") in ("ETF", "MUTUALFUND"):
            return f"ETF: {i.get('category') or i.get('longName') or sym}"
        return i.get("sector") or "Unknown"
    except Exception:
        return "Unknown"


class PortfolioAnalyticsTool(BaseTool):
    name: str = "portfolio_analytics"
    description: str = (
        "Risk dashboard for the whole portfolio. Input holdings as 'SYM:SHARES' pairs plus cash. "
        "Returns market value and weight per position with limit status (OK/Warn/Breach), sector "
        "weights with limit status, cash vs minimum, per-position beta and volatility, portfolio "
        "beta and volatility vs SPY, 1-year max drawdown of the current mix, and clusters of highly "
        "correlated positions (90-day)."
    )
    args_schema: type[BaseModel] = PortfolioAnalyticsInput

    def _run(
        self,
        holdings: str,
        cash: float = 0.0,
        sectors: Optional[str] = None,
        max_position_pct: float = 10.0,
        max_sector_pct: float = 30.0,
        min_cash_pct: float = 5.0,
        corr_threshold: float = 0.70,
    ) -> str:
        args = dict(holdings=holdings, cash=cash, sectors=sectors, max_position_pct=max_position_pct,
                    max_sector_pct=max_sector_pct, min_cash_pct=min_cash_pct, corr_threshold=corr_threshold)
        if _md is None:
            return self._fetch(**args)
        return _md.resilient("portfolio_analytics", args, 900, lambda: self._fetch(**args))

    def _fetch(
        self,
        holdings: str,
        cash: float = 0.0,
        sectors: Optional[str] = None,
        max_position_pct: float = 10.0,
        max_sector_pct: float = 30.0,
        min_cash_pct: float = 5.0,
        corr_threshold: float = 0.70,
    ) -> str:
        try:
            shares = {k: float(v) for k, v in _pairs(holdings).items()}
        except ValueError:
            return "ERROR: holdings must look like 'AAPL:200, FCX:300'."
        if not shares:
            return "ERROR: no holdings given. Use 'SYM:SHARES' pairs."
        try:
            data = yf.download(list(shares) + [BENCH], period="1y", interval="1d",
                               auto_adjust=True, progress=False, group_by="column")
            prices = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
        except Exception as e:
            return f"ERROR downloading prices: {e}. Report risk figures as N/A (unavailable)."
        prices = prices.dropna(how="all")
        if prices.empty or BENCH not in prices.columns or prices[BENCH].notna().sum() < 20:
            return "ERROR: price data unavailable (download returned no data). Report risk figures as N/A (unavailable)."
        if not any(s in prices.columns and prices[s].notna().sum() > 20 for s in shares):
            return "ERROR: no price history for any holding. Report risk figures as N/A (unavailable)."

        sec_map = _pairs(sectors) if sectors else {}
        for s in shares:
            sec_map.setdefault(s, _lookup_sector(s))

        a = analyze(prices, shares, cash, sec_map, max_position_pct, max_sector_pct, min_cash_pct, corr_threshold)
        now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        out = [
            "## Portfolio risk dashboard",
            f"Prices as of {a['asof']} close. Source: {SOURCE}, retrieved {now}.",
            f"Total value {a['total']:,.0f} | Cash {a['cash']:,.0f} ({a['cash_pct']:.1f}%, min {min_cash_pct:g}%: {a['cash_status']}) | "
            f"Portfolio beta {a['port_beta']:.2f} | Annualized volatility {a['port_vol']:.1%} | 1-year max drawdown of current mix {a['max_dd']:.1%}",
            f"\n### Positions (limit {max_position_pct:g}%)",
            "| Symbol | Shares | Price | Value | Weight | Status | Sector | Beta | Volatility |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for _, p in a["positions"].iterrows():
            beta = "N/A" if pd.isna(p["beta"]) else f"{p['beta']:.2f}"
            out.append(f"| {p['symbol']} | {p['shares']:,.0f} | {p['price']:,.2f} | {p['value']:,.0f} | {p['pct']:.1f}% | "
                       f"{p['status']} | {p['sector']} | {beta} | {p['vol']:.0%} |")
        out += [f"\n### Sectors (limit {max_sector_pct:g}%)", "| Sector | Weight | Status |", "|---|---|---|"]
        out += [f"| {s['sector']} | {s['pct']:.1f}% | {s['status']} |" for s in a["sectors"]]
        out.append(f"\n### Correlation (90-day, threshold {corr_threshold:.2f})")
        if a["clusters"]:
            out += [f"- Cluster {', '.join(g)}: combined weight {pct:.1f}%" for g, pct in a["clusters"]]
            out += [f"- {x} / {y}: {c:.2f}" for x, y, c in a["high_pairs"][:10]]
        else:
            out.append("No pairs above the threshold.")
        if a["missing"]:
            out.append(f"\nNo price data for: {', '.join(a['missing'])}. Their risk checks are Unverified.")
        return "\n".join(out)
