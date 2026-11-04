"""Run reports for the Portfolio Management Crew: what ran, how long, how many tokens, what it cost.

HOW IT'S WIRED IN
    crew.jsonc references two functions here:
      before_kickoff_callbacks -> observability.start_run   (opens a new run folder)
      after_kickoff_callbacks  -> observability.finish_run  (writes the summary)
    Importing this module also registers an event listener on CrewAI's event bus,
    so every model call, tool call, guardrail check and task is recorded as it happens.
    If a run fails (e.g. a guardrail gives up), an exit handler still writes the summary.

WHAT YOU GET, PER RUN, IN output/runs/<date_time>/
    summary.md     per-task table (time, model calls, tokens, estimated cost, tool calls,
                   guardrail failures), tool stats, every guardrail failure with its message,
                   every blocked tool/website, and the slowest and most expensive task.
    events.jsonl   one JSON line per event, for digging deeper (open in any editor, or load
                   with pandas: pd.read_json(path, lines=True)).
    *.md           a copy of that run's task reports, so later runs don't overwrite them.

PRIVACY
    Everything stays on your machine. Prompts and full model responses are NOT written;
    tool arguments and outputs are truncated. Nothing is sent anywhere.

COST ESTIMATES
    Costs are estimates from the PRICES table below (US$ per 1M tokens). Check them against
    your provider's current price page and add any model you use. Models not in the table
    show "n/a" for cost but still report token counts.
"""
import atexit
import json
import logging
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from crewai.events import (
    BaseEventListener,
    CrewKickoffFailedEvent,
    LLMCallCompletedEvent,
    LLMCallFailedEvent,
    LLMCallStartedEvent,
    LLMGuardrailCompletedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    crewai_event_bus,
)

ROOT = Path(__file__).resolve().parent
# When this process started: a run folder only collects reports written after this.
_PROCESS_START = time.time()
OUTPUT = ROOT / "output"

# US$ per 1M tokens: (input, cached input, output). Verify against your provider's pricing
# page; prices change. Keys match the model name CrewAI reports (no "openai/" prefix);
# a longer key wins, so "gpt-5-mini" is matched before "gpt-5".
PRICES = {
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    # DeepSeek list prices at peak hours; off-peak is half, so costs may be overstated.
    "deepseek-flash": (0.30, 0.006, 1.20),
    "deepseek-v4-pro": (1.32, 0.044, 3.96),
    # Claude (Haiku 5.5 at its rate for prompts up to 100K tokens)
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
    # Gemini (no cached-input price listed here, so cached input is charged as input).
    # 3.8 Flash and 4 Argon are at launch prices that rise in Dec 2026 / Jan 2027.
    "gemini-3.5-flash-lite": (0.30, 0.30, 2.50),
    "gemini-3.8-flash": (0.75, 0.75, 3.75),
    "gemini-4-argon": (2.00, 2.00, 10.00),
}

TRUNC = 300  # max characters kept from tool arguments, outputs and error messages


# ---------------------------------------------------------------------------
# Run state (shared across CrewAI's handler threads, so guarded by a lock)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_run = {"dir": None, "start": None, "finished": False, "status": "running", "error": None}
_llm_started: dict[str, datetime] = {}
_task_start: dict[str, datetime] = {}
_task_end: dict[str, datetime] = {}
_task_status: dict[str, str] = {}
_task_agent: dict[str, str] = {}
_llm = defaultdict(lambda: {"calls": 0, "failed": 0, "in": 0, "cached": 0, "out": 0,
                            "reasoning": 0, "cost": 0.0, "cost_known": True, "models": set(), "secs": 0.0})
_tools = defaultdict(lambda: {"calls": 0, "errors": 0, "cache": 0, "secs": 0.0})
_tool_calls_by_task = defaultdict(int)
_guard_fails: list[dict] = []
_guard_fails_by_task = defaultdict(int)
_blocks: list[dict] = []
_buffer: list[dict] = []  # events that arrive before the run folder exists


