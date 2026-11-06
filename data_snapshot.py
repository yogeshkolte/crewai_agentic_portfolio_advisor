"""Fetch all market data once, before any agent runs.

WHY
    Previously each agent fetched its own data with its tools. If an agent failed, the data
    it had gathered was lost to every task after it, and agents sometimes saw slightly
    different prices. Now the input check (guardrails.validate_inputs) calls build() once at
    the start of the run. The results go to every task as inputs, so no task depends on
    another one to get its numbers. Agents keep their tools to refresh anything missing.

WHAT IS FETCHED (the same tools the agents use, so the same retries, cache and backups apply)
    events_calendar      all holdings and watchlist symbols    -> {data_events}
    price_indicators     SPY, QQQ, IWM + 11 sector ETFs         -> {data_market}
    market_internals     breadth, credit spreads, leadership     -> appended to {data_market}
    price_indicators     every holding and watchlist symbol     -> {data_prices}
    fundamentals         holdings / watchlist                   -> {data_fundamentals_holdings} / {data_fundamentals_watchlist}
    portfolio_analytics  holdings with your risk limits         -> {data_portfolio}
    track_record         past plans/screens scored vs SPY       -> {data_track_record} / {data_track_record_scores}
    a status table of what worked and what didn't              -> {data_status}

    Everything is also saved as output/<YYYYMMDD>_00_data_snapshot.md, so you can check any
    figure an agent used against the raw data.

    SNAPSHOT_WORKERS (default 3) sets how many fetches run at once; more is faster but more
    likely to hit Yahoo's rate limit.
"""
import importlib.util
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("portfolio_crew.data")

SLICES = ("data_events", "data_market", "data_prices", "data_fundamentals_holdings",
          "data_fundamentals_watchlist", "data_portfolio", "data_track_record", "data_track_record_scores",
          "data_status")
# Broad market, then the 11 SPDR sector ETFs (for the market briefing's sector rotation table).
BENCHMARKS = ("SPY", "QQQ", "IWM",
              "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC")


