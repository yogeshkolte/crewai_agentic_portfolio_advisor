"""Description and expected output of every task, read from task_profiles.yaml.

Each task in crew.jsonc says "description": "{description_<task>}" and
"expected_output": "{expected_output_<task>}". guardrails.validate_inputs checks the file
(check()) before the data snapshot and fills the inputs (fill_inputs()) after it, because task
text uses the snapshot's {data_*} values. CrewAI then puts the text into each task.

task_profiles.yaml layout:
    risk_assessment_task:    # the task name in crew.jsonc
      description: |-
        text...
      expected_output: |-
        text...
Placeholders such as {portfolio}, {data_events} and {current_date} are replaced with this
run's values.

Command line:
  uv run python task_profiles.py                        # check the file
  uv run python task_profiles.py risk_assessment_task   # print one task's text
"""
import re
import sys
from pathlib import Path

from agent_profiles import fill, load_entries, main, missing_sections, unknown_placeholders

ROOT = Path(__file__).resolve().parent
PROFILES = ROOT / "task_profiles.yaml"
FIELDS = ("description", "expected_output")


def task_names() -> list:
    """Tasks in crew.jsonc that take their text from this file, including a commented-out
    one (covered calls), so its text is kept ready for when it is switched back on."""
    return re.findall(r'"\{description_([A-Za-z0-9_]+)\}"', (ROOT / "crew.jsonc").read_text())


def load() -> dict:
    return load_entries(PROFILES, FIELDS)


def problems(profiles: dict, known: set | None = None) -> list:
    out = missing_sections(profiles, task_names(), FIELDS, "a task in crew.jsonc")
    if known is not None:
        out += unknown_placeholders(profiles, known)
    return out


def check(inputs: dict) -> None:
    """Stop the run early if task_profiles.yaml is broken (before the slow data fetch)."""
    errors = problems(load(), set(inputs))
    if errors:
        raise ValueError("task_profiles.yaml: " + "; ".join(errors))


def fill_inputs(inputs: dict) -> dict:
    """Add description_<task> and expected_output_<task> to the run inputs."""
    check(inputs)
    profiles = load()
    for name in task_names():
        for field in FIELDS:
            inputs[f"{field}_{name}"] = fill(profiles[name][field], inputs)
    return inputs


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main(load, problems, task_names, FIELDS, "task_profiles.yaml")
