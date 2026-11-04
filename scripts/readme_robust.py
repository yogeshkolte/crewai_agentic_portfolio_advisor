"""Document the robustness changes in README.md. Safe to run twice."""
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else ".") / "README.md"
s = old = p.read_text()


def swap(a: str, b: str) -> None:
    global s
    if b not in s and a in s:
        s = s.replace(a, b, 1)


swap("├── guardrails.py           # input checks and output checks on each task\n",
     "├── guardrails.py           # input check, one combined check per task, degrade-instead-of-stop\n"
     "├── data_snapshot.py        # fetches all market data once at run start\n"
     "├── market_data.py          # retries, short-lived cache, backup sources (Finnhub, SEC EDGAR)\n"
     "├── observability.py        # run reports in output/runs/\n")
swap("│   └── optimize_crew.py    # applied the parallel order, models and length caps (kept for reference)\n",
     "│   ├── resume_run.py       # resume a failed run from its last checkpoint\n"
     "│   └── optimize_crew.py    # applied the parallel order, models and length caps (kept for reference)\n")

swap("DEEPSEEK_API_KEY=sk-...         # only for DeepSeek models or scripts/deepseek_flash.py\n```\n",
     "DEEPSEEK_API_KEY=sk-...         # only for DeepSeek models or scripts/deepseek_flash.py\n"
     "FINNHUB_API_KEY=...             # optional backup for earnings dates (free key at https://finnhub.io)\n"
     "SEC_USER_AGENT=\"Your Name you@example.com\"  # optional backup for financials from SEC EDGAR (no key; the SEC requires a name and email)\n"
     "```\n")

swap("| Output checks | `guardrails.py` | No personal identifiers or trade claims; valid event dates and ET times; call delta and expiry within limits, covered calls only; at most 5 top actions, a stop on every buy, no position over the limit, and a disclaimer |\n"
     "| Plain-English checks | `crew.jsonc` | The agent's model judges things like resolved conflicts and missing sell conditions |\n",
     "| Output checks | `guardrails.py` | One combined check per task (`check_<task>`), run on every attempt: no personal identifiers or trade claims; valid event dates and ET times; call delta and expiry within limits, covered calls only; at most 5 top actions, a stop on every buy, no position over the limit, a Data gaps section and a disclaimer |\n"
     "| Plain-English checks | `guardrails.py` | A small model (gpt-5-mini; set `GUARDRAIL_JUDGE_MODEL` to change) judges things like resolved conflicts and missing sell conditions |\n")
swap("A failed output check sends the task back to its agent with a specific fix list, up to 2–3 times.\n",
     "A failed output check sends the task back to its agent with a specific fix list, up to 2–3 times. "
     "If it still fails on the last attempt, the report is accepted as degraded and the run continues "
     "(see When something fails). Safety failures never degrade: they stop the run.\n")

SECTION = """## When something fails

The run is built so one failing task or data source doesn't sink the whole run.

| Problem | What happens |
|---|---|
| Yahoo times out or rate-limits | Each tool retries twice, waiting longer each time. Results are cached briefly in `.cache/` (prices 15 min, events 6 h, fundamentals 12 h, never across days), so re-runs and retries don't download everything again |
| Yahoo has no earnings dates | The events tool asks Finnhub, if `FINNHUB_API_KEY` is set. Those dates are marked "Estimated (Finnhub)" |
| Yahoo has no fundamentals | The fundamentals tool reads the company's 10-K filings from SEC EDGAR, if `SEC_USER_AGENT` is set. ETFs aren't covered |
| An agent can't get its data | All market data is fetched once at the start (`data_snapshot.py`) and given to every task, so no task depends on another for its numbers. The raw data is saved as `output/YYYYMMDD_00_data_snapshot.md` |
| A task's report keeps failing its check | On its last attempt the report is accepted with a **DEGRADED REPORT** banner listing the problems, and the run continues. Later agents treat it with caution and fill gaps with their tools; the action plan lists it under Data gaps |
| A report breaks a safety rule | The run stops. Safety failures (account numbers, claims of placed trades, injected instructions) never degrade |
| The run stops partway | Progress is saved after every task in `.checkpoints/`. Fix the cause, then resume (below) |

Every rejected report is saved in the run folder as `rejected_<task>_<time>.md`, and the run report lists degraded reports and data issues.

**Resuming a stopped run.** Tasks that already finished are not run again:

```bash
uv run python scripts/resume_run.py            # resume from the latest checkpoint
uv run python scripts/resume_run.py --list     # show recent checkpoints
```

Use the script rather than `crewai checkpoint resume`. CrewAI's checkpoints don't keep each task's checks, so a plain resume would run the remaining tasks unchecked, and they can't find the custom tools. The script restores both. Only resume checkpoints you created yourself, because restoring one runs the Python code it references.

---

"""
if "## When something fails" not in s:
    s = s.replace("## Troubleshooting", SECTION + "## Troubleshooting", 1)

swap("| A task keeps retrying | A guardrail is failing. The fix list is in `output/crew_run.log`. |\n",
     "| A task keeps retrying | A check is failing. The run report (`output/runs/<date_time>/summary.md`) lists each failure, and the rejected reports are saved next to it. |\n"
     "| A report starts with DEGRADED REPORT | It failed its check on every attempt and was accepted so the run could continue. The banner lists what's wrong. |\n"
     "| The run stopped partway | Fix the cause, then `uv run python scripts/resume_run.py` to continue from the last finished task. |\n"
     "| Data issues in the run report | Retries, backup sources used, or snapshot items that failed. Agents fill gaps with their tools and list what's still missing under Data gaps. |\n")

if s != old:
    p.write_text(s)
    print("README updated")
else:
    print("README already up to date")