def _tool(name: str):
    """Load tools/<name>.py the way CrewAI does and return an instance of its tool class."""
    from crewai.tools import BaseTool

    spec = importlib.util.spec_from_file_location(f"snapshot_tools.{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for attr in dir(module):
        obj = getattr(module, attr)
        if isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool:
            return obj()
    raise RuntimeError(f"no tool class in tools/{name}.py")


def _status(out: str) -> str:
    first = out.strip().splitlines()[0] if out.strip() else "empty result"
    if out.startswith("ERROR"):
        return "FAILED: " + first[:140]
    if "backup source" in out:
        return "OK (backup source used)"
    if "WARNING" in out:
        warn = next(l for l in out.splitlines() if "WARNING" in l)
        return "PARTIAL: " + warn[:140]
    return "OK"


# Diversification score per watchlist name, computed by portfolio_fit() (read by guardrails.watchlist_scores).
FIT: dict[str, int] = {}


def _sector_industry(fundamentals: str) -> tuple[str, str]:
    m = re.search(r"\| Name / sector / industry \| (.*) / (.*?) / (.*?) \|", fundamentals or "")
    return (m.group(2).strip(), m.group(3).strip()) if m else ("N/A", "N/A")


def portfolio_fit(held: list[str], watched: list[str], results: dict[str, str], max_sector_pct: float) -> str:
    """Diversification score (1-5) for every watchlist name, by fixed rule, as a table for the screener.

    Sectors come from Yahoo for holdings and candidates alike, so both use one classification
    (portfolio.csv labels can differ). Holding weights come from portfolio_analytics.
    5 = sector not held; 4 = held, under half the limit; 3 = held, room under the limit;
    2 = sector at Warn (over 80% of the limit); 1 = sector over the limit.
    Same industry as a holding caps the score at 2 (it duplicates an existing exposure).
    """
    import guardrails  # table helpers; already loaded, since guardrails calls build()
    FIT.clear()
    weights = {}
    for r in guardrails._table_with(results.get("Portfolio analytics", ""), "symbol", "weight") or []:
        weights[guardrails._col(r, "symbol").upper()] = guardrails._num(guardrails._col(r, "weight")) or 0.0
    sector_w: dict[str, float] = {}
    industry_of: dict[str, list[str]] = {}
    for s in held:
        sec, ind = _sector_industry(results.get(f"Fundamentals {s}", ""))
        sector_w[sec] = sector_w.get(sec, 0.0) + weights.get(s, 0.0)
        industry_of.setdefault(ind, []).append(s)
    lim = max_sector_pct
    lines = [f"## Portfolio fit: Diversification score for every watchlist name ({len(watched)} names)",
             f"Computed at run start. Sectors from Yahoo for holdings and candidates; sector limit {lim:g}%. "
             "Copy Sector and Diversification into your table exactly as given.", "",
             "| Symbol | Sector | Industry | Portfolio weight in sector | Same industry as | Diversification |",
             "|---|---|---|---|---|---|"]
    for s in watched:
        sec, ind = _sector_industry(results.get(f"Fundamentals {s}", ""))
        w = sector_w.get(sec, 0.0)
        if sec == "N/A":
            score = 3  # unknown sector: neutral
        elif w <= 0:
            score = 5
        elif w > lim:
            score = 1
        elif w > 0.8 * lim:
            score = 2
        elif w < 0.5 * lim:
            score = 4
        else:
            score = 3
        same = industry_of.get(ind, []) if ind != "N/A" else []
        if same:
            score = min(score, 2)
        FIT[s] = score
        lines.append(f"| {s} | {sec} | {ind} | {w:.1f}% | {', '.join(same) or '-'} | {score} |")
    return "\n".join(lines)


def _safe_fit(held, watched, results, max_sector_pct) -> str:
    try:
        return portfolio_fit(held, watched, results, max_sector_pct)
    except Exception as e:  # noqa: BLE001 - the screener can still score fit itself
        log.warning("Portfolio fit table failed: %s", e)
        FIT.clear()
        return f"Portfolio fit table: N/A (failed: {e}). Score Diversification yourself from the portfolio sector weights."


def build(holdings: list[dict], watchlist: list[dict], risk_profile: dict, inputs: dict) -> dict:
    """Fetch everything and return {placeholder name: text} for the task descriptions."""
    started = time.time()
    held = [r["symbol"].upper() for r in holdings]
    watched = [r["symbol"].upper() for r in watchlist if r["symbol"].upper() not in held]
    everyone = held + watched

    events, prices, funds, portfolio = (_tool("events_calendar"), _tool("price_indicators"),
                                        _tool("fundamentals"), _tool("portfolio_analytics"))
    sectors = ", ".join(f"{r['symbol'].upper()}:{r['sector']}" for r in holdings if r.get("sector"))
    pos = ", ".join(f"{r['symbol'].upper()}:{r['shares']}" for r in holdings)

    jobs: dict[str, callable] = {
        "Events calendar": lambda: events._run(symbols=",".join(everyone),
                                              days_ahead=int(inputs.get("events_days") or 14)),
        "Portfolio analytics": lambda: portfolio._run(
            holdings=pos, cash=float(inputs.get("cash") or 0), sectors=sectors or None,
            max_position_pct=float(risk_profile.get("max_position_pct", 10)),
            max_sector_pct=float(risk_profile.get("max_sector_pct", 30)),
            min_cash_pct=float(risk_profile.get("min_cash_pct", 5))),
    }
    for s in BENCHMARKS:
        jobs[f"Benchmark {s}"] = (lambda s=s: prices._run(symbol=s))
    # Breadth, credit spreads and relative leadership for the market briefing (market_internals.py).
    import market_internals
    jobs["Market internals"] = lambda: market_internals.build(held, watched)
    # How past plans and watchlist scores turned out (track_record.py; also updates the ledger).
    import track_record
    jobs["Track record"] = lambda: track_record.build(datetime.now().strftime("%Y-%m-%d"))
    for s in everyone:
        jobs[f"Prices {s}"] = (lambda s=s: prices._run(symbol=s))
        # Holdings also get returns on capital, capital allocation and debt/dividend safety.
        jobs[f"Fundamentals {s}"] = (lambda s=s: funds._run(symbol=s, capital=s in held))

    workers = max(1, int(os.getenv("SNAPSHOT_WORKERS", "3")))
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as e:  # tools catch their own errors; this is a last resort
                results[name] = f"ERROR fetching {name}: {e}. Report figures as N/A (unavailable)."
    track = results.pop("Track record")
    if isinstance(track, str):  # the job failed: give both inputs the error
        track = {"data_track_record": track, "data_track_record_scores": track}
    results["Track record"] = track["data_track_record"]

    secs = time.time() - started
    status_rows = [(name, _status(out)) for name, out in results.items()]
    failed = [n for n, st in status_rows if not st.startswith("OK")]
    for n in failed:
        log.warning("Data snapshot: %s -> %s", n, dict(status_rows)[n])

    status = "\n".join(
        [f"Data snapshot fetched at {datetime.now():%H:%M} in {secs:.0f}s: "
         f"{len(results) - len(failed)} of {len(results)} items OK."
         + (f" Problems: {', '.join(failed)}." if failed else ""),
         "", "| Data | Result |", "|---|---|"] + [f"| {n} | {st} |" for n, st in status_rows])

    def join(names: list[str]) -> str:
        return "\n\n".join(results[n] for n in names) or "None."

    out = {
        "data_events": results["Events calendar"],
        "data_market": join([f"Benchmark {s}" for s in BENCHMARKS] + ["Market internals"]),
        "data_prices": join([f"Prices {s}" for s in everyone]),
        "data_fundamentals_holdings": join([f"Fundamentals {s}" for s in held]),
        "data_fundamentals_watchlist": (
            _safe_fit(held, watched, results, float(risk_profile.get("max_sector_pct", 30)))
            + "\n\n" + join([f"Fundamentals {s}" for s in watched])),
        "data_portfolio": results["Portfolio analytics"],
        "data_track_record": track["data_track_record"],
        "data_track_record_scores": track["data_track_record_scores"],
        "data_status": status,
    }

    # Keep the raw data with the reports (observability copies it into the run folder).
    try:
        folder = ROOT / "output"
        folder.mkdir(exist_ok=True)
        titles = {"data_status": "Status", "data_events": "Events calendar", "data_market": "Benchmarks",
                  "data_prices": "Prices and indicators", "data_fundamentals_holdings": "Fundamentals: holdings",
                  "data_fundamentals_watchlist": "Fundamentals: watchlist", "data_portfolio": "Portfolio analytics",
                  "data_track_record": "Track record: action plans", "data_track_record_scores": "Track record: watchlist scores"}
        body = "\n\n".join(f"# {titles[k]}\n\n{out[k]}" for k in titles)
        (folder / f"{inputs.get('file_date', 'snapshot')}_00_data_snapshot.md").write_text(
            f"<!-- Raw data fetched at run start by data_snapshot.py; every task received the relevant part. -->\n\n{body}\n")
    except Exception as e:
        log.warning("Could not save the data snapshot file: %s", e)
    return out
