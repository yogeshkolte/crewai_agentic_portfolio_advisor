"""Track record: how the crew's past recommendations and watchlist scores turned out.

The agents don't learn between runs; this module gives them evidence instead. At run start
(data_snapshot.build) it
  1. reads every action plan (output/*_07_action_plan.md, "Top 5 actions" table) and watchlist
     screen (output/*_03_watchlist_screen.md, ranked table) still on disk into a ledger in
     data/track_record/ (git-ignored; rows survive after old reports are deleted),
  2. scores each past item against Yahoo daily prices, always relative to SPY:
       Buy/Add      filled? (price traded at or below the top of the entry level) stop hit
                    afterwards? return from the entry to the stop or to the latest close
       Trim/Exit    right call when the stock then lagged SPY
       watch scores later return vs SPY by Total and by Value read
  3. writes two inputs: {data_track_record} for the action plan (scorecard + the open items from
     the last plan) and {data_track_record_scores} for the watchlist screener.

Every figure is computed here by a fixed rule; a group is "usable" only once MIN_N items from
MIN_N different names have a month of prices (a week of returns is mostly noise). The Size column is never stored (it holds dollar amounts).

Run on its own:  uv run python track_record.py      (updates the ledger and prints both sections)
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
LEDGER = ROOT / "data" / "track_record"
RECS = LEDGER / "recommendations.csv"
SCORES = LEDGER / "watchlist_scores.csv"
REC_FIELDS = ["date", "symbol", "action", "action_text", "level_low", "level_high", "stop", "conviction", "degraded"]
SCORE_FIELDS = ["date", "symbol", "quality", "valuation", "value_read", "catalyst", "diversification",
                "total", "verdict", "degraded"]
HORIZONS = (5, 21, 63)     # trading days: 1 week, 1 month, 3 months
MIN_N = 10                 # fewer items than this: shown, but marked too few to act on
log = logging.getLogger("portfolio_crew.data")

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?")
_NUM = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# 1. Ledger: parse the reports on disk
# ---------------------------------------------------------------------------
def _action(text: str) -> str:
    t = text.lower()
    for word, kind in (("exit", "Exit"), ("sell all", "Exit"), ("trim", "Trim"), ("reduce", "Trim"),
                       ("sell", "Trim"), ("buy", "Buy"), ("add", "Add"), ("remove", "Remove"),
                       ("hold", "Hold"), ("watch", "Hold")):
        if word in t:
            return kind
    return "Other"


def _levels(text: str) -> tuple[Optional[float], Optional[float]]:
    """'$1,290–1,340' -> (1290, 1340); 'Limit >= $280.00 (support, 2026-11-03)' -> (280, 280)."""
    t = _DATE.sub(" ", text.replace(",", ""))
    t = re.sub(r"\d+(?:\.\d+)?\s*(%|sh\b|shares|x\b|d\b|days)", " ", t)  # sizes, multiples, periods
    nums = [float(n) for n in _NUM.findall(t)]
    if not nums:
        return None, None
    if len(nums) >= 2 and re.search(r"\d\s*[–—-]\s*\$?\d", t):
        return min(nums[:2]), max(nums[:2])
    return nums[0], nums[0]


def _stop(text: str) -> Optional[float]:
    low, _ = _levels(text)
    return low


def _date_of(path: Path) -> Optional[str]:
    m = re.match(r"(\d{8})_", path.name)
    return datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d") if m else None


def _parse_plan(path: Path) -> list[dict]:
    import guardrails as g
    text = path.read_text(errors="replace")
    rows = g._table_with(text, "priority", "symbol", "action") or []
    degraded = "yes" if g.DEGRADED in text[:400] else ""
    out = []
    for r in rows:
        sym = g._symbol_cell(r)
        act = re.sub(r"[*_`]", "", g._col(r, "action")).strip()
        if not sym or not act:
            continue
        low, high = _levels(g._col(r, "price/level"))
        conv = re.sub(r"[*_`]", "", g._col(r, "conviction")).strip()[:1].upper()
        out.append({"date": _date_of(path), "symbol": sym, "action": _action(act), "action_text": act[:80],
                    "level_low": low, "level_high": high, "stop": _stop(g._col(r, "stop")),
                    "conviction": conv if conv in "HML" else "", "degraded": degraded})
    return out


def _parse_screen(path: Path) -> list[dict]:
    import guardrails as g
    text = path.read_text(errors="replace")
    rows = g._table_with(text, "symbol", "quality", "valuation", "total") or []
    degraded = "yes" if g.DEGRADED in text[:400] else ""
    out = []
    for r in rows:
        sym = g._symbol_cell(r)
        total = g._num(g._col(r, "total"))
        if not sym or total is None:
            continue
        num = lambda c: g._num(g._col(r, c))  # noqa: E731
        read = re.sub(r"[*_`]", "", g._col(r, "value read")).strip()
        out.append({"date": _date_of(path), "symbol": sym, "quality": num("quality"), "valuation": num("valuation"),
                    "value_read": read[:30], "catalyst": num("catalyst"), "diversification": num("diversification"),
                    "total": total, "verdict": re.sub(r"[*_`]", "", g._col(r, "verdict")).strip()[:20],
                    "degraded": degraded})
    return out


def _merge(path: Path, fields: list[str], fresh: dict[str, list[dict]]) -> list[dict]:
    """Replace the rows of every date parsed from a report; keep older dates whose report is gone."""
    old = []
    if path.exists():
        with path.open(newline="") as f:
            old = [r for r in csv.DictReader(f) if r.get("date") not in fresh]
    rows = old + [r for day in fresh.values() for r in day]
    rows.sort(key=lambda r: (r["date"], r["symbol"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: ("" if r.get(k) is None else r.get(k)) for k in fields} for r in rows)
    return rows


def update_ledger(before: str) -> tuple[list[dict], list[dict]]:
    """Parse every report dated before `before` (YYYY-MM-DD) into the ledger; return all rows."""
    plans: dict[str, list[dict]] = {}
    screens: dict[str, list[dict]] = {}
    for p in sorted(OUTPUT.glob("*_07_action_plan.md")):
        d = _date_of(p)
        if d and d < before:
            plans[d] = _parse_plan(p)
    for p in sorted(OUTPUT.glob("*_03_watchlist_screen.md")):
        d = _date_of(p)
        if d and d < before:
            screens[d] = _parse_screen(p)
    return _merge(RECS, REC_FIELDS, plans), _merge(SCORES, SCORE_FIELDS, screens)


# ---------------------------------------------------------------------------
# 2. Scoring against prices
# ---------------------------------------------------------------------------
def _f(x) -> Optional[float]:
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def _prices(symbols: list[str], start: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    import yfinance as yf
    tickers = sorted(set(symbols) | {"SPY"})
    first = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    df = yf.download(tickers, start=first, interval="1d", auto_adjust=True, progress=False, threads=True)
    if df is None or df.empty:
        raise RuntimeError("no price data from Yahoo")
    pick = lambda k: (df[k] if isinstance(df.columns, pd.MultiIndex)  # noqa: E731
                      else df[[k]].rename(columns={k: tickers[0]}))
    return pick("Open"), pick("Low"), pick("Close")


def _ref_index(closes: pd.Series, day: str) -> Optional[int]:
    """Index of the last close on or before `day` (what the agents saw)."""
    s = closes.dropna()
    pos = s.index.searchsorted(pd.Timestamp(day), side="right") - 1
    return int(pos) if pos >= 0 else None


def _excess(c: pd.Series, spy: pd.Series, i0: int, i1: int) -> Optional[float]:
    """Stock return minus SPY return between bars i0 and i1 of the shared index, in % points."""
    a, b, sa, sb = c.iloc[i0], c.iloc[i1], spy.iloc[i0], spy.iloc[i1]
    if any(pd.isna(x) or not x for x in (a, sa)) or pd.isna(b) or pd.isna(sb):
        return None
    return ((b / a) - (sb / sa)) * 100


def score_recs(recs: list[dict], o: pd.DataFrame, lo: pd.DataFrame, c: pd.DataFrame) -> list[dict]:
    spy = c["SPY"]
    out = []
    for r in recs:
        s = r["symbol"]
        if s not in c or r["action"] not in ("Buy", "Add", "Trim", "Exit"):
            continue
        i = _ref_index(c[s], r["date"])
        if i is None:
            continue
        last = len(c) - 1
        row = dict(r, ref=float(c[s].iloc[i]), days=last - i)
        for h in HORIZONS:
            row[f"x{h}"] = _excess(c[s], spy, i, i + h) if i + h <= last else None
        row["x_now"] = _excess(c[s], spy, i, last) if last > i else None
        if r["action"] in ("Buy", "Add"):
            top, stop = _f(r["level_high"]), _f(r["stop"])
            ref = row["ref"]
            if top and not (0.5 * ref <= top <= 1.5 * ref):
                top = None  # unparseable level (e.g. a weight); treat as a market buy
            fill_i, entry = None, None
            if top is None:
                fill_i, entry = i, ref
            else:
                for j in range(i + 1, last + 1):
                    if not pd.isna(lo[s].iloc[j]) and lo[s].iloc[j] <= top:
                        fill_i, entry = j, min(top, float(o[s].iloc[j]) if not pd.isna(o[s].iloc[j]) else top)
                        break
            row.update(filled=fill_i is not None, stopped=False, result=None)
            if fill_i is not None:
                exit_px, exit_i = float(c[s].iloc[last]), last
                if stop:
                    for j in range(fill_i + 1, last + 1):
                        if not pd.isna(lo[s].iloc[j]) and lo[s].iloc[j] <= stop:
                            exit_px, exit_i, row["stopped"] = stop, j, True
                            break
                sp0, sp1 = spy.iloc[fill_i], spy.iloc[exit_i]
                if entry and exit_i > fill_i and not pd.isna(sp0) and not pd.isna(sp1):
                    row["result"] = ((exit_px / entry) - (sp1 / sp0)) * 100
            row["right"] = row["result"] > 0 if row["result"] is not None else None
        else:
            row["right"] = row["x_now"] < 0 if row["x_now"] is not None else None
        out.append(row)
    return out


def score_watch(scores: list[dict], c: pd.DataFrame) -> list[dict]:
    spy, out, last = c["SPY"], [], len(c) - 1
    for r in scores:
        s = r["symbol"]
        if s not in c:
            continue
        i = _ref_index(c[s], r["date"])
        if i is None or i >= last:
            continue
        row = dict(r)
        for h in HORIZONS:
            row[f"x{h}"] = _excess(c[s], spy, i, i + h) if i + h <= last else None
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 3. Summaries for the agents
# ---------------------------------------------------------------------------
def _avg(xs: list) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "N/A"


def _pp(x: Optional[float]) -> str:
    return "N/A" if x is None else f"{x:+.1f} pts"


def _note(n: int, names: int) -> str:
    return "too few to act on" if min(n, names) < MIN_N else "usable"


def _first_only(rows: list[dict], key) -> list[dict]:
    """Keep one row per key (the earliest), so a call repeated in every plan counts once."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["date"]):
        if key(r) not in seen:
            seen.add(key(r))
            out.append(r)
    return out