def _short(x, n: int = TRUNC) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _ts(event) -> datetime:
    """The event's own time, as local time without a time zone.

    CrewAI stamps events in UTC; converting keeps every time comparable with
    datetime.now() and makes events.jsonl read in your local time.
    """
    t = getattr(event, "timestamp", None)
    if not isinstance(t, datetime):
        return datetime.now()
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _task(event) -> str:
    name = getattr(event, "task_name", None)
    if not name and getattr(event, "agent_role", None) == "Guardrail Agent":
        return "(plain-English reviews)"  # guardrails.py's review model, not a task
    return name or "(no task)"


def _write(record: dict) -> None:
    """Append one event to events.jsonl (or buffer it until the run folder exists)."""
    if _run["dir"] is None:
        _buffer.append(record)
        return
    with open(_run["dir"] / "events.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _price(model: str):
    for key in sorted(PRICES, key=len, reverse=True):
        if model == key or model.startswith(key + "-") or model.startswith(key + "_"):
            return PRICES[key]
    return None


def _usage(u: dict | None) -> tuple[int, int, int, int]:
    """Normalize OpenAI/Anthropic usage dicts to (input, cached input, output, reasoning)."""
    u = u or {}
    inp = u.get("prompt_tokens") or u.get("input_tokens") or 0
    out = u.get("completion_tokens") or u.get("output_tokens") or 0
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or u.get("cached_prompt_tokens") or u.get("cache_read_input_tokens") or 0)
    reasoning = ((u.get("completion_tokens_details") or {}).get("reasoning_tokens")
                 or u.get("reasoning_tokens") or 0)
    return int(inp), int(cached), int(out), int(reasoning)


