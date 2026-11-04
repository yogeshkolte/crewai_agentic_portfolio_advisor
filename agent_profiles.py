"""Goal and backstory of every agent, read from agent_profiles.yaml.

Each agent file says "goal": "{goal_<agent>}" and "backstory": "{backstory_<agent>}". At the
start of a run, guardrails.validate_inputs calls fill_inputs(), which reads agent_profiles.yaml
and adds those two inputs per agent; CrewAI then puts the text into each agent.

agent_profiles.yaml layout:
    risk_manager:            # the agent's file name in agents/
      goal: |-
        text...
      backstory: |-
        text...
Placeholders such as {portfolio}, {watchlist}, {risk_profile} and {current_date} are replaced
with this run's values.

The helpers here (load_entries, unknown_placeholders, fill) are shared with task_profiles.py,
which does the same for task descriptions and expected outputs.

Command line:
  uv run python agent_profiles.py               # check the file: every agent has a goal and backstory
  uv run python agent_profiles.py risk_manager  # print one agent's goal and backstory
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES = ROOT / "agent_profiles.yaml"
FIELDS = ("goal", "backstory")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_entries(path: Path, fields: tuple) -> dict:
    """{name: {field: text}} from a YAML file of entries, each with exactly these text fields."""
    import yaml

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        # e.g. a line indented too little under "|-", or a stray tab
        raise ValueError(f"{path.name} is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected one entry per name at the top level")
    out = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}, {name}: expected the fields {', '.join(fields)} under it")
        unknown = set(entry) - set(fields)
        if unknown:
            raise ValueError(f"{path.name}, {name}: unknown field(s) {', '.join(map(str, sorted(unknown)))} "
                             f"(use {' and '.join(fields)})")
        out[str(name)] = {f: (entry[f].strip() if isinstance(entry.get(f), str) else entry.get(f))
                          for f in fields if f in entry}
    return out


def missing_sections(profiles: dict, names: list, fields: tuple, what: str) -> list:
    problems = []
    for name in names:
        for field in fields:
            value = profiles.get(name, {}).get(field)
            if not isinstance(value, str) or not value:
                problems.append(f"{name}: missing or empty '{field}'")
    for name in sorted(set(profiles) - set(names)):
        problems.append(f"'{name}' does not match {what}")
    return problems


def unknown_placeholders(profiles: dict, known: set) -> list:
    """{typo} placeholders the run has no value for (they would reach the model as-is)."""
    problems = []
    for name, sections in profiles.items():
        for field, text in sections.items():
            for ph in sorted(set(_PLACEHOLDER.findall(text or "")) - known):
                problems.append(f"{name} ({field}): unknown placeholder {{{ph}}}")
    return problems


def fill(text: str, inputs: dict) -> str:
    """Replace {key} with this run's value. CrewAI won't look inside inserted text, so this
    is where {portfolio}, {data_events} and the like get their values."""
    def sub(m):
        v = inputs.get(m.group(1))
        return str(v) if isinstance(v, (str, int, float)) else m.group(0)
    return _PLACEHOLDER.sub(sub, text)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def agent_names() -> list:
    """Agents that take their text from this file (their goal is "{goal_<name>}")."""
    return [f.stem for f in sorted((ROOT / "agents").glob("*.jsonc"))
            if "{goal_" + f.stem + "}" in f.read_text()]


def load() -> dict:
    return load_entries(PROFILES, FIELDS)


def problems(profiles: dict, known: set | None = None) -> list:
    out = missing_sections(profiles, agent_names(), FIELDS,
                           "an agent file in agents/ (or that file has its own goal)")
    if known is not None:
        out += unknown_placeholders(profiles, known)
    return out


def fill_inputs(inputs: dict) -> dict:
    """Add goal_<agent> and backstory_<agent> to the run inputs. Raises on a broken file."""
    profiles = load()
    errors = problems(profiles, set(inputs))
    if errors:
        raise ValueError("agent_profiles.yaml: " + "; ".join(errors))
    for name in agent_names():
        for field in FIELDS:
            inputs[f"{field}_{name}"] = fill(profiles[name][field], inputs)
    return inputs


def crew_input_names() -> set:
    """Input names declared in crew.jsonc, plus file_date (set at run start)."""
    import json
    from guardrails import _strip_jsonc
    return set(json.loads(_strip_jsonc((ROOT / "crew.jsonc").read_text()))["inputs"]) | {"file_date"}


def main(load_fn, problems_fn, names_fn, fields: tuple, filename: str) -> None:
    """Command line shared with task_profiles.py."""
    try:
        profiles = load_fn()
    except ValueError as e:
        sys.exit(str(e))
    if len(sys.argv) == 2:
        p = profiles.get(sys.argv[1]) or sys.exit(f"No '{sys.argv[1]}' entry in {filename}")
        for field in fields:
            print(f"--- {field} ---\n{p.get(field, '(missing)')}\n")
        return
    errors = problems_fn(profiles, crew_input_names())
    if errors:
        sys.exit(f"{filename} has problems:\n  " + "\n  ".join(errors))
    for name in names_fn():
        sizes = ", ".join(f"{f} {len(profiles[name][f]):5} chars" for f in fields)
        print(f"  {name:34} {sizes}")
    print(f"\n{filename}: OK")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main(load, problems, agent_names, FIELDS, "agent_profiles.yaml")
