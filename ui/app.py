"""Report viewer for the Portfolio Management Crew.

    uv run streamlit run ui/app.py

Opens in your browser at http://localhost:8501. Read-only: it shows the reports in output/
and never changes them. It only accepts connections from this computer
(see .streamlit/config.toml), because the reports describe your holdings.

Where it reads from
    output/runs/<date_time>/   one folder per run (written by observability.py): that run's
                               reports, summary.md, events.jsonl and any rejected reports.
    output/*.md                reports from before run folders existed, grouped by date prefix.
"""
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

# Report tabs, in reading order: (label, file name ending).
REPORTS = [
    ("Action plan", "07_action_plan.md"),
    ("Risk", "06_risk.md"),
    ("Technicals", "04_technicals.md"),
    ("Holdings", "02_holdings_fundamentals.md"),
    ("Watchlist", "03_watchlist_screen.md"),
    ("Market", "01_market_context.md"),
    ("Events", "events_calendar.md"),
    ("Covered calls", "05_covered_calls.md"),
    ("Data snapshot", "00_data_snapshot.md"),
]
DEGRADED = "DEGRADED REPORT"
# Short names for tasks in charts.
TASK_LABELS = {
    "events_calendar_task": "Events", "market_context_task": "Market", "holdings_fundamental_review_task": "Holdings",
    "watchlist_screening_task": "Watchlist", "technical_analysis_task": "Technicals", "covered_call_task": "Covered calls",
    "risk_assessment_task": "Risk", "portfolio_action_plan_task": "Action plan", "(plain-English reviews)": "Reviews",
}

st.set_page_config(page_title="Portfolio Advisor Reports", page_icon="📈", layout="wide")


# ---------------------------------------------------------------------------
# Finding runs
# ---------------------------------------------------------------------------
def list_runs() -> list[dict]:
    """Every run, newest first. A run is a folder in output/runs/, or a date group of loose files."""
    runs = []
    runs_dir = OUTPUT / "runs"
    if runs_dir.is_dir():
        for d in sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True):
            try:
                started = datetime.strptime(d.name, "%Y-%m-%d_%H%M%S")
            except ValueError:
                continue
            files = {f.name: f for f in d.glob("*.md")}
            runs.append({"key": f"run:{d.name}", "started": started, "folder": d, "files": files})

    # Loose reports in output/, only for days with no run folder (older layout). On days that
    # have run folders, output/ just holds copies of the latest run's files.
    run_days = {r["started"].strftime("%Y%m%d") for r in runs}
    loose: dict[str, dict] = {}
    for f in OUTPUT.glob("*.md") if OUTPUT.is_dir() else []:
        m = re.match(r"(\d{8})_", f.name)
        day = m.group(1) if m else "undated"
        if day not in run_days:
            loose.setdefault(day, {})[f.name] = f
    for day, files in sorted(loose.items(), reverse=True):
        started = datetime.strptime(day, "%Y%m%d") if day != "undated" else datetime.min
        runs.append({"key": f"loose:{day}", "started": started, "folder": None, "files": files})
    return runs


def find(files: dict, ending: str) -> Path | None:
    """The report whose name ends with this suffix (with or without a date prefix)."""
    for name, path in files.items():
        if name == ending or name.endswith("_" + ending):
            return path
    return None


def summary_fields(text: str) -> dict:
    """The bold 'Label: value' pairs at the top of summary.md."""
    fields = {}
    for label, value in re.findall(r"\*\*([^*:]+):\*\*\s*([^|\n]+)", text):
        fields[label.strip()] = value.strip()
    return fields


def run_label(run: dict) -> str:
    if run["folder"] is None:
        day = run["key"].split(":")[1]
        when = run["started"].strftime("%a %d %b %Y") if day != "undated" else "Undated reports"
        return f"{when} · reports in output/"
    label = run["started"].strftime("%a %d %b %Y, %H:%M")
    summary = run["folder"] / "summary.md"
    if summary.exists():
        f = summary_fields(summary.read_text())
        status = f.get("Status", "")
        icon = {"completed": "✅", "failed": "❌"}.get(status, "⚠️" if status else "")
        cost = f.get("Estimated cost", "")
        label += f" · {icon} {status}" + (f" · {cost}" if cost else "")
    else:
        label += " · in progress or interrupted"
    return label


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def show_markdown(path: Path) -> None:
    text = path.read_text()
    text = re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S)  # hidden notes at the top of some files
    if text.lstrip().startswith(f"> **{DEGRADED}**"):
        banner, _, rest = text.partition("\n\n")
        problems = [line[4:] for line in banner.splitlines() if line.startswith("> - ")]
        st.warning("**Degraded report.** It failed its checks on every attempt and the run continued with it. "
                   "Verify anything you rely on.\n\n" + "\n".join(f"- {p}" for p in problems))
        text = rest
    st.markdown(text)
    st.download_button("Download", path.read_bytes(), file_name=path.name, mime="text/markdown",
                       key=f"dl-{path}")


