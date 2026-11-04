"""Make a run survive a failing task. Run from the project root after scripts/harden_tools.py:

    python3 scripts/make_robust.py

1. Data snapshot: validate_inputs fetches all market data once (data_snapshot.py) and every
   task gets its slice as an input ({data_events}, {data_prices}, ...).
2. One combined check per task (safety + content rules + a plain-English review on a cheap
   model). On the last attempt a failing report is stamped DEGRADED REPORT and the run
   continues, except for safety failures, which still stop the run.
3. Fallback rule for every agent; technicals and risk get the events tool.
4. The action plan gets a Data gaps section (and a check that it's there).
5. Checkpoints after every task, so a failed run can be resumed.
Also: run reports list degraded reports and data issues; .gitignore covers the new folders.

Backups: <file>.bak. Safe to run twice.
"""
import json
import re
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
done: list[str] = []


def edit(rel: str, fn) -> None:
    p = root / rel
    old = p.read_text()
    new = fn(old)
    if new != old:
        shutil.copy(p, p.with_name(p.name + ".bak"))
        p.write_text(new)
        done.append(rel)


def once(s: str, a: str, b: str, marker: str) -> str:
    """Replace a with b exactly once, unless marker shows it's already done."""
    if marker in s:
        return s
    assert s.count(a) == 1, f"expected text not found exactly once: {a[:70]!r}"
    return s.replace(a, b, 1)


# ===========================================================================
# guardrails.py
# ===========================================================================
SNAPSHOT_CALL = '''    inputs["risk_profile"] = json.dumps(rp, indent=2)

    # Fetch all market data once, before any agent starts (data_snapshot.py). Each task
    # receives its part as an input, so no task depends on another to get its numbers.
    # A failure here never stops the run: the agents fall back to their own tools.
    try:
        import data_snapshot
        inputs.update(data_snapshot.build(holdings, watch, rp, inputs))
    except Exception as e:  # noqa: BLE001 - any failure just means "agents fetch it themselves"
        for k in SNAPSHOT_SLICES:
            inputs[k] = (f"Not available: the data snapshot failed ({e}). "
                         "Fetch what you need with your tools and list it under data gaps.")
    return inputs
'''

