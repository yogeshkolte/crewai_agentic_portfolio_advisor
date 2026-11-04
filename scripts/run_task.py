"""Run one task on its own, e.g. to test a change or try another model on one agent.

    uv run python scripts/run_task.py events                       # the events calendar only
    uv run python scripts/run_task.py events --model deepseek/deepseek-flash --effort low
    uv run python scripts/run_task.py --list                       # task names and short names
    uv run python scripts/run_task.py events --provider openai     # whole provider set from models.jsonc

Everything else works as in a full run: the data snapshot is fetched first, the task's checks
run (including degrade-instead-of-stop), its report is saved to output/ under today's date,
and a run report is written to output/runs/. Nothing in crew.jsonc or agents/ is changed;
--provider, --model and --effort only apply to this run.

Tasks that normally read earlier reports (e.g. risk) get the most recent saved copy of each
report from output/ instead, so run those after a full run. A task with no saved earlier
report runs without it and is told so.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:  # same .env that `crewai run` loads
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SHORT = {
    "events": "events_calendar_task", "market": "market_context_task",
    "holdings": "holdings_fundamental_review_task", "watchlist": "watchlist_screening_task",
    "technicals": "technical_analysis_task", "calls": "covered_call_task",
    "risk": "risk_assessment_task", "plan": "portfolio_action_plan_task",
}


def latest_report(output_file: str) -> Path | None:
    """Newest saved file for a task's output_file template, e.g. output/{file_date}_06_risk.md."""
    pattern = Path(output_file.replace("{file_date}", "*")).name
    found = sorted((ROOT / "output").glob(pattern), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one task of the crew.")
    ap.add_argument("task", nargs="?", help="task name or short name (see --list)")
    ap.add_argument("--model", help='model for this run only, e.g. "deepseek/deepseek-flash" or "openai/gpt-5-mini"')
    ap.add_argument("--effort", choices=["low", "medium", "high", "max"], help="reasoning effort, with --model")
    ap.add_argument("--provider", help="provider from models.jsonc for this run, e.g. openai, anthropic, gemini")
    ap.add_argument("--list", action="store_true", help="list the tasks")
    args = ap.parse_args()

    if args.provider:
        os.environ["MODEL_PROVIDER"] = args.provider  # read by models.py when the crew loads
    import json5
    from crewai import Crew, Process
    from crewai.project.crew_loader import load_crew

    loaded = load_crew("crew.jsonc")
    crew = loaded[0] if isinstance(loaded, tuple) else loaded
    by_name = {t.name: t for t in crew.tasks}

    if args.list or not args.task:
        for short, name in SHORT.items():
            if name in by_name:
                print(f"  {short:11} {name:34} agent: {by_name[name].agent.role}")
        return

    name = SHORT.get(args.task, args.task)
    if name not in by_name:
        sys.exit(f"Unknown or disabled task '{args.task}'. Use --list to see the tasks.")
    task = by_name[name]
    task.async_execution = False

    if args.model:
        settings = {"model": args.model}
        if args.effort:
            settings["reasoning_effort"] = args.effort
        import models
        task.agent.llm = models.make_llm(settings)
        print(f"{task.agent.role} uses {args.model}" + (f" ({args.effort} effort)" if args.effort else "") + " for this run.")

    # Earlier reports this task normally reads: use the latest saved copies instead.
    earlier = []
    for dep in task.context or []:
        path = latest_report(dep.output_file or "")
        if path:
            earlier.append(f"\n\nEARLIER REPORT: {dep.name} (saved as {path.name}):\n{path.read_text()}")
        else:
            earlier.append(f"\n\nEARLIER REPORT: {dep.name}: not available (no saved copy). Treat its content as N/A.")
    if earlier:
        import re
        # Neutralise {placeholder}-like text inside saved reports so CrewAI doesn't try to fill it.
        text = re.sub(r"\{([A-Za-z_][A-Za-z0-9_\-]*)\}", r"(\1)", "".join(earlier))
        print("Earlier reports: " + ", ".join(
            f"{d.name} -> {(latest_report(d.output_file or '') or Path('none')).name}" for d in task.context))
        task.description += text
        task.context = []

    solo = Crew(
        name=f"{crew.name}: {name}",
        agents=[task.agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
        before_kickoff_callbacks=crew.before_kickoff_callbacks,
        after_kickoff_callbacks=crew.after_kickoff_callbacks,
        output_log_file=crew.output_log_file,
    )
    inputs = dict(json5.load(open(ROOT / "crew.jsonc"))["inputs"])
    result = solo.kickoff(inputs=inputs)
    print(f"\nSaved: {task.output_file}")
    print(str(result.raw)[:300])


if __name__ == "__main__":
    main()