VALUE_READS = ("Cheap for a reason", "Undervalued", "Fair", "Rich")


def _value_read(text: str) -> str:
    return next((v for v in VALUE_READS if text.lower().startswith(v.lower())), "N/A")


def _group_rows(rows: list[dict], key, label: str) -> list[str]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    lines = [f"| {label} | Items | Right call | Avg vs SPY 1 wk | Avg vs SPY 1 mo | Avg vs SPY 3 mo | Sample |",
             "|---|---|---|---|---|---|---|"]
    for k in sorted(groups):
        g = groups[k]
        judged = [r for r in g if r.get("right") is not None]
        lines.append(f"| {k} | {len(g)} | {_pct(sum(r['right'] for r in judged), len(judged))} | "
                     + " | ".join(_pp(_avg([r.get(f'x{h}') for r in g])) for h in HORIZONS)
                     + f" | {_note(len(month := [r for r in judged if r.get('x21') is not None]), len({r['symbol'] for r in month}))} |")
    return lines


def plan_section(scored: list[dict], recs: list[dict], c: Optional[pd.DataFrame]) -> str:
    lines = ["## Track record of past action plans",
             f"Computed in code at run start from {len({r['date'] for r in recs})} past plans "
             f"({len(recs)} top actions) and Yahoo daily prices. All returns are relative to SPY. Buy/Add is a right call "
             "when, after filling, it beat SPY to the stop or to the latest close; Trim/Exit is a right call when the "
             "stock then lagged SPY. A call repeated in later plans counts once, from the first plan that made it. "
             f"A group is usable only once {MIN_N} items from {MIN_N} different names have a month of prices; until then it is too few to act on.", ""]
    stats = _first_only(scored, lambda r: (r["symbol"], r["action"] in ("Buy", "Add")))
    if not stats:
        lines.append("No past recommendation has price history yet (the ledger starts with the first plan).")
    else:
        side = lambda r: "Buy/Add" if r["action"] in ("Buy", "Add") else "Trim/Exit"  # noqa: E731
        lines += _group_rows(stats, side, "Action") + [""]
        lines += _group_rows([r for r in stats if r.get("conviction")], lambda r: r["conviction"], "Conviction") + [""]
        buys = [r for r in stats if r["action"] in ("Buy", "Add")]
        filled = [r for r in buys if r["filled"]]
        stopped = [r for r in filled if r["stopped"]]
        lines += [f"Buy/Add levels: filled {_pct(len(filled), len(buys))}; stop hit after filling "
                  f"{_pct(len(stopped), len(filled))}"
                  + (" (many stops hit: check whether they sit inside normal daily noise)" if filled and len(stopped) / len(filled) > 0.4 else "")
                  + ".", ""]
    # Open items from the most recent plan, with where the price is now.
    if recs:
        last_day = max(r["date"] for r in recs)
        latest = {(r["symbol"], r["action"]): r for r in scored if r["date"] == last_day}
        lines += [f"### Open items from the last plan ({last_day})",
                  "Say for each one whether it still stands, and why, before adding new actions.", "",
                  "| Symbol | Action | Level | Stop | Price then | Price now | Status |", "|---|---|---|---|---|---|---|"]
        for r in (x for x in recs if x["date"] == last_day):
            s = latest.get((r["symbol"], r["action"]))
            px = c[r["symbol"]].dropna() if c is not None and r["symbol"] in c else pd.Series(dtype=float)
            now = f"{float(px.iloc[-1]):.2f}" if not px.empty else "N/A"
            low, high = r["level_low"] or "", r["level_high"] or ""
            lvl = (low if low == high else f"{low}–{high}") or "N/A"
            if s is None:
                status = "Not scored"
            elif r["action"] in ("Buy", "Add"):
                status = "Stopped out" if s["stopped"] else "Filled" if s["filled"] else "Not filled"
            elif s.get("x_now") is None:
                status = "No price since"
            else:
                status = "Lagging SPY since" if s["x_now"] < 0 else "Beating SPY since"
            then = f"{s['ref']:.2f}" if s else "N/A"
            lines.append(f"| {r['symbol']} | {r['action_text']} | {lvl} | {r['stop'] or 'N/A'} | {then} | {now} | {status} |")
        repeats = {}
        for r in recs:
            repeats.setdefault((r["symbol"], r["action"]), set()).add(r["date"])
        rep = sorted(f"{s} {a} ({len(d)} plans)" for (s, a), d in repeats.items() if len(d) >= 3)
        lines += ["", "Repeated across 3+ plans: " + (", ".join(rep) if rep else "none") + "."]
    return "\n".join(lines)