COMBINED = r'''

# ===========================================================================
# ONE CHECK PER TASK, AND "DEGRADE INSTEAD OF STOP"
# ===========================================================================
# crew.jsonc gives each task a single check, check_<task>, which runs:
#   1. safety_check (on every attempt, including retries),
#   2. the task's content rules (the functions above),
#   3. a plain-English review by a small, cheap model (only if 1 and 2 pass).
#
# Why one combined check: CrewAI gives each listed check its own retry loop, and a
# retried report is only re-checked by the current check and the ones after it. With
# safety_check listed first, retried reports were never safety-checked.
#
# Degrade instead of stop: if a report still fails on its LAST attempt, it is accepted
# with a "DEGRADED REPORT" banner listing the problems, and the run continues. Later
# agents are told to treat such reports with caution and fill gaps with their tools,
# and the action plan lists them under Data gaps. Safety failures never degrade: they
# still stop the run. Every rejected report is saved next to the run report.
import contextvars  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402

_glog = logging.getLogger("portfolio_crew.guardrails")
DEGRADED = "DEGRADED REPORT"

# Model for the plain-English reviews. A small model is enough to judge a checklist.
JUDGE_MODEL = os.getenv("GUARDRAIL_JUDGE_MODEL", "openai/gpt-5-mini")


def _task_retries() -> dict:
    """guardrail_max_retries per task from crew.jsonc (CrewAI allows retries + 1 attempts)."""
    try:
        crew = json.loads(_strip_jsonc((ROOT / "crew.jsonc").read_text()))
        return {t["name"]: int(t.get("guardrail_max_retries", 3)) for t in crew.get("tasks", [])}
    except Exception:
        return {}


_RETRIES = _task_retries()
_attempts: dict = {}
_attempts_lock = threading.Lock()
_judge = None


def _judge_llm():
    global _judge
    if _judge is None:
        from crewai import LLM
        try:
            _judge = LLM(model=JUDGE_MODEL, reasoning_effort="low")
        except Exception:
            _judge = LLM(model=JUDGE_MODEL)
    return _judge


def _review(result, criteria: str) -> list:
    """Plain-English review. A review that can't run (model error) is skipped, not failed."""
    if os.getenv("GUARDRAIL_REVIEW", "on").lower() == "off":
        return []
    from crewai.tasks.llm_guardrail import LLMGuardrail

    ok, feedback = LLMGuardrail(description=criteria, llm=_judge_llm())(result)
    if ok:
        return []
    if str(feedback).startswith("Error while validating"):
        _glog.warning("Plain-English review skipped for %s: %s", getattr(result, "name", "task"), str(feedback)[:200])
        return []
    return [f"Review: {feedback}"]


def _problems(check, result) -> list:
    ok, msg = check(result)
    if ok:
        return []
    msg = str(msg)
    return [p.strip() for p in msg.split("\n- ")[1:]] if "\n- " in msg else [msg]


def _stamp(result, problems: list, attempts: int) -> str:
    banner = (f"> **{DEGRADED}**: this report still failed these checks after {attempts} attempts, and the run "
              "continued with it. Treat it with caution and verify anything you rely on.\n"
              + "".join(f"> - {p}\n" for p in problems) + "\n")
    return banner + _text(result)


def _run_task_checks(result, checks: list, review: str | None = None):
    task = getattr(result, "name", None) or "task"
    with _attempts_lock:
        _attempts[task] = _attempts.get(task, 0) + 1
        attempt = _attempts[task]
    final = attempt >= _RETRIES.get(task, 3) + 1

    token = _SAVE_REJECTED.set(False)  # save once below, not once per rule
    try:
        safety = _problems(safety_check, result)
        problems = list(safety)
        for check in checks:
            problems += _problems(check, result)
        if not problems and review:
            problems += _review(result, review)
    finally:
        _SAVE_REJECTED.reset(token)

    if not problems:
        return True, result
    _save_rejected(result, problems)
    if final and not safety:
        _glog.warning("%s accepted as DEGRADED after %d attempts: %s", task, attempt, "; ".join(problems)[:400])
        return True, _stamp(result, problems, attempt)
    return False, "Fix these problems and return the full corrected output:\n- " + "\n- ".join(problems)


def data_gaps_section(result):
    """The action plan must say what it couldn't verify, so a plan built on partial data says so."""
    if re.search(r"(?i)data gaps", _text(result)):
        return True, result
    return False, ("Add a 'Data gaps' section listing every DEGRADED report, every figure given as N/A and "
                   "every backup data source used (write None if there were none).")


# Plain-English review criteria (previously listed as text in crew.jsonc).
REVIEW_MARKET = ("The briefing states facts and flags only. It contains no buy, sell or hold recommendations, "
                 "and every news item has a date and a source.")
REVIEW_HOLDINGS = ("Every holding in the portfolio has a scorecard row, a bull case, a bear case and a specific, "
                   "checkable 'what would make me sell' condition. Every valuation claim includes a figure, or says "
                   "N/A (unavailable).")
REVIEW_WATCHLIST = "Every symbol in the watchlist appears in the ranked table with a verdict of Shortlist, Keep watching or Remove."
REVIEW_TECHNICALS = ("For every long position the stop level is below the entry or add zone and the trim zone is above "
                     "it, and every name with earnings in the next 2 weeks is flagged.")
REVIEW_PLAN = ("Every disagreement between specialists is stated with its resolution, and no action contradicts a "
               "risk-manager red flag without an explicit written justification.")


# The functions crew.jsonc references, one per task.
def check_market_context(result):
    return _run_task_checks(result, [cites_sources], REVIEW_MARKET)


def check_events_calendar(result):
    return _run_task_checks(result, [valid_event_calendar, cites_sources])


def check_holdings_fundamentals(result):
    return _run_task_checks(result, [], REVIEW_HOLDINGS)


def check_watchlist_screen(result):
    return _run_task_checks(result, [watchlist_shortlist_size], REVIEW_WATCHLIST)


def check_technicals(result):
    return _run_task_checks(result, [], REVIEW_TECHNICALS)


def check_covered_calls(result):
    return _run_task_checks(result, [covered_call_limits])


def check_risk(result):
    return _run_task_checks(result, [risk_dashboard_complete])


def check_action_plan(result):
    return _run_task_checks(result, [action_plan_checks, data_gaps_section], REVIEW_PLAN)
'''


