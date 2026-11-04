"""Fundamentals, valuation and analyst estimates for one ticker (Yahoo Finance via yfinance)."""

import math
from datetime import datetime
from typing import Any, Optional
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


class FundamentalsInput(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, e.g. MSFT")
    capital: bool = Field(False, description="Also return returns on capital (ROIC, incremental ROIC, ROE), "
                          "capital allocation (capex, SBC, dilution, buybacks, dividends, acquisitions) and "
                          "debt/dividend safety. Use it for holdings.")


def _ok(x: Any) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _money(x: Any) -> str:
    if not _ok(x):
        return "N/A"
    x = float(x)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"{x / div:,.2f}{unit}"
    return f"{x:,.0f}"


def _ratio(x: Any, d: int = 1) -> str:
    return f"{float(x):,.{d}f}" if _ok(x) else "N/A"


def _pct(x: Any) -> str:
    return f"{float(x) * 100:+.1f}%" if _ok(x) else "N/A"


def _row(df: Optional[pd.DataFrame], *names: str) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def _annual_table(t: yf.Ticker) -> str:
    try:
        inc, cf = t.financials, t.cashflow
    except Exception:
        return "Annual financials: N/A (unavailable)"
    rev = _row(inc, "Total Revenue")
    op = _row(inc, "Operating Income")
    ni = _row(inc, "Net Income")
    fcf = _row(cf, "Free Cash Flow")
    if rev is None:
        return "Annual financials: N/A (unavailable)"
    cols = list(rev.index)[:4]
    lines = ["| Fiscal year end | Revenue | Rev growth | Operating margin | Net income | Free cash flow |", "|---|---|---|---|---|---|"]
    for i, c in enumerate(cols):
        r = rev.get(c)
        prev = rev.get(cols[i + 1]) if i + 1 < len(cols) else None
        growth = (r / prev - 1) if _ok(r) and _ok(prev) and prev else None
        opm = (op.get(c) / r) if op is not None and _ok(op.get(c)) and _ok(r) and r else None
        lines.append(
            f"| {pd.Timestamp(c).strftime('%Y-%m-%d')} | {_money(r)} | {_pct(growth)} | {_pct(opm)} | "
            f"{_money(ni.get(c) if ni is not None else None)} | {_money(fcf.get(c) if fcf is not None else None)} |"
        )
    return "\n".join(lines)


def _val(s: Optional[pd.Series], c: Any) -> Optional[float]:
    v = s.get(c) if s is not None else None
    return float(v) if _ok(v) else None


def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return a / b if a is not None and b else None


def _capital_section(t: yf.Ticker) -> str:
    """Returns on capital, capital allocation, debt and dividend safety from the annual statements.

    ROIC = NOPAT / average invested capital, NOPAT = EBIT x (1 - tax rate) (tax rate from the
    statements, clamped 0-35%, 21% if unknown). Incremental ROIC = change in NOPAT / change in
    invested capital from the oldest to the latest year: the return on the capital added. ROE is
    marked not meaningful when equity is zero or negative (buybacks or losses).
    """
    try:
        inc, bs, cf = t.financials, t.balance_sheet, t.cashflow
    except Exception:
        return "Returns on capital and capital allocation: N/A (unavailable)"
    rev, ebit = _row(inc, "Total Revenue"), _row(inc, "EBIT", "Operating Income")
    ni, pretax, tax = _row(inc, "Net Income"), _row(inc, "Pretax Income"), _row(inc, "Tax Provision")
    interest, shares = _row(inc, "Interest Expense"), _row(inc, "Diluted Average Shares")
    ic, eq = _row(bs, "Invested Capital"), _row(bs, "Stockholders Equity")
    debt = _row(bs, "Total Debt")
    cur_debt = _row(bs, "Current Debt And Capital Lease Obligation", "Current Debt")
    cash = _row(bs, "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")
    fcf, capex = _row(cf, "Free Cash Flow"), _row(cf, "Capital Expenditure")
    sbc, da = _row(cf, "Stock Based Compensation"), _row(cf, "Depreciation And Amortization")
    buyback = _row(cf, "Repurchase Of Capital Stock")
    divs = _row(cf, "Cash Dividends Paid", "Common Stock Dividend Paid")
    acq = _row(cf, "Net Business Purchase And Sale", "Purchase Of Business")
    net_debt_iss = _row(cf, "Net Issuance Payments Of Debt")
    if rev is None or ebit is None:
        return "Returns on capital and capital allocation: N/A (unavailable)"

    cols = [c for c in list(rev.index)[:4] if _ok(rev.get(c))]  # newest first
    if not cols:
        return "Returns on capital and capital allocation: N/A (unavailable)"

    def nopat(c):
        e = _val(ebit, c)
        if e is None:
            return None
        rate = _div(_val(tax, c), _val(pretax, c))
        rate = 0.21 if rate is None else min(max(rate, 0.0), 0.35)
        return e * (1 - rate)

    def avg(s, i):  # average of this and the prior year-end balance, else the year-end one
        now = _val(s, cols[i])
        prev = _val(s, cols[i + 1]) if i + 1 < len(cols) else None
        return (now + prev) / 2 if now is not None and prev is not None else now

    def neg(x):  # cash-flow outflows are negative; show them as positive amounts
        return (-x or 0.0) if x is not None else None

    ret = ["### Returns on capital (annual)",
           "| Fiscal year end | NOPAT | Invested capital | ROIC | ROE |", "|---|---|---|---|---|"]
    alloc = ["### Capital allocation (annual cash flow)",
             "| Fiscal year end | Capex | Capex % revenue | Capex / D&A | SBC | SBC % revenue | SBC % FCF "
             "| Diluted shares | Share count change | Buybacks | Dividends paid | Acquisitions net of sales (negative = net sales) | Net debt issued |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(cols):
        fy = pd.Timestamp(c).strftime("%Y-%m-%d")
        avg_ic, avg_eq = avg(ic, i), avg(eq, i)
        roe = "not meaningful (equity ≤ 0)" if avg_eq is not None and avg_eq <= 0 else _pct(_div(_val(ni, c), avg_eq))
        ret.append(f"| {fy} | {_money(nopat(c))} | {_money(_val(ic, c))} | "
                   f"{_pct(_div(nopat(c), avg_ic)) if avg_ic and avg_ic > 0 else 'N/A'} | {roe} |")
        r, cx, s, f = _val(rev, c), neg(_val(capex, c)), _val(sbc, c), _val(fcf, c)
        sh = _val(shares, c)
        prev_sh = _val(shares, cols[i + 1]) if i + 1 < len(cols) else None
        alloc.append(
            f"| {fy} | {_money(cx)} | {_pct(_div(cx, r))} | {_ratio(_div(cx, _val(da, c)), 2)} | {_money(s)} | "
            f"{_pct(_div(s, r))} | {_pct(_div(s, f)) if f and f > 0 else 'N/A (FCF ≤ 0)'} | {_money(sh)} | "
            f"{_pct(_div(sh, prev_sh) - 1) if sh and prev_sh else 'N/A'} | {_money(neg(_val(buyback, c)))} | "
            f"{_money(neg(_val(divs, c)))} | {_money(neg(_val(acq, c)))} | {_money(_val(net_debt_iss, c))} |")

    first, last = cols[-1], cols[0]
    d_nopat = (nopat(last) - nopat(first)) if nopat(last) is not None and nopat(first) is not None else None
    d_ic = (_val(ic, last) - _val(ic, first)) if _val(ic, last) is not None and _val(ic, first) is not None else None
    if d_nopat is None or d_ic is None:
        inc_roic = "N/A (unavailable)"
    elif d_ic <= 0:
        inc_roic = (f"not meaningful: invested capital fell by {_money(-d_ic)} while NOPAT changed by "
                    f"{_money(d_nopat)} (capital returned, not added)")
    else:
        inc_roic = f"{_pct(d_nopat / d_ic)} (NOPAT {_money(d_nopat)} more on {_money(d_ic)} more invested capital)"
    ret.append(f"\nIncremental ROIC {pd.Timestamp(first):%Y}→{pd.Timestamp(last):%Y}: {inc_roic}")

    e, it = _val(ebit, last), _val(interest, last)
    d, cd, cs = _val(debt, last), _val(cur_debt, last), _val(cash, last)
    f, dv, n = _val(fcf, last), neg(_val(divs, last)), _val(ni, last)
    safety = [
        f"### Debt and dividend safety (fiscal year {pd.Timestamp(last):%Y})", "| Measure | Value |", "|---|---|",
        f"| Total debt / debt due within 12 months / cash and short-term investments | {_money(d)} / {_money(cd)} / {_money(cs)} |",
        f"| Debt due within 12 months covered by cash | {_ratio(_div(cs, cd), 1) + 'x' if cd else 'no debt due'} |",
        f"| Interest coverage (EBIT / interest expense) | {_ratio(_div(e, it), 1) + 'x' if it else 'no interest expense'} |",
        f"| Implied interest rate on debt | {_pct(_div(it, d)) if it and d else 'N/A'} |",
        f"| Dividends / free cash flow | {'no dividend' if not dv else _pct(_div(dv, f)) if f and f > 0 else 'N/A (FCF ≤ 0)'} |",
        f"| Dividends / net income | {'no dividend' if not dv else _pct(_div(dv, n)) if n and n > 0 else 'N/A (loss)'} |",
        "| Debt maturities beyond 12 months | Not in Yahoo data: see the debt note in the latest 10-K (sec.gov) |",
    ]
    return "\n".join(ret) + "\n\n" + "\n".join(alloc) + "\n\n" + "\n".join(safety)


def _estimates(t: yf.Ticker) -> str:
    out = []
    for label, attr in (("EPS estimates", "earnings_estimate"), ("Revenue estimates", "revenue_estimate")):
        try:
            df = getattr(t, attr)
            if df is not None and not df.empty:
                cols = [c for c in ("avg", "low", "high", "numberOfAnalysts", "growth") if c in df.columns]
                lines = [f"**{label}**", "| Period | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
                for period, r in df[cols].iterrows():
                    cells = [(_money(r[c]) if label.startswith("Revenue") and c in ("avg", "low", "high")
                              else _pct(r[c]) if c == "growth" else _ratio(r[c], 0) if c == "numberOfAnalysts" else _ratio(r[c], 2)) for c in cols]
                    lines.append(f"| {period} | " + " | ".join(cells) + " |")
                out.append("\n".join(lines))
        except Exception:
            pass
    try:
        trend = t.eps_trend
        if trend is not None and not trend.empty:
            cols = [c for c in ("current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo") if c in trend.columns]
            lines = ["**EPS estimate trend (revisions)**", "| Period | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
            for period, r in trend[cols].iterrows():
                lines.append(f"| {period} | " + " | ".join(_ratio(r[c], 2) for c in cols) + " |")
            out.append("\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(out) if out else "Analyst estimates: N/A (unavailable)"


class FundamentalsTool(BaseTool):
    name: str = "fundamentals"
    description: str = (
        "Fundamentals for ONE ticker: sector, market cap, valuation (P/E trailing and "
        "forward, PEG, EV/EBITDA, P/S, P/B, FCF yield), margins, growth, balance sheet "
        "(cash, debt, net debt), returns, dividend, analyst targets, 4 years of annual "
        "revenue/margin/net income/free cash flow, and EPS/revenue estimates with revision trend. "
        "With capital=true also ROIC and incremental ROIC, ROE, capex, stock-based compensation, "
        "share dilution, buybacks, dividends, acquisitions, debt due within 12 months, interest "
        "coverage and dividend payout versus free cash flow."
    )
    args_schema: type[BaseModel] = FundamentalsInput

    def _run(self, symbol: str, capital: bool = False) -> str:
        if _md is None:
            return self._fetch(symbol, capital)
        out = _md.resilient("fundamentals", {"symbol": symbol.strip().upper(), "capital": bool(capital)},
                            12 * 3600, lambda: self._fetch(symbol, capital))
        if out.startswith("ERROR"):
            # Backup: annual statements from SEC EDGAR (needs SEC_USER_AGENT in .env).
            backup = _md.edgar_fundamentals(symbol.strip().upper())
            if backup:
                return backup + f"\n\n(Yahoo Finance error was: {out[:200]})"
        return out

    def _fetch(self, symbol: str, capital: bool = False) -> str:
        symbol = symbol.strip().upper()
        try:
            t = yf.Ticker(symbol)
            i = t.info or {}
        except Exception as e:
            return f"ERROR fetching fundamentals for {symbol}: {e}. Report figures as N/A (unavailable)."
        if not i or not (i.get("longName") or i.get("shortName")):
            return f"ERROR: no fundamentals found for {symbol}. Report figures as N/A (unavailable)."

        cash, debt = i.get("totalCash"), i.get("totalDebt")
        net_debt = (debt - cash) if _ok(cash) and _ok(debt) else None
        mcap, fcf = i.get("marketCap"), i.get("freeCashflow")
        fcf_yield = fcf / mcap if _ok(fcf) and _ok(mcap) and mcap else None
        price = i.get("currentPrice") or i.get("regularMarketPrice")
        div_rate = i.get("dividendRate")
        div_yield = div_rate / price if _ok(div_rate) and _ok(price) and price else None
        upside = i.get("targetMeanPrice") / price - 1 if _ok(i.get("targetMeanPrice")) and _ok(price) and price else None

        rows = [
            ("Name / sector / industry", f"{i.get('longName') or i.get('shortName')} / {i.get('sector', 'N/A')} / {i.get('industry', 'N/A')}"),
            ("Price / market cap / EV", f"{_ratio(price, 2)} / {_money(mcap)} / {_money(i.get('enterpriseValue'))}"),
            ("P/E trailing / forward", f"{_ratio(i.get('trailingPE'))} / {_ratio(i.get('forwardPE'))}"),
            ("PEG / EV-EBITDA / P-S / P-B", f"{_ratio(i.get('trailingPegRatio') or i.get('pegRatio'), 2)} / {_ratio(i.get('enterpriseToEbitda'))} / {_ratio(i.get('priceToSalesTrailing12Months'))} / {_ratio(i.get('priceToBook'))}"),
            ("FCF (TTM) / FCF yield", f"{_money(fcf)} / {_pct(fcf_yield)}"),
            ("Gross / operating / net margin", f"{_pct(i.get('grossMargins'))} / {_pct(i.get('operatingMargins'))} / {_pct(i.get('profitMargins'))}"),
            ("Revenue growth (YoY qtr) / earnings growth", f"{_pct(i.get('revenueGrowth'))} / {_pct(i.get('earningsGrowth'))}"),
            ("Cash / debt / net debt", f"{_money(cash)} / {_money(debt)} / {_money(net_debt)}"),
            ("Debt-to-equity / current ratio", f"{_ratio(i.get('debtToEquity'))} / {_ratio(i.get('currentRatio'), 2)}"),
            ("ROE / ROA", f"{_pct(i.get('returnOnEquity'))} / {_pct(i.get('returnOnAssets'))}"),
            ("Dividend rate / yield / payout", f"{_ratio(div_rate, 2)} / {_pct(div_yield)} / {_pct(i.get('payoutRatio'))}"),
            ("Beta (5y monthly)", _ratio(i.get("beta"), 2)),
            ("Analyst target mean / low / high", f"{_ratio(i.get('targetMeanPrice'), 2)} / {_ratio(i.get('targetLowPrice'), 2)} / {_ratio(i.get('targetHighPrice'), 2)} (mean implies {_pct(upside)})"),
            ("Consensus / # analysts", f"{i.get('recommendationKey', 'N/A')} / {i.get('numberOfAnalystOpinions', 'N/A')}"),
        ]
        now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        snap = "\n".join(f"| {a} | {b} |" for a, b in rows)
        return (
            f"## {symbol} fundamentals\nSource: {SOURCE}, retrieved {now}. TTM figures as reported by Yahoo.\n\n"
            f"| Metric | Value |\n|---|---|\n{snap}\n\n### Annual history\n{_annual_table(t)}\n\n### Estimates\n{_estimates(t)}"
            + (f"\n\n{_capital_section(t)}" if capital else "")
        )
