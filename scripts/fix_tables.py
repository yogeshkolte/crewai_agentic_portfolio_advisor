"""Make guardrails.py read every Markdown table style, and save rejected reports.

Run from the project root: python3 scripts/fix_tables.py   (backup: guardrails.py.bak; safe to run twice)
"""
import shutil
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else ".") / "guardrails.py"
s = old = p.read_text()

OLD_TABLES = s[s.index("def _tables(text: str)"):s.index("def _col(row: dict[str, str], name: str)")]
NEW_TABLES = '''_SEP = re.compile(r"^\\|?\\s*:?-+:?\\s*(\\|\\s*:?-+:?\\s*)*\\|?$")


def _cells(line: str) -> list[str]:
    """Split one table line into cells, with or without the outer pipes."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _tables(text: str) -> list[list[dict[str, str]]]:
    """Find every markdown table in the report.

    Each table becomes a list of rows, and each row a dict of
    {lower-cased column header: cell text}. This lets the checks read a
    specific column (e.g. "delta") without caring about column order.

    Accepts every common Markdown table style: with or without the outer
    pipes ("| a | b |" or "a | b"), alignment colons in the separator
    ("|:--|--:|"), and bold headers ("**Symbol**"). An earlier version only
    accepted rows starting with "|", and failed a correct report written in
    the other style three times in a row.
    """
    tables, lines = [], text.splitlines()
    i = 0
    while i < len(lines) - 1:
        head, sep = lines[i].strip(), lines[i + 1].strip()
        # A table is a header line containing "|" followed by a separator line of dashes.
        if "|" in head and "-" in sep and _SEP.match(sep):
            headers = [re.sub(r"[*_`]", "", h).strip().lower() for h in _cells(head)]
            rows, i = [], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(dict(zip(headers, _cells(lines[i]))))
                i += 1
            tables.append(rows)
        else:
            i += 1
    return tables


def _table_with(text: str, *cols: str) -> list[dict[str, str]] | None:
    """Return the first table whose headers include all the given words.

    Matching is by substring, so asking for "status" also finds a column
    headed "Earnings status". A word can list alternatives separated by "/",
    e.g. "symbol/ticker". Returns None if no such table exists.
    """
    for t in _tables(text):
        if t and all(any(alt in h for alt in c.split("/") for h in t[0]) for c in cols):
            return t
    return None


def _headers_found(text: str) -> str:
    """Describe the tables present, for failure messages: 'Date, Ticker, Notes; Event, Date'."""
    found = ["; ".join([", ".join(t[0].keys())]) for t in _tables(text) if t]
    return " / ".join(found) if found else "none"


'''
if "_SEP = re.compile" not in s:
    s = s.replace(OLD_TABLES, NEW_TABLES, 1)

OLD_COL = '''def _col(row: dict[str, str], name: str) -> str:
    """Read a cell from a row by (part of) its column name; '' if absent."""
    for k, v in row.items():
        if name in k:
            return v
    return ""
'''
NEW_COL = '''def _col(row: dict[str, str], name: str) -> str:
    """Read a cell from a row by (part of) its column name; '' if absent.

    "symbol/ticker" reads whichever of the two columns exists.
    """
    for alt in name.split("/"):
        for k, v in row.items():
            if alt in k:
                return v
    return ""
'''
s = s.replace(OLD_COL, NEW_COL, 1)

OLD_FAIL = '''def _fail(problems: list[str]) -> Tuple[bool, Any]:
    """Build the failure result: one bullet per problem, read by the agent on retry."""
    return False, "Fix these problems and return the full corrected output:\\n- " + "\\n- ".join(problems)
'''
NEW_FAIL = '''def _save_rejected(result: Any, problems: list[str]) -> None:
    """Save a report that failed a check, so you can see exactly what was rejected.

    Written next to this run's summary (output/runs/<date_time>/rejected_<task>_<time>.md),
    or to output/rejected/ if run reports aren't set up. Never raises: a failure to
    save must not change the check's result.
    """
    try:
        try:
            import observability  # same module CrewAI loaded via crew.jsonc
            folder = observability._run.get("dir")
        except Exception:
            folder = None
        folder = Path(folder) if folder else ROOT / "output" / "rejected"
        folder.mkdir(parents=True, exist_ok=True)
        task = getattr(result, "name", None) or "task"
        path = folder / f"rejected_{task}_{datetime.now():%H%M%S}.md"
        path.write_text("<!-- Rejected by guardrails.py:\\n- " + "\\n- ".join(problems) + "\\n-->\\n\\n" + _text(result))
    except Exception:
        pass


def _fail(problems: list[str], result: Any = None) -> Tuple[bool, Any]:
    """Build the failure result: one bullet per problem, read by the agent on retry.

    If the report is passed in, it's saved first (see _save_rejected).
    """
    if result is not None:
        _save_rejected(result, problems)
    return False, "Fix these problems and return the full corrected output:\\n- " + "\\n- ".join(problems)
'''
s = s.replace(OLD_FAIL, NEW_FAIL, 1)
s = s.replace("return _fail(problems) if problems else (True, result)",
              "return _fail(problems, result) if problems else (True, result)")

# Events calendar: accept Ticker as well as Symbol, and say which tables were found.
s = s.replace('earnings = _table_with(text, "symbol", "status")',
              'earnings = _table_with(text, "symbol/ticker", "status")', 1)
s = s.replace('''            problems.append("Add the earnings table with Date, Symbol and Status columns, "
                            "or state 'No earnings in the window'.")''',
              '''            problems.append("Add the earnings table as a Markdown table with Date, Symbol and Status "
                            "columns, or state 'No earnings in the window'. Tables found (by column "
                            f"headers): {_headers_found(text)}.")''', 1)
s = s.replace('            sym = _col(r, "symbol") or "A row"', '            sym = _col(r, "symbol/ticker") or "A row"', 1)

if s == old:
    print("guardrails.py: already up to date")
else:
    shutil.copy(p, p.with_name("guardrails.py.bak"))
    p.write_text(s)
    print("guardrails.py: updated")