def guardrails(s: str) -> str:
    s = once(s, "from typing import Any, Tuple\n", "import contextvars\nfrom typing import Any, Tuple\n",
             "import contextvars\nfrom typing")
    s = once(s, '''    Raising an error here stops the run before any agent starts.
    """
''', '''    Raising an error here stops the run before any agent starts.
    """
    # Resuming from a checkpoint (scripts/resume_run.py): CrewAI calls this again with no
    # inputs, and the restored tasks already hold the data. Nothing to check or fetch.
    if not inputs or "portfolio_file" not in inputs:
        return inputs or {}
''', "Resuming from a checkpoint")
    s = once(s, 'DISCLAIMER = "not investment advice"\n',
             'DISCLAIMER = "not investment advice"\n\n'
             '# Inputs filled by data_snapshot.build() (see validate_inputs).\n'
             'SNAPSHOT_SLICES = ("data_events", "data_market", "data_prices", "data_fundamentals_holdings",\n'
             '                   "data_fundamentals_watchlist", "data_portfolio", "data_status")\n',
             "SNAPSHOT_SLICES = (")
    s = once(s, '    inputs["risk_profile"] = json.dumps(rp, indent=2)\n    return inputs\n', SNAPSHOT_CALL,
             "import data_snapshot")
    s = once(s, "def _fail(problems: list[str], result: Any = None) -> Tuple[bool, Any]:",
             "# Set to False by the combined checks below, which save one copy themselves.\n"
             "_SAVE_REJECTED = contextvars.ContextVar(\"save_rejected\", default=True)\n\n\n"
             "def _fail(problems: list[str], result: Any = None) -> Tuple[bool, Any]:",
             "_SAVE_REJECTED = contextvars")
    s = once(s, '        path = folder / f"rejected_{task}_{datetime.now():%H%M%S}.md"\n',
             '        path = folder / f"rejected_{task}_{datetime.now():%H%M%S_%f}.md"  # unique per attempt\n',
             "%H%M%S_%f")
    s = once(s, "    if result is not None:\n        _save_rejected(result, problems)\n",
             "    if result is not None and _SAVE_REJECTED.get():\n        _save_rejected(result, problems)\n",
             "_SAVE_REJECTED.get()")
    if "def check_action_plan" not in s:
        s = s.rstrip("\n") + "\n" + COMBINED
    return s


edit("guardrails.py", guardrails)

# ===========================================================================
# observability.py: list degraded reports and data issues in summary.md
# ===========================================================================
NOTES = '''

class _NoteHandler(logging.Handler):
    """Collects warnings from guardrails.py (degraded reports) and the data layer (retries,
    backup sources, snapshot failures) for the run report."""

    def __init__(self, sink: list, kind: str) -> None:
        super().__init__(level=logging.WARNING)
        self.sink, self.kind = sink, kind

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        with _lock:
            rec = {"t": datetime.now(), "type": self.kind, "message": _short(msg, 500)}
            self.sink.append(rec)
            _write(rec)


_degraded: list[dict] = []
_data_issues: list[dict] = []
logging.getLogger("portfolio_crew.guardrails").addHandler(_NoteHandler(_degraded, "degraded"))
logging.getLogger("portfolio_crew.data").addHandler(_NoteHandler(_data_issues, "data_issue"))
'''


def observability(s: str) -> str:
    s = once(s, '''def start_run(inputs: dict) -> dict:
    """before_kickoff_callbacks: create output/runs/<date_time>/ and start the clock."""
''', '''def start_run(inputs: dict) -> dict:
    """before_kickoff_callbacks: create output/runs/<date_time>/ and start the clock."""
    resumed = not inputs  # a resumed run (scripts/resume_run.py) passes no inputs
    inputs = inputs or {}
''', "resumed = not inputs")
    s = once(s, '''        _write({"t": now, "type": "run_started", "current_date": inputs.get("current_date"),''',
             '''        _write({"t": now, "type": "run_resumed" if resumed else "run_started", "current_date": inputs.get("current_date"),''',
             '"run_resumed" if resumed')
    s = once(s, '''def _task(event) -> str:
    return getattr(event, "task_name", None) or "(no task)"
''', '''def _task(event) -> str:
    name = getattr(event, "task_name", None)
    if not name and getattr(event, "agent_role", None) == "Guardrail Agent":
        return "(plain-English reviews)"  # guardrails.py's review model, not a task
    return name or "(no task)"
''', "(plain-English reviews)")
    s = once(s, 'logging.getLogger("portfolio_crew.hooks").addHandler(_HookBlockHandler(level=logging.WARNING))\n',
             'logging.getLogger("portfolio_crew.hooks").addHandler(_HookBlockHandler(level=logging.WARNING))\n' + NOTES,
             "class _NoteHandler")
    s = once(s, '''        f"**Guardrail failures:** {len(_guard_fails)}  |  **Blocked by hooks:** {len(_blocks)}",
    ]''', '''        f"**Guardrail failures:** {len(_guard_fails)}  |  **Blocked by hooks:** {len(_blocks)}  ",
        f"**Degraded reports:** {len(_degraded)}  |  **Data issues:** {len(_data_issues)}",
    ]''', "**Degraded reports:**")
    s = once(s, '''    if _blocks:
        lines += ["", "## Blocked by hooks", ""] + [f"- {b['message']}" for b in _blocks]
''', '''    if _blocks:
        lines += ["", "## Blocked by hooks", ""] + [f"- {b['message']}" for b in _blocks]

    if _degraded:
        lines += ["", "## Degraded reports", "",
                  "Accepted on their last attempt despite failing checks; each starts with a DEGRADED REPORT banner.", ""]
        lines += [f"- {d['message']}" for d in _degraded]

    if _data_issues:
        lines += ["", "## Data issues", "", "Retries, backup sources used and snapshot items that failed.", ""]
        lines += [f"- {d['message']}" for d in _data_issues[:40]]
        if len(_data_issues) > 40:
            lines.append(f"- … and {len(_data_issues) - 40} more in events.jsonl")
''', "## Degraded reports")
    return s


