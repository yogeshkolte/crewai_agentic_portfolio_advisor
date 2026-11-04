"""Replace the README's opening with a search-friendly header, and list LICENSE. Safe to run twice."""
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else ".") / "README.md"
s = p.read_text()

HEADER = """# CrewAI Portfolio Advisor: multi-agent AI stock portfolio analysis

[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](https://www.python.org/)
[![Built with CrewAI](https://img.shields.io/badge/built%20with-CrewAI-orange)](https://www.crewai.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An AI portfolio advisor built with [CrewAI](https://www.crewai.com/). A team of AI agents reviews your stock holdings and watchlist (fundamental analysis, technical analysis, the earnings calendar, market news and portfolio risk) and writes a dated, prioritized action plan: hold, add, trim or exit for each position, with entry levels and stops.

> **Not investment advice.** This is a research tool. It never places trades. Verify everything before acting on it.

## Features

- **Multi-agent analysis:** eight specialized AI agents (events calendar, market context, fundamentals, watchlist screening, technical analysis, covered calls, risk management and a portfolio manager), coordinated by CrewAI.
- **Parallel execution:** four analysis tasks run at the same time to cut run time.
- **Free market data:** Yahoo Finance via yfinance, with retries, caching and backup sources (Finnhub for earnings dates, SEC EDGAR for financial statements).
- **Guardrails:** input validation, a check on every report, blocked trading tools, a website allowlist and prompt-injection screening.
- **Resilient runs:** a report that keeps failing its check is marked degraded instead of stopping the run, and a stopped run resumes from its last checkpoint.
- **Run reports:** time, tokens, estimated cost and check failures for every task in every run.
- **Any major model provider:** OpenAI, Anthropic or DeepSeek, chosen per agent.
- **Optional covered-call module:** strike, delta and expiry screening for covered-call income (off by default).

<!-- Add a screenshot of a sample action plan here, made from the example data (never your real holdings):
![Sample action plan](docs/sample-action-plan.png) -->

---
"""

if not s.startswith("# CrewAI Portfolio Advisor"):
    assert s.startswith("# Portfolio Management Crew"), "README doesn't start with the expected title; not changed"
    end = s.index("\n---\n") + len("\n---\n")
    s = HEADER + s[end:]

line = "├── requirements.txt\n"
if "├── LICENSE" not in s and line in s:
    s = s.replace(line, "├── LICENSE                 # MIT\n" + line, 1)

p.write_text(s)
print("README header done")
