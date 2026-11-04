"""Update README.md for the parallel order, new agents and models. Safe to run twice."""
import re
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else ".") / "README.md"
s = old = p.read_text()

HOW = """## How it works

A run has three stages. Every task writes a markdown report to `output/`.

1. **Events calendar** runs first, because every analysis task uses its dates.
2. **Four analysis tasks run at the same time:** market context, holdings fundamentals, watchlist screen and technicals.
3. **Risk** waits for all four, then the **action plan** reads everything and makes the final calls.

| Stage | Task | Agent | Model | Output |
|---|---|---|---|---|
| 1 | Events calendar: earnings, dividends, macro | Events Calendar Clerk | gpt-5-mini, low effort | `events_calendar.md` |
| 2 | Market context: regime, sector rotation, news | Market Intelligence Analyst | gpt-5-mini, medium | `01_market_context.md` |
| 2 | Holdings fundamental review | Fundamental Research Analyst | gpt-5, medium | `02_holdings_fundamentals.md` |
| 2 | Watchlist screen (shortlist of up to 5) | Watchlist Screener | gpt-5-mini, low | `03_watchlist_screen.md` |
| 2 | Technicals for every holding and watchlist name | Technical Analyst | gpt-5-mini, low | `04_technicals.md` |
| – | Covered calls (switched off; see below) | Options Income Strategist | gpt-5 | `05_covered_calls.md` |
| 3 | Risk: weights, beta, correlation, bias check | Risk Manager | gpt-5, medium | `06_risk.md` |
| 3 | Final action plan | Portfolio Manager | gpt-5, medium | `07_action_plan.md` |

Tasks marked `"async_execution": true` in `crew.jsonc` are the ones that run in parallel. Each report has a length cap, so later tasks read less. Market context has a research budget (at most 6 searches and 3 page reads).

**Covered calls** are switched off. `python3 scripts/toggle_covered_calls.py on` turns them back on (and `off` again); the task then runs after the parallel stage.

"""

if "A run has three stages." not in s:
    s = re.sub(r"## How it works\n\nThe crew runs eight tasks in order\..*?\n\n(?=Report files are named)", HOW, s, count=1, flags=re.S)

s = s.replace(
    "By default the four specialists use Sonnet and the Risk Manager and Portfolio Manager use Opus. ",
    "The current models are listed under How it works: gpt-5-mini where the work is mostly reading tool "
    "output, gpt-5 where judgement matters. In an agent file, `\"llm\"` can be a model name or an object "
    "such as `{\"model\": \"openai/gpt-5-mini\", \"reasoning_effort\": \"low\"}`. ", 1)

s = s.replace(
    "│   └── deepseek_flash.py   # standalone DeepSeek test script (not used by the crew)",
    "│   ├── deepseek_flash.py   # standalone DeepSeek test script (not used by the crew)\n"
    "│   ├── toggle_covered_calls.py  # switch the covered call task off/on\n"
    "│   └── optimize_crew.py    # applied the parallel order, models and length caps (kept for reference)", 1)

if s != old:
    p.write_text(s)
    print("README updated")
else:
    print("README already up to date")