# ---------------------------------------------------------------------------
# Event listener
# ---------------------------------------------------------------------------
class RunRecorder(BaseEventListener):
    def setup_listeners(self, bus) -> None:  # noqa: C901 - one handler per event type

        @bus.on(TaskStartedEvent)
        def _task_started(source, event):
            with _lock:
                name = _task(event)
                _task_start.setdefault(name, _ts(event))
                _task_agent[name] = getattr(event, "agent_role", None) or _task_agent.get(name, "")
                _task_status[name] = "running"
                _write({"t": _ts(event), "type": "task_started", "task": name})

        @bus.on(TaskCompletedEvent)
        def _task_completed(source, event):
            with _lock:
                name = _task(event)
                _task_end[name] = _ts(event)
                _task_status[name] = "completed"
                _write({"t": _ts(event), "type": "task_completed", "task": name})

        @bus.on(TaskFailedEvent)
        def _task_failed(source, event):
            with _lock:
                name = _task(event)
                _task_end[name] = _ts(event)
                _task_status[name] = "failed"
                _write({"t": _ts(event), "type": "task_failed", "task": name, "error": _short(event.error)})

        @bus.on(LLMCallStartedEvent)
        def _llm_start(source, event):
            with _lock:
                _llm_started[event.call_id] = _ts(event)

        @bus.on(LLMCallCompletedEvent)
        def _llm_done(source, event):
            with _lock:
                name, model = _task(event), getattr(event, "model", None) or "unknown"
                inp, cached, out, reasoning = _usage(event.usage)
                started = _llm_started.pop(event.call_id, None)
                secs = (_ts(event) - started).total_seconds() if started else 0.0
                price = _price(model)
                cost = None
                if price:
                    p_in, p_cached, p_out = price
                    cost = ((inp - cached) * p_in + cached * p_cached + out * p_out) / 1_000_000
                s = _llm[name]
                s["calls"] += 1
                s["in"] += inp; s["cached"] += cached; s["out"] += out; s["reasoning"] += reasoning
                s["secs"] += secs
                s["models"].add(model)
                if not _task_agent.get(name) and getattr(event, "agent_role", None):
                    _task_agent[name] = event.agent_role  # some task events don't carry the agent
                if cost is None:
                    s["cost_known"] = False
                else:
                    s["cost"] += cost
                _write({"t": _ts(event), "type": "llm_call", "task": name, "agent": getattr(event, "agent_role", None),
                        "model": model, "secs": round(secs, 2), "input_tokens": inp, "cached_tokens": cached,
                        "output_tokens": out, "reasoning_tokens": reasoning,
                        "est_cost_usd": round(cost, 5) if cost is not None else None})

        @bus.on(LLMCallFailedEvent)
        def _llm_failed(source, event):
            with _lock:
                name = _task(event)
                _llm[name]["failed"] += 1
                _llm_started.pop(getattr(event, "call_id", None), None)
                _write({"t": _ts(event), "type": "llm_failed", "task": name,
                        "model": getattr(event, "model", None), "error": _short(event.error)})

        @bus.on(ToolUsageFinishedEvent)
        def _tool_done(source, event):
            with _lock:
                secs = (event.finished_at - event.started_at).total_seconds()
                t = _tools[event.tool_name]
                t["calls"] += 1; t["secs"] += secs
                t["cache"] += 1 if getattr(event, "from_cache", False) else 0
                _tool_calls_by_task[_task(event)] += 1
                _write({"t": _ts(event), "type": "tool_call", "task": _task(event), "tool": event.tool_name,
                        "secs": round(secs, 2), "from_cache": getattr(event, "from_cache", False),
                        "args": _short(event.tool_args), "output": _short(str(event.output), 200)})

        @bus.on(ToolUsageErrorEvent)
        def _tool_error(source, event):
            with _lock:
                _tools[event.tool_name]["errors"] += 1
                _tool_calls_by_task[_task(event)] += 1
                _write({"t": _ts(event), "type": "tool_error", "task": _task(event), "tool": event.tool_name,
                        "args": _short(event.tool_args), "error": _short(str(event.error))})

        @bus.on(LLMGuardrailCompletedEvent)
        def _guardrail(source, event):
            with _lock:
                name = _task(event)
                check = getattr(event, "guardrail_name", None) or getattr(event, "guardrail_type", None) or "guardrail"
                rec = {"t": _ts(event), "type": "guardrail", "task": name, "check": _short(str(check), 120),
                       "passed": event.success, "attempt": event.retry_count + 1}
                if not event.success:
                    rec["error"] = str(event.error)
                    _guard_fails.append(rec)
                    _guard_fails_by_task[name] += 1
                _write(rec)

        @bus.on(CrewKickoffFailedEvent)
        def _crew_failed(source, event):
            with _lock:
                _run["status"] = "failed"
                _run["error"] = str(event.error)
                _write({"t": _ts(event), "type": "crew_failed", "error": _short(str(event.error), 1000)})