def show_header(run: dict) -> None:
    folder = run["folder"]
    summary = folder / "summary.md" if folder else None
    if not summary or not summary.exists():
        st.caption("No run report for these files.")
        return
    f = summary_fields(summary.read_text())
    status = f.get("Status", "unknown")
    if status == "completed":
        st.success(f"Run completed in {f.get('Duration', '?')}.")
    else:
        error = re.search(r"\*\*Error:\*\*\s*(.+?)(?:\n\n|\Z)", summary.read_text(), re.S)
        st.error(f"**Run {status}.** " + (error.group(1).strip()[:800] if error else ""))

    cols = st.columns(5)
    cols[0].metric("Duration", f.get("Duration", "–"))
    cols[1].metric("Estimated cost", f.get("Estimated cost", "–"))
    cols[2].metric("Model calls", f.get("Model calls", "–"))
    cols[3].metric("Degraded reports", f.get("Degraded reports", "0"))
    cols[4].metric("Data issues", f.get("Data issues", "0"))


# Model name (as CrewAI reports it, without the provider prefix) -> provider.
PROVIDER_OF = (("deepseek", "deepseek"), ("gpt-", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
               ("claude", "anthropic"), ("gemini", "gemini"))


def infer_provider(events: Path) -> str | None:
    """Provider(s) of the agents' models, from events.jsonl (older runs have no LLM provider line)."""
    found = []
    for line in events.read_text().splitlines() if events.exists() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "llm_call" or e.get("task") == "(plain-English reviews)":
            continue
        model = str(e.get("model") or "").split("/")[-1].lower()
        name = next((p for prefix, p in PROVIDER_OF if model.startswith(prefix)), model or None)
        if name and name not in found:
            found.append(name)
    return ", ".join(found) or None


def show_run_report(run: dict) -> None:
    folder = run["folder"]
    summary = folder / "summary.md"
    events = folder / "events.jsonl"
    text = summary.read_text()
    if "**LLM provider:**" not in text:
        guess = infer_provider(events)
        text = re.sub(r"(\*\*Status:\*\*[^\n]*\n)",
                      lambda m: m.group(1) + f"**LLM provider:** {guess or 'not recorded'}"
                      + (" (inferred from the models used)" if guess else "") + "  \n", text, count=1)

    if events.exists():
        rows = []
        for line in events.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "llm_call":
                task = e.get("task") or "(other)"
                rows.append({"task": TASK_LABELS.get(task, task), "cost": e.get("est_cost_usd") or 0,
                             "tokens": (e.get("input_tokens") or 0) + (e.get("output_tokens") or 0)})
        if rows:
            df = pd.DataFrame(rows).groupby("task")[["cost", "tokens"]].sum().sort_values("cost", ascending=False)
            left, right = st.columns(2)
            left.caption("Estimated cost by task (US$)")
            left.bar_chart(df["cost"], horizontal=True)
            right.caption("Tokens by task")
            right.bar_chart(df["tokens"], horizontal=True)

    st.markdown(re.sub(r"^# Run report.*\n", "", text))

    rejected = sorted(folder.glob("rejected_*.md"))
    if rejected:
        st.subheader(f"Rejected reports ({len(rejected)})")
        st.caption("Reports that failed a check, saved with the reasons at the top.")
        for path in rejected:
            text = path.read_text()
            reasons = re.search(r"<!-- Rejected by guardrails.py:\n(.*?)\n-->", text, re.S)
            with st.expander(path.stem.replace("rejected_", "").replace("_", " ")):
                if reasons:
                    st.markdown("**Why it was rejected**\n\n" + reasons.group(1))
                st.code(re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S), language="markdown")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.title("Portfolio Advisor Reports")

runs = list_runs()
if not runs:
    st.info("No reports yet. Run the crew with `crewai run`, then refresh this page.")
    st.stop()

with st.sidebar:
    st.header("Runs")
    choice = st.radio("Run", runs, format_func=run_label, label_visibility="collapsed")
    st.caption(f"{len(runs)} run(s) in `output/`. Newest first.")
    if st.button("Refresh"):
        st.rerun()
    st.divider()
    st.caption("Research output, not investment advice. Verify before trading.")

st.subheader(run_label(choice))
show_header(choice)

available = [(label, find(choice["files"], ending)) for label, ending in REPORTS]
available = [(label, path) for label, path in available if path]
extra = ["Run report"] if choice["folder"] and (choice["folder"] / "summary.md").exists() else []

if not available and not extra:
    st.info("This run produced no reports.")
    st.stop()

if not find(choice["files"], "07_action_plan.md"):
    st.info("This run has no action plan; it stopped before the last task. See the Run report tab.")

tabs = st.tabs([("⚠️ " if path.read_text().lstrip().startswith(f"> **{DEGRADED}**") else "") + label
                for label, path in available] + extra)
for tab, (label, path) in zip(tabs, available):
    with tab:
        show_markdown(path)
if extra:
    with tabs[-1]:
        show_run_report(choice)