edit("observability.py", observability)

# ===========================================================================
# crew.jsonc
# ===========================================================================
CHECKS = {
    "market_context_task": "check_market_context",
    "events_calendar_task": "check_events_calendar",
    "holdings_fundamental_review_task": "check_holdings_fundamentals",
    "watchlist_screening_task": "check_watchlist_screen",
    "technical_analysis_task": "check_technicals",
    "covered_call_task": "check_covered_calls",
    "risk_assessment_task": "check_risk",
    "portfolio_action_plan_task": "check_action_plan",
}
DATA = {  # task -> text appended to its description (inside the JSON string, so \\n)
    "market_context_task": "\\n\\nPRE-FETCHED DATA (price_indicators tool for SPY, QQQ and IWM, fetched at run start; "
                           "use it for the regime numbers):\\n{data_market}",
    "holdings_fundamental_review_task": "\\n\\nPRE-FETCHED DATA (fundamentals tool for each holding, fetched at run start; "
                                        "call the tool only for a holding missing here or showing ERROR):\\n{data_fundamentals_holdings}",
    "watchlist_screening_task": "\\n\\nPRE-FETCHED DATA (fundamentals tool for each watchlist name, fetched at run start; "
                                "call the tool only for a name missing here or showing ERROR):\\n{data_fundamentals_watchlist}",
    "technical_analysis_task": "\\n\\nPRE-FETCHED DATA (price_indicators tool for every holding and watchlist name vs SPY, "
                               "fetched at run start; call the tool only for a symbol missing here or showing ERROR, or to "
                               "compare with a sector ETF):\\n{data_prices}",
    "risk_assessment_task": "\\n\\nPRE-FETCHED DATA (portfolio_analytics tool with your limits, fetched at run start; call the "
                            "tool only if this shows ERROR):\\n{data_portfolio}\\n\\nData snapshot status:\\n{data_status}",
    "portfolio_action_plan_task": "\\nIf any report you're given starts with DEGRADED REPORT, or the data snapshot status "
                                  "below lists problems, say how that limits your recommendations and lower the conviction "
                                  "of any action that depends on it.\\n\\nData snapshot status:\\n{data_status}",
}
INPUTS = '''    "events_days": 14,                          // look-ahead for the events calendar; keep equal to the events task's "next N days"
    "cash": 0,                                  // cash balance in dollars, for the risk check's cash-vs-minimum test
    // Filled at run start by data_snapshot.py (via guardrails.validate_inputs); placeholders only.
    "data_events": "(fetched at run start)",
    "data_market": "(fetched at run start)",
    "data_prices": "(fetched at run start)",
    "data_fundamentals_holdings": "(fetched at run start)",
    "data_fundamentals_watchlist": "(fetched at run start)",
    "data_portfolio": "(fetched at run start)",
    "data_status": "(fetched at run start)",
'''


def task_block(s: str, name: str) -> tuple[int, int]:
    a = s.index(f'"name": "{name}"')
    a = s.rindex("    {\n", 0, a)
    b = s.index("\n    }", a) + len("\n    }")
    return a, b


