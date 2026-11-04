"""Resume a failed or interrupted run from its last checkpoint, instead of starting over.

    uv run python scripts/resume_run.py            # resume the most recent checkpoint
    uv run python scripts/resume_run.py --list     # show the recent checkpoints
    uv run python scripts/resume_run.py <file>     # resume a specific checkpoint file

Tasks that already finished are not run again; the run continues with the first unfinished
task. Fix whatever made it fail first (e.g. a guardrail or a config value), then resume.

Why this script instead of `crewai checkpoint resume`: checkpoints refer to your custom tools
as Python modules named custom_tools.<name>, which only exist while CrewAI loads crew.jsonc.
This script registers those modules first, then restores the checkpoint. It also allows
CrewAI to rebuild Python objects from the checkpoint (CREWAI_DESERIALIZE_CALLBACKS=1); that is
only safe because these are your own local checkpoint files, so never point it at files
someone else gave you.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ["CREWAI_DESERIALIZE_CALLBACKS"] = "1"

try:  # same .env that `crewai run` loads
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

CHECKPOINTS = ROOT / ".checkpoints"


def checkpoints() -> list[Path]:
    return sorted(CHECKPOINTS.rglob("*.json"), key=lambda p: p.stat().st_mtime)


def register_custom_tools() -> None:
    """Make tools/<name>.py importable as custom_tools.<name>, as CrewAI names them in checkpoints."""
    pkg = types.ModuleType("custom_tools")
    pkg.__path__ = [str(ROOT / "tools")]
    sys.modules["custom_tools"] = pkg
    for f in sorted((ROOT / "tools").glob("*.py")):
        name = f"custom_tools.{f.stem}"
        spec = importlib.util.spec_from_file_location(name, f)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        setattr(pkg, f.stem, module)


def reattach_checks(crew) -> None:
    """Put each task's checks back. CrewAI's checkpoints don't keep them: a restored task comes
    back with no checks at all, so without this a resumed run would skip the safety check."""
    import importlib

    import guardrails

    definitions = {t["name"]: t for t in json.loads(guardrails._strip_jsonc((ROOT / "crew.jsonc").read_text()))["tasks"]}
    for task in crew.tasks:
        spec = definitions.get(task.name)
        if spec is None:
            sys.exit(f"Task {task.name} is no longer in crew.jsonc; start a fresh run with `crewai run` instead.")
        checks = []
        for g in spec.get("guardrails", []):
            if isinstance(g, dict) and "python" in g:
                module, _, attr = g["python"].rpartition(".")
                checks.append(getattr(importlib.import_module(module), attr))
            elif isinstance(g, str):
                from crewai.tasks.llm_guardrail import LLMGuardrail
                checks.append(LLMGuardrail(description=g, llm=task.agent.llm))
        task.guardrails = checks
        task._guardrails = checks
        task.guardrail = task._guardrail = None
        task.guardrail_max_retries = int(spec.get("guardrail_max_retries", 3))
        print(f"  checks restored for {task.name}: {[getattr(c, '__name__', type(c).__name__) for c in checks] or 'none'}")


def main() -> None:
    found = checkpoints()
    if not found:
        sys.exit("No checkpoints in .checkpoints/. Checkpoints are written after each finished task.")
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for p in found[-10:]:
            print(p.relative_to(ROOT))
        return
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else found[-1]
    print(f"Resuming from {target}")

    register_custom_tools()
    import guardrails  # noqa: F401  - registers the hooks and the checks
    import observability  # noqa: F401  - records this resumed run in output/runs/

    from crewai.crew import Crew
    from crewai.state.checkpoint_config import CheckpointConfig

    crew = Crew.from_checkpoint(CheckpointConfig(restore_from=str(target)))
    reattach_checks(crew)
    result = asyncio.run(crew.akickoff(inputs=None))
    print("\nResumed run finished.")
    print(str(getattr(result, "raw", result))[:500])


if __name__ == "__main__":
    main()
