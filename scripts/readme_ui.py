"""Document the report viewer in README.md and requirements.txt. Safe to run twice."""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = root / "README.md"
s = p.read_text()
SECTION = """## View the reports

A small local web app shows every run's reports in your browser:

```bash
uv add streamlit                 # once
uv run streamlit run ui/app.py   # opens http://localhost:8501
```

- **Runs**, newest first, with status and cost, in the sidebar.
- **Each report in its own tab:** the action plan first, then risk, technicals, holdings, watchlist, market, events and the raw data snapshot. Degraded reports are marked with ⚠️ and show what failed.
- **Run report tab:** cost and tokens per task, the full run summary, and any rejected reports with the reasons they failed.

It's read-only and only accepts connections from your own computer (`.streamlit/config.toml`), because the reports describe your holdings. Press **Refresh** after a new run.

---

"""
if "## View the reports" not in s:
    assert "## Choosing models" in s, "README section 'Choosing models' not found; not changed"
    s = s.replace("## Choosing models", SECTION + "## Choosing models", 1)
if "├── ui/app.py" not in s:
    s = s.replace("├── observability.py        # run reports in output/runs/\n",
                  "├── observability.py        # run reports in output/runs/\n"
                  "├── ui/app.py               # report viewer (uv run streamlit run ui/app.py)\n", 1)
p.write_text(s)

r = root / "requirements.txt"
req = r.read_text()
if "streamlit" not in req:
    r.write_text(req.rstrip("\n") + "\n\n# Report viewer (ui/app.py)\nstreamlit>=1.40\n")
print("README and requirements updated")
