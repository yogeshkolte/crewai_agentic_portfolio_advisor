"""Add retries, a short-lived cache and backup sources to the five tools.

Run from the project root: python3 scripts/harden_tools.py   (needs market_data.py in the root)
Backups: tools/<name>.py.bak. Safe to run twice.
"""
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
tools = root / "tools"

IMPORT = '''
try:  # retries, a short-lived cache and backup sources (market_data.py in the project root)
    import market_data as _md
except ImportError:  # the tool still works without it, just without those extras
    _md = None
'''


def _guarded(fetch_call: str, namespace: str, key: str, ttl: int) -> str:
    return (f"        if _md is None:\n"
            f"            return {fetch_call}\n"
            f"        return _md.resilient(\"{namespace}\", {key}, {ttl}, lambda: {fetch_call})\n")


def patch(name: str, edits: list[tuple[str, str]]) -> None:
    p = tools / f"{name}.py"
    s = old = p.read_text()
    if "import market_data as _md" in s:
        print(f"tools/{name}.py: already done")
        return
    for a, b in edits:
        assert s.count(a) == 1, f"tools/{name}.py: expected text not found: {a[:60]!r}"
        s = s.replace(a, b, 1)
    shutil.copy(p, p.with_name(p.name + ".bak"))
    p.write_text(s)
    print(f"tools/{name}.py: updated")


def add_import(after: str) -> tuple[str, str]:
    return after, after + IMPORT


# --- price_indicators: 15-minute cache ---------------------------------------------------
patch("price_indicators", [
    add_import("from pydantic import BaseModel, Field\n"),
    ('    def _run(self, symbol: str, benchmark: str = "SPY", sector_etf: Optional[str] = None) -> str:\n',
     '    def _run(self, symbol: str, benchmark: str = "SPY", sector_etf: Optional[str] = None) -> str:\n'
     + _guarded("self._fetch(symbol, benchmark, sector_etf)", "price_indicators",
                '{"symbol": symbol.strip().upper(), "benchmark": benchmark, "sector_etf": sector_etf}', 900)
     + '\n    def _fetch(self, symbol: str, benchmark: str = "SPY", sector_etf: Optional[str] = None) -> str:\n'),
])

# --- fundamentals: 12-hour cache, SEC EDGAR when Yahoo has nothing -------------------------
patch("fundamentals", [
    add_import("from pydantic import BaseModel, Field\n"),
    ('    def _run(self, symbol: str) -> str:\n',
     '    def _run(self, symbol: str) -> str:\n'
     '        if _md is None:\n'
     '            return self._fetch(symbol)\n'
     '        out = _md.resilient("fundamentals", {"symbol": symbol.strip().upper()}, 12 * 3600,\n'
     '                            lambda: self._fetch(symbol))\n'
     '        if out.startswith("ERROR"):\n'
     '            # Backup: annual statements from SEC EDGAR (needs SEC_USER_AGENT in .env).\n'
     '            backup = _md.edgar_fundamentals(symbol.strip().upper())\n'
     '            if backup:\n'
     '                return backup + f"\\n\\n(Yahoo Finance error was: {out[:200]})"\n'
     '        return out\n'
     '\n    def _fetch(self, symbol: str) -> str:\n'),
])

# --- options_chain: 15-minute cache --------------------------------------------------------
patch("options_chain", [
    add_import("from pydantic import BaseModel, Field\n"),
    ('''    def _run(
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
''', '''    def _run(
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
'''),
])

# --- portfolio_analytics: 15-minute cache --------------------------------------------------
patch("portfolio_analytics", [
    add_import("from pydantic import BaseModel, Field\n"),
    ('''    def _run(
        self,
        holdings: str,
        cash: float = 0.0,
        sectors: Optional[str] = None,
        max_position_pct: float = 10.0,
        max_sector_pct: float = 30.0,
        min_cash_pct: float = 5.0,
        corr_threshold: float = 0.70,
    ) -> str:
''', '''    def _run(
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
'''),
])

# --- events_calendar: 6-hour cache, Finnhub for earnings when Yahoo fails or has none ------
patch("events_calendar", [
    add_import("from pydantic import BaseModel, Field\n"),
    ('    def _run(self, symbols: str, days_ahead: int = 45) -> str:\n',
     '    def _run(self, symbols: str, days_ahead: int = 45) -> str:\n'
     '        key = {"symbols": ",".join(sorted({s.strip().upper() for s in symbols.replace(";", ",").split(",") if s.strip()})),\n'
     '               "days_ahead": days_ahead}\n'
     + _guarded("self._fetch(symbols, days_ahead)", "events_calendar", "key", 6 * 3600)
     + '\n    def _fetch(self, symbols: str, days_ahead: int = 45) -> str:\n'),
    ('''        if len(errors) == len(syms):
            return ("ERROR: could not fetch event data for any symbol. Report events as N/A (unavailable), "
                    "not as 'none'.\\n" + "\\n".join(f"- {e}" for e in errors))
''', '''        # Backup for earnings: Finnhub (needs FINNHUB_API_KEY), for symbols where Yahoo failed or
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
                    "not as 'none'.\\n" + "\\n".join(f"- {e}" for e in errors))
'''),
    ('''        out = [f"## Events {today} to {end}", f"Source: {SOURCE}, retrieved {now}. All dates YYYY-MM-DD, times ET."]
''', '''        out = [f"## Events {today} to {end}", f"Source: {SOURCE}, retrieved {now}. All dates YYYY-MM-DD, times ET."]
        if backup_used:
            out.append(f"Earnings for {', '.join(backup_used)} checked with the backup source Finnhub "
                       "(Yahoo failed or had none); those dates are marked Estimated (Finnhub).")
'''),
    ('''            out.append(f"WARNING: no data for {', '.join(e.split(':')[0] for e in errors)}; their events are N/A (unavailable), not 'none'.")
''', '''            out.append(f"WARNING: Yahoo returned no data for {', '.join(e.split(':')[0] for e in errors)}; their dividends, "
                       "and their earnings unless listed from Finnhub, are N/A (unavailable), not 'none'.")
'''),
])
