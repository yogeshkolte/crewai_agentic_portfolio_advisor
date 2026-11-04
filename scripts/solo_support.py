"""Support for scripts/run_task.py. Run from the project root; safe to run twice.

- observability.py: DeepSeek prices; a run folder only collects reports written during that run
  (so a one-task run doesn't pick up earlier reports from the same day).
- README.md: a "Run one task" section.
Backups: <file>.bak.
"""
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def edit(rel, pairs):
    p = root / rel
    s = old = p.read_text()
    for a, b, marker in pairs:
        if marker in s:
            continue
        assert s.count(a) == 1, f"{rel}: expected text not found: {a[:60]!r}"
        s = s.replace(a, b, 1)
    if s != old:
        shutil.copy(p, p.with_name(p.name + ".bak"))
        p.write_text(s)
        print(f"{rel}: updated")
    else:
        print(f"{rel}: already up to date")


edit("observability.py", [
    ('import threading\n', 'import threading\nimport time\n', 'import threading\nimport time\n'),
    ('    "gpt-5-mini": (0.25, 0.025, 2.00),\n',
     '    "gpt-5-mini": (0.25, 0.025, 2.00),\n'
     '    # DeepSeek list prices at peak hours; off-peak is half, so costs may be overstated.\n'
     '    "deepseek-flash": (0.30, 0.006, 1.20),\n'
     '    "deepseek-v4-pro": (1.32, 0.044, 3.96),\n',
     '"deepseek-flash":'),
    ('ROOT = Path(__file__).resolve().parent\n',
     'ROOT = Path(__file__).resolve().parent\n'
     '# When this process started: a run folder only collects reports written after this.\n'
     '_PROCESS_START = time.time()\n',
     '_PROCESS_START ='),
    ('        for md in OUTPUT.glob(pattern):\n            shutil.copy2(md, _run["dir"] / md.name)\n',
     '        for md in OUTPUT.glob(pattern):\n'
     '            if md.stat().st_mtime >= _PROCESS_START - 1:  # written by this run, not an earlier one\n'
     '                shutil.copy2(md, _run["dir"] / md.name)\n',
     '_PROCESS_START - 1'),
])

SECTION = """## Run one task

To test a change, or try another model on one agent, run a single task:

```bash
uv run python scripts/run_task.py --list                     # task short names
uv run python scripts/run_task.py events                     # the events calendar only
uv run python scripts/run_task.py events --model deepseek/deepseek-flash --effort low
```

It works like a full run (data snapshot, checks, saved report, run report) but runs one agent. `--model` and `--effort` apply to that run only; nothing in `crew.jsonc` or `agents/` changes. Tasks that read earlier reports, such as `risk` and `plan`, use the latest saved copies from `output/`, so run them after a full run.

---

"""
p = root / "README.md"
s = p.read_text()
if "## Run one task" not in s:
    assert "## View the reports" in s, "README section 'View the reports' not found"
    s = s.replace("## View the reports", SECTION + "## View the reports", 1)
    s = s.replace("│   ├── resume_run.py       # resume a failed run from its last checkpoint\n",
                  "│   ├── resume_run.py       # resume a failed run from its last checkpoint\n"
                  "│   ├── run_task.py         # run one task, optionally with another model\n", 1)
    shutil.copy(p, p.with_name("README.md.bak"))
    p.write_text(s)
    print("README.md: updated")
else:
    print("README.md: already up to date")