class _HookBlockHandler(logging.Handler):
    """Records the warnings hooks.py logs when it blocks a tool call or website."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        with _lock:
            rec = {"t": datetime.now(), "type": "hook_block", "message": _short(msg)}
            _blocks.append(rec)
            _write(rec)


_recorder = RunRecorder()
logging.getLogger("portfolio_crew.hooks").addHandler(_HookBlockHandler(level=logging.WARNING))


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


# ---------------------------------------------------------------------------
# Callbacks referenced from crew.jsonc
# ---------------------------------------------------------------------------
def start_run(inputs: dict) -> dict:
    """before_kickoff_callbacks: create output/runs/<date_time>/ and start the clock."""
    resumed = not inputs  # a resumed run (scripts/resume_run.py) passes no inputs
    inputs = inputs or {}
    with _lock:
        now = datetime.now()
        run_dir = OUTPUT / "runs" / now.strftime("%Y-%m-%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        _run.update(dir=run_dir, start=now, prefix=inputs.get("file_date"), provider=_provider_line())
        for rec in _buffer:
            _write(rec)
        _buffer.clear()
        _write({"t": now, "type": "run_resumed" if resumed else "run_started", "current_date": inputs.get("current_date"),
                "inputs": sorted(inputs)})  # input names only; your holdings aren't copied here
    return inputs


def _provider_line() -> str:
    """'deepseek (from MODEL_PROVIDER)  |  **Review model:** openai/gpt-5-mini' for summary.md."""
    import os
    try:
        import models
        line = f"{models.provider()} (from {'MODEL_PROVIDER' if os.getenv('MODEL_PROVIDER') else 'models.jsonc'})"
        if os.getenv("GUARDRAIL_REVIEW", "on").lower() != "off":
            line += f"  |  **Review model:** {models.judge_spec()['model']}"
        return line
    except Exception as e:  # never let the run report stop a run
        return f"unknown ({e})"


def finish_run(output):
    """after_kickoff_callbacks: write the summary for a run that completed."""
    with _lock:
        if _run["status"] == "running":
            _run["status"] = "completed"
    _finish()
    return output


def _finish() -> None:
    if _run["dir"] is None or _run["finished"]:
        return
    try:
        crewai_event_bus.flush(timeout=10)  # let in-flight handlers finish first
    except Exception:
        pass
    with _lock:
        _run["finished"] = True
        if _run["status"] == "running":
            _run["status"] = "failed or interrupted"
        # Keep this run's reports with its summary. Only files with this run's date
        # prefix, so earlier days' reports in output/ aren't swept in.
        pattern = f"{_run['prefix']}_*.md" if _run.get("prefix") else "*.md"
        for md in OUTPUT.glob(pattern):
            if md.stat().st_mtime >= _PROCESS_START - 1:  # written by this run, not an earlier one
                shutil.copy2(md, _run["dir"] / md.name)
        (_run["dir"] / "summary.md").write_text(_summary())
    print(f"\nRun report: {_run['dir'] / 'summary.md'}")
    _open_viewer()


VIEWER_PORT = 8501  # matches .streamlit/config.toml


def _open_viewer() -> None:
    """Open the report viewer in the browser when a run ends (REPORT_VIEWER=off in .env skips it).

    Starts `streamlit run ui/app.py` in the background if it isn't running yet. It keeps
    running after `crewai run` exits (stop it with `pkill -f "streamlit run ui/app.py"`), and
    it lists runs newest first, so the page opens on this run. Never raises.
    """
    import os
    if os.getenv("REPORT_VIEWER", "on").lower() == "off":
        return
    try:
        import socket
        import subprocess
        import sys
        import webbrowser

        def up() -> bool:
            with socket.socket() as s:
                s.settimeout(0.5)
                return s.connect_ex(("127.0.0.1", VIEWER_PORT)) == 0

        if not up():
            log_file = (OUTPUT / "viewer.log").open("a")
            subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", str(ROOT / "ui" / "app.py"),
                 "--server.port", str(VIEWER_PORT),
                 "--server.headless", "true"],  # we open the browser ourselves, once
                cwd=ROOT, stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL,
                start_new_session=True)  # survives the end of this run
            for _ in range(40):
                if up():
                    break
                time.sleep(0.5)
            else:
                print(f"Report viewer did not start; see {OUTPUT / 'viewer.log'}")
                return
        url = f"http://localhost:{VIEWER_PORT}"
        webbrowser.open(url)
        print(f"Report viewer: {url}")
    except Exception as e:  # noqa: BLE001 - opening the viewer must never break a run
        print(f"Could not open the report viewer: {e}")


atexit.register(_finish)  # failed runs never reach finish_run, so write the report on exit


# ---------------------------------------------------------------------------
# summary.md
# ---------------------------------------------------------------------------
def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _fmt_cost(c: float, known: bool) -> str:
    return f"${c:,.3f}" + ("" if known else " + n/a")


def _summary() -> str:
    end = datetime.now()
    total = (end - _run["start"]).total_seconds() if _run["start"] else 0
    tasks = sorted(set(_task_start) | set(_llm), key=lambda n: _task_start.get(n, end))
    tot = {"calls": 0, "in": 0, "cached": 0, "out": 0, "cost": 0.0, "known": True}

    rows = []
    for name in tasks:
        s = _llm[name]
        dur = ((_task_end.get(name, end) - _task_start[name]).total_seconds() if name in _task_start else 0)
        tot["calls"] += s["calls"]; tot["in"] += s["in"]; tot["cached"] += s["cached"]; tot["out"] += s["out"]
        tot["cost"] += s["cost"]; tot["known"] &= s["cost_known"]
        rows.append((name, dur, s))

    lines = [
        f"# Run report: {_run['start']:%Y-%m-%d %H:%M}",
        "",
        f"**Status:** {_run['status']}  ",
        f"**LLM provider:** {_run.get('provider') or 'unknown'}  ",
        f"**Duration:** {_fmt_secs(total)}  ",
        f"**Model calls:** {tot['calls']}  |  **Tokens:** {tot['in']:,} in ({tot['cached']:,} cached), "
        f"{tot['out']:,} out  |  **Estimated cost:** {_fmt_cost(tot['cost'], tot['known'])}  ",
        f"**Tool calls:** {sum(t['calls'] + t['errors'] for t in _tools.values())} "
        f"({sum(t['errors'] for t in _tools.values())} errors)  |  "
        f"**Guardrail failures:** {len(_guard_fails)}  |  **Blocked by hooks:** {len(_blocks)}  ",
        f"**Degraded reports:** {len(_degraded)}  |  **Data issues:** {len(_data_issues)}",
    ]
    if _run["error"]:
        lines += ["", f"**Error:** {_short(_run['error'], 1000)}"]

    lines += ["", "## Tasks", "",
              "| Task | Agent | Model | Status | Time | Model calls | Input tokens | Output tokens | Est. cost | Tool calls | Guardrail fails |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, dur, s in rows:
        lines.append(
            f"| {name} | {_task_agent.get(name, '')} | {', '.join(sorted(s['models'])) or '-'} | "
            f"{_task_status.get(name, '-')} | {_fmt_secs(dur)} | {s['calls']} | {s['in']:,} | {s['out']:,} | "
            f"{_fmt_cost(s['cost'], s['cost_known'])} | {_tool_calls_by_task.get(name, 0)} | {_guard_fails_by_task.get(name, 0)} |")

    timed = [r for r in rows if r[0] in _task_start]
    if timed:
        slow = max(timed, key=lambda r: r[1])
        costly = max(rows, key=lambda r: r[2]["cost"])
        lines += ["", f"Slowest task: **{slow[0]}** ({_fmt_secs(slow[1])}). "
                      f"Most expensive: **{costly[0]}** ({_fmt_cost(costly[2]['cost'], costly[2]['cost_known'])})."]

    if _tools:
        lines += ["", "## Tools", "", "| Tool | Calls | Errors | Cache hits | Avg time |", "|---|---|---|---|---|"]
        for tool, t in sorted(_tools.items(), key=lambda kv: -kv[1]["calls"]):
            avg = t["secs"] / t["calls"] if t["calls"] else 0
            lines.append(f"| {tool} | {t['calls']} | {t['errors']} | {t['cache']} | {avg:.1f}s |")

    if _guard_fails:
        lines += ["", "## Guardrail failures", ""]
        for g in _guard_fails:
            lines.append(f"- **{g['task']}**, attempt {g['attempt']}, check `{g['check']}`:")
            lines += [f"  > {ln}" for ln in str(g.get("error", "")).splitlines()[:12]]

    if _blocks:
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

    lines += ["", "---", "Costs are estimates from the PRICES table in observability.py; "
                     "\"n/a\" means a model isn't in that table. Details: events.jsonl in this folder."]
    return "\n".join(lines) + "\n"
