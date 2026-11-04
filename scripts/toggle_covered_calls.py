"""Switch the covered call task off (default) or back on.

Run from the project root:
    python3 scripts/toggle_covered_calls.py off    # disable  (backups: crew.jsonc.bak, task_profiles.yaml.bak)
    python3 scripts/toggle_covered_calls.py on     # restore

Disabling:
  * comments the covered call task out of crew.jsonc with /* ... */ instead of deleting it,
  * comments it out of the risk and action-plan context lists,
  * in task_profiles.yaml: tells the action plan not to recommend options trades, and removes
    open short calls from the events calendar (the open-position step, the option-expirations
    table, and the "call spans earnings" conflict check).
Enabling reverses all of it. Safe to run twice. The covered call task's own text stays in
task_profiles.yaml either way.
"""
import re
import shutil
import sys
from pathlib import Path

mode = (sys.argv[1] if len(sys.argv) > 1 else "off").lower()
if mode not in ("on", "off"):
    sys.exit("usage: toggle_covered_calls.py off|on")
root = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
crew_path, text_path = root / "crew.jsonc", root / "task_profiles.yaml"
crew = crew_orig = crew_path.read_text()
text = text_orig = text_path.read_text()

START = "/* COVERED CALLS DISABLED: run scripts/toggle_covered_calls.py on to restore\n"
END = "    END COVERED CALLS DISABLED */\n"

# (text when covered calls are ON, text when OFF). Line breaks are written as \n here; in
# task_profiles.yaml each line of text is indented by 4 spaces, which _yaml() adds.
IND = "\n    "


def _yaml(text: str) -> str:
    return text.replace("\n", IND)


STEP4 = ("\n4. From {portfolio}, list each open option position's expiration and whether it spans "
         "an earnings or ex-dividend date.\n")
SWAPS = [
    # Action plan: no options trades without the analysis behind them.
    ("Include covered call actions.",
     "Covered call analysis is switched off for now: do not recommend any options trades."),
    ("5. Covered call actions.",
     "5. Covered call actions: write 'Switched off for now.'"),
    # Events calendar: drop open short calls.
    ("Flag conflicts that matter: a covered call whose expiry spans earnings or an ex-dividend date, "
     "and several holdings reporting in the same week.",
     "Flag conflicts that matter: several holdings reporting in the same week."),
    ("\n4. Option expirations table (Date | Symbol | Strike | Contracts | Spans earnings? Y/N)."
     "\n5. Macro calendar table (Date | Time ET | Event | Source)."
     "\n6. Conflicts and alerts list (write None if there are none).",
     "\n4. Macro calendar table (Date | Time ET | Event | Source)."
     "\n5. Conflicts and alerts list (write None if there are none)."),
]
# Where the open-position step sits in the events description when covered calls are on.
ANCHOR = "list each once with Status 'N/A (ETF)' and no date.\nDo not research"
STEP4, ANCHOR = _yaml(STEP4), _yaml(ANCHOR)
SWAPS = [(_yaml(on), _yaml(off)) for on, off in SWAPS]

if mode == "off":
    if START not in crew:
        # Comment out the whole task object, including its trailing comma.
        m = re.search(r'(\n)(    \{\n      "name": "covered_call_task".*?\n    \},\n)', crew, re.S)
        if not m:
            sys.exit("covered_call_task not found in crew.jsonc")
        crew = crew[:m.start(2)] + "    " + START + m.group(2) + END + crew[m.end(2):]
        # Comment it out of every context list that references it.
        crew = crew.replace(', "covered_call_task"', ' /*, "covered_call_task" */')
    text = text.replace(STEP4, IND)
    for on, off in SWAPS:
        text = text.replace(on, off)
else:
    crew = crew.replace("    " + START, "").replace(END, "")
    crew = crew.replace(' /*, "covered_call_task" */', ', "covered_call_task"')
    for on, off in SWAPS:
        text = text.replace(off, on)
    if STEP4 not in text and ANCHOR in text:
        text = text.replace(ANCHOR, ANCHOR.replace(IND + "Do not research", STEP4 + "Do not research"), 1)

if crew == crew_orig and text == text_orig:
    sys.exit(f"already {'disabled' if mode == 'off' else 'enabled'}")
for path, new, old in ((crew_path, crew, crew_orig), (text_path, text, text_orig)):
    if new != old:
        shutil.copy(path, path.with_name(path.name + ".bak"))
        path.write_text(new)
print(f"covered calls: {'disabled' if mode == 'off' else 'enabled'}")