def scores_section(scored: list[dict], n_days: int) -> str:
    lines = ["## Track record of past watchlist scores",
             f"Computed in code from {n_days} past screens and Yahoo daily prices: later return vs SPY for names by "
             "their Total and Value read. Each name counts once per week (its first score that week). "
             f"A group is usable only once {MIN_N} items from {MIN_N} different names have a month of prices.", ""]
    scored = _first_only(scored, lambda r: (r["symbol"], date.fromisoformat(r["date"]).isocalendar()[:2]))
    if not scored:
        lines.append("No past score has price history yet.")
        return "\n".join(lines)

    def table(key, label):
        groups: dict[str, list[dict]] = {}
        for r in scored:
            groups.setdefault(key(r), []).append(r)
        out = [f"| {label} | Items | Avg vs SPY 1 wk | Avg vs SPY 1 mo | Avg vs SPY 3 mo | Sample |", "|---|---|---|---|---|---|"]
        for k in sorted(groups):
            g = groups[k]
            month = [r for r in g if r.get("x21") is not None]
            out.append(f"| {k} | {len(g)} | " + " | ".join(_pp(_avg([r.get(f'x{h}') for r in g])) for h in HORIZONS)
                       + f" | {_note(len(month), len({r['symbol'] for r in month}))} |")
        return out

    def bucket(r):
        t = _f(r["total"]) or 0
        return "16–20" if t >= 16 else "12–15" if t >= 12 else "4–11"

    lines += table(bucket, "Total") + [""] + table(lambda r: _value_read(r["value_read"]), "Value read")
    lines += ["", "If high Totals have not beaten low Totals once the sample is usable, say which score misled and weight it less."]
    return "\n".join(lines)


def build(today: Optional[str] = None) -> dict[str, str]:
    """Update the ledger and return {"data_track_record": ..., "data_track_record_scores": ...}."""
    today = today or date.today().strftime("%Y-%m-%d")
    recs, scores = update_ledger(today)
    if not recs and not scores:
        msg = "No past reports yet: the track record starts after the first full run."
        return {"data_track_record": msg, "data_track_record_scores": msg}
    symbols = sorted({r["symbol"] for r in recs} | {r["symbol"] for r in scores})
    start = min(r["date"] for r in recs + scores)
    try:
        o, lo, c = _prices(symbols, start)
        sr, sw, warn = score_recs(recs, o, lo, c), score_watch(scores, c), ""
    except Exception as e:  # noqa: BLE001 - the plan still gets its open items, unscored
        log.warning("Track record prices failed: %s", e)
        c, sr, sw = None, [], []
        warn = f"\n\nWARNING: prices unavailable ({e}); returns not scored this run."
    return {"data_track_record": plan_section(sr, recs, c) + warn,
            "data_track_record_scores": scores_section(sw, len({r["date"] for r in scores})) + warn}


if __name__ == "__main__":
    out = build()
    print(out["data_track_record"], "\n\n", out["data_track_record_scores"], sep="")