def crew(s: str) -> str:
    # 5. Checkpoints
    s = once(s, '  "output_log_file": "output/crew_run.log", // full audit trail of every run\n',
             '  "output_log_file": "output/crew_run.log", // full audit trail of every run\n'
             '  // Saves progress after every task in .checkpoints/, so a failed run can be resumed with\n'
             '  // `crewai checkpoint resume` instead of starting over. Keeps the 20 most recent.\n'
             '  "checkpoint": {"max_checkpoints": 20},\n',
             '"checkpoint":')
    # 1. Snapshot inputs
    s = once(s, '    "watchlist": "(loaded from watchlist_file)",\n',
             '    "watchlist": "(loaded from watchlist_file)",\n' + INPUTS, '"data_events":')

    for task, fn in CHECKS.items():
        if f'"name": "{task}"' not in s:
            continue
        a, b = task_block(s, task)
        blk = s[a:b]
        # 2. One combined check
        blk = re.sub(r'      // Guardrails run in order[^\n]*\n', "", blk)
        blk = re.sub(r'"guardrails": \[\n.*?\n      \],',
                     '// One combined check: safety, content rules and a plain-English review; on the last attempt a\n'
                     '      // failing report is accepted as DEGRADED and the run continues (see guardrails.py).\n'
                     f'      "guardrails": [{{"python": "guardrails.{fn}"}}],', blk, count=1, flags=re.S)
        # 1. Snapshot data in the description
        if task in DATA and DATA[task].split("{")[1].split("}")[0] not in blk:
            blk = re.sub(r'("description": "(?:[^"\\]|\\.)*)(")', lambda m: m.group(1) + DATA[task] + m.group(2),
                         blk, count=1)
        s = s[:a] + blk + s[b:]

    # Events: use the pre-fetched tool output instead of calling the tool.
    s = re.sub(r"1\. Call the events_calendar tool ONCE, passing every holding and watchlist symbol in one "
               r"comma-separated list, with days_ahead=\d+\. Use its earnings, dividend and option-expiration figures "
               r"exactly as returned\.",
               "1. The events_calendar tool output for every holding and watchlist symbol was fetched at run start and "
               "is included below under PRE-FETCHED DATA. Use its earnings, dividend and expiration figures exactly as "
               "given. Call the tool yourself only if that data shows ERROR.", s)
    if "{data_events}" not in s:
        a, b = task_block(s, "events_calendar_task")
        blk = re.sub(r'("description": "(?:[^"\\]|\\.)*)(")',
                     lambda m: m.group(1) + "\\n\\nPRE-FETCHED DATA (events_calendar tool, fetched at run start):\\n{data_events}"
                     + m.group(2), s[a:b], count=1)
        s = s[:a] + blk + s[b:]

    # 4. Data gaps section in the action plan.
    a, b = task_block(s, "portfolio_action_plan_task")
    blk = s[a:b]
    if "Data gaps" not in blk:
        blk = re.sub(r'("expected_output": "(?:[^"\\]|\\.)*?)( Length:[^"]*)?(")',
                     lambda m: m.group(1) + "\\n10. Data gaps: every DEGRADED report, every figure given as N/A, and "
                     "every backup data source used (write None if there were none)." + (m.group(2) or "") + m.group(3),
                     blk, count=1)
    s = s[:a] + blk + s[b:]
    return s


edit("crew.jsonc", crew)

# ===========================================================================
# agents/*.jsonc: fallback rule; events tool for technicals and risk
# ===========================================================================
FALLBACK = ("\\n\\nFallback rule: use the PRE-FETCHED DATA in your task and the earlier reports first. If something you "
            "need is missing there, shows ERROR or N/A, or comes from a report that starts with DEGRADED REPORT, get it "
            "yourself with your tools if you can, and say in your report that you did. If you still can't get it, write "
            "N/A (unavailable) so it can be listed under data gaps.")

for p in sorted((root / "agents").glob("*.jsonc")):
    def agent(s: str, name=p.stem) -> str:
        if "Fallback rule:" not in s:
            s = re.sub(r'("backstory": "(?:[^"\\]|\\.)*)(")', lambda m: m.group(1) + FALLBACK + m.group(2), s, count=1)
        if name in ("technical_analyst", "risk_manager") and "custom:events_calendar" not in s:
            s = re.sub(r'("tools": \[)', r'\1"custom:events_calendar", ', s, count=1)
        return s
    edit(f"agents/{p.name}", agent)

# ===========================================================================
# .gitignore: the new folders hold your holdings and API responses
# ===========================================================================
gi = root / ".gitignore"
lines = gi.read_text().splitlines() if gi.exists() else []
add = [x for x in ("output/", ".cache/", ".checkpoints/", "*.bak") if x not in lines]
if add:
    gi.write_text("\n".join(lines + add) + "\n")
    done.append(".gitignore")

print("changed:" if done else "nothing to change (already applied)")
for d in done:
    print("  " + d)
