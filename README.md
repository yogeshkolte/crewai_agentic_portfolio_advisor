# CrewAI Portfolio Advisor: multi-agent AI stock portfolio analysis

[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](https://www.python.org/)
[![Built with CrewAI](https://img.shields.io/badge/built%20with-CrewAI-orange)](https://www.crewai.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An AI portfolio advisor built with [CrewAI](https://www.crewai.com/). A team of AI agents reviews your stock holdings and watchlist (fundamental analysis, technical analysis, the earnings calendar, market news and portfolio risk) and writes a dated, prioritized action plan: hold, add, trim or exit for each position, with entry levels and stops.

> **Not investment advice.** This is a research tool. It never places trades. Verify everything before acting on it.

## Features

- **Multi-agent analysis:** eight specialized AI agents (events calendar, market context, fundamentals, watchlist screening, technical analysis, covered calls, risk management and a portfolio manager), coordinated by CrewAI.
- **Parallel execution:** four analysis tasks run at the same time to cut run time.
- **Free market data:** Yahoo Finance via yfinance, with retries, caching and backup sources (Finnhub for earnings dates, SEC EDGAR for financial statements).
- **Guardrails:** input validation, a check on every report, blocked trading tools, a website allowlist (also given to the agents in their prompt) and prompt-injection screening.
- **Resilient runs:** a report that keeps failing its check is marked degraded instead of stopping the run, and a stopped run resumes from its last checkpoint.
- **Run reports:** time, tokens, estimated cost and check failures for every task in every run.
- **Choose your model provider:** DeepSeek, OpenAI, Anthropic or Gemini with one setting, or mix providers per agent.
- **Optional covered-call module:** strike, delta and expiry screening for covered-call income (off by default).

<!-- Add a screenshot of a sample action plan here, made from the example data (never your real holdings):
![Sample action plan](docs/sample-action-plan.png) -->

---

## How it works

A run has three stages. Every task writes a markdown report to `output/`.

1. **Events calendar** runs first, because every analysis task uses its dates.
2. **Four analysis tasks run at the same time:** market context, holdings fundamentals, watchlist screen and technicals.
3. **Risk** waits for all four, then the **action plan** reads everything and makes the final calls.

| Stage | Task                                            | Agent                        | Model (default provider)   | Output                        |
| ----- | ----------------------------------------------- | ---------------------------- | -------------------------- | ----------------------------- |
| 1     | Events calendar: earnings, dividends, macro     | Events Calendar Clerk        | DeepSeek Flash, low effort | `events_calendar.md`          |
| 2     | Market context: regime, sector rotation, news   | Market Intelligence Analyst  | DeepSeek Flash, high       | `01_market_context.md`        |
| 2     | Holdings fundamental review                     | Fundamental Research Analyst | DeepSeek V4 Pro, high      | `02_holdings_fundamentals.md` |
| 2     | Watchlist screen (portfolio fit, up to 5)       | Watchlist Screener           | DeepSeek Flash, high       | `03_watchlist_screen.md`      |
| 2     | Technicals for every holding and watchlist name | Technical Analyst            | DeepSeek Flash, high       | `04_technicals.md`            |
| –     | Covered calls (switched off; see below)         | Options Income Strategist    | gpt-5                      | `05_covered_calls.md`         |
| 3     | Risk: weights, beta, correlation, bias check    | Risk Manager                 | DeepSeek V4 Pro, high      | `06_risk.md`                  |
| 3     | Final action plan                               | Portfolio Manager            | DeepSeek V4 Pro, high      | `07_action_plan.md`           |

Models come from `models.jsonc`; see [Choosing models](#choosing-models) to use OpenAI, Claude or Gemini instead.

Tasks marked `"async_execution": true` in `crew.jsonc` are the ones that run in parallel. Each report has a length cap, so later tasks read less. Market context has a research budget (at most 6 searches and 3 page reads).

**Covered calls** are switched off. `python3 scripts/toggle_covered_calls.py on` turns them back on (and `off` again); the task then runs after the parallel stage. The script edits `crew.jsonc` and the events and action-plan text in `task_profiles.yaml`.

Report files are named with the run date, e.g. `20261027_01_market_context.md`, so each day's reports sit side by side. A full log of every run goes to `output/crew_run.log`.

For how CrewAI loads and runs the agents, how data flows between tasks, and why each setting has its value, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Project structure

```
crewai_agentic_portfolio_advisor/
├── crew.jsonc              # crew settings, inputs, risk limits, and all 8 tasks
├── models.jsonc            # model provider and the model for each agent
├── chat/                   # always-running stock chat: agent (server.py) + CopilotKit page (web/)
├── models.py               # reads models.jsonc; `uv run python models.py` shows the choice
├── agents/                 # one file per agent: role, tools, limits
├── agent_profiles.yaml       # goal and backstory of every agent, in one file
├── agent_profiles.py       # reads agent_profiles.yaml at run start
├── task_profiles.yaml        # description and expected output of every task, in one file
├── task_profiles.py        # reads task_profiles.yaml at run start
├── docs/ARCHITECTURE.md    # how the crew runs inside CrewAI, with diagrams
├── tools/                  # custom tools (Yahoo Finance via yfinance)
│   ├── events_calendar.py
│   ├── fundamentals.py
│   ├── options_chain.py
│   ├── portfolio_analytics.py
│   └── price_indicators.py
├── guardrails.py           # input check, one combined check per task, degrade-instead-of-stop
├── data_snapshot.py        # fetches all market data once at run start
├── market_internals.py     # breadth, credit spreads (FRED), relative leadership for the market briefing
├── track_record.py         # scores past plans and watchlist scores vs SPY, fed back into the next run
├── market_data.py          # retries, short-lived cache, backup sources (Finnhub, SEC EDGAR)
├── observability.py        # run reports in output/runs/
├── ui/app.py               # report viewer (uv run streamlit run ui/app.py)
├── hooks.py                # blocks trading tools, limits websites, flags prompt injection
├── data/
│   ├── portfolio.example.csv
│   ├── watchlist.example.csv
│   └── cash.example.csv
├── scripts/
│   ├── deepseek_flash.py   # standalone DeepSeek test script (not used by the crew)
│   ├── toggle_covered_calls.py  # switch the covered call task off/on
│   ├── resume_run.py       # resume a failed run from its last checkpoint
│   ├── run_task.py         # run one task, optionally with another model
│   ├── premarket_snapshot.py  # pre-market prices and levels for a watchlist (for a daily report)
│   └── optimize_crew.py    # applied the parallel order, models and length caps (kept for reference)
├── LICENSE                 # MIT
├── requirements.txt
└── README.md
```

Keep `guardrails.py` and `hooks.py` in the project root, and keep tool file names unchanged: each `"custom:<name>"` in an agent file loads `tools/<name>.py`.

---

## Setup

### 1. Install uv and CrewAI

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install 'crewai[tools]'
```

### 2. Get the project and install its dependencies

```bash
git clone https://github.com/<your-account>/crewai_agentic_portfolio_advisor.git
cd crewai_agentic_portfolio_advisor
uv sync
```

`uv sync` creates the project's own environment (`.venv`) and installs everything listed in `pyproject.toml`, at the exact versions in `uv.lock`. `crewai install` does the same. Run it before the first `crewai run`, which uses that environment but never installs anything itself.

To use Claude or Gemini models (see [Choosing models](#choosing-models)), add the provider's package once:

```bash
uv add "crewai[tools,anthropic]"       # Claude
uv add "crewai[tools,google-genai]"    # Gemini
```

Add any other package the same way, with `uv add <package>`. `pip install` would put it outside `.venv`, where `crewai run` doesn't look.

If you move or rename the project folder, rebuild the environment, since `.venv` remembers the old path:

```bash
rm -rf .venv
uv sync
```

`requirements.txt` lists the same packages for a plain virtualenv (`pip install -r requirements.txt`), which is enough for the scripts and the report viewer. `crewai run` itself needs uv.

### 3. Set API keys

Create a `.env` file in the project folder. `crewai run` loads it automatically.

```bash
DEEPSEEK_API_KEY=sk-...         # agents use DeepSeek models (the default provider)
OPENAI_API_KEY=sk-...           # plain-English checks (gpt-5-mini) and covered calls (gpt-5)
# ANTHROPIC_API_KEY=sk-ant-...  # only for the anthropic provider
# GEMINI_API_KEY=...            # only for the gemini provider
# MODEL_PROVIDER=openai         # optional: use another provider from models.jsonc
SERPER_API_KEY=...              # web search, from https://serper.dev
FINNHUB_API_KEY=...             # optional backup for earnings dates (free key at https://finnhub.io)
SEC_USER_AGENT="Your Name you@example.com"  # optional backup for financials from SEC EDGAR (no key; the SEC requires a name and email)
```

Keep `.env` out of git.

### 4. Add your holdings and watchlist

```bash
cp data/portfolio.example.csv data/portfolio.csv
cp data/watchlist.example.csv data/watchlist.csv
cp data/cash.example.csv data/cash.csv
```

Then edit the files with your real data.

**`data/portfolio.csv`** has one row per holding. Fill in the option columns only for a short call you hold against the shares.

| Column        | Example      | Notes                                                  |
| ------------- | ------------ | ------------------------------------------------------ |
| `symbol`      | `QQQ`        | Required                                               |
| `shares`      | `300`        | Required, positive                                     |
| `avg_cost`    | `38.50`      | Required, positive                                     |
| `sector`      | `Materials`  | Optional                                               |
| `option_type` | `call`       | Optional                                               |
| `strike`      | `48`         | Optional                                               |
| `expiry`      | `2026-12-18` | Optional, YYYY-MM-DD                                   |
| `contracts`   | `3`          | Optional; must be covered by shares (100 per contract) |

**`data/watchlist.csv`** has the columns `symbol` and `reason`.

**`data/cash.csv`** has the columns `account` and `amount`: one row per account, in dollars, summed for the cash-floor check (`min_cash_pct`). Label the accounts (`Brokerage`, `IRA`); the run stops if the label looks like an account number. The run stops if the file is missing; write a row with amount `0` if you hold no cash.

**`data/track_record/`** is written by the run itself (git-ignored): a ledger of every past top action and watchlist score, read from the reports in `output/`. See [Track record](#track-record) below.

### 5. Set your risk limits

Edit `inputs.risk_profile` in `crew.jsonc`. The guardrails read these numbers, so this is the one place to change them.

| Setting                                | Default    | Meaning                                         |
| -------------------------------------- | ---------- | ----------------------------------------------- |
| `max_position_pct`                     | 10         | Largest position, % of portfolio                |
| `max_sector_pct`                       | 30         | Largest sector, % of portfolio                  |
| `min_cash_pct`                         | 5          | Minimum cash                                    |
| `max_new_trades_per_week`              | 3          | Overtrading limit                               |
| `hurdle_return_pct`                    | 10         | Minimum expected annual return for a holding's valuation to count as adequate |
| `covered_calls.target_delta`           | 0.20–0.30  | Preferred call delta                            |
| `covered_calls.hard_delta_limit`       | 0.10–0.40  | Calls outside this are rejected                 |
| `covered_calls.target_dte` / `max_dte` | 30–45 / 90 | Preferred and maximum days to expiry            |
| `covered_calls.allow_through_earnings` | false      | Reject calls spanning earnings unless justified |
| `known_biases`                         | list       | Habits the Risk Manager checks for              |

---

## Run

```bash
crewai run
```

The run prints plain progress output and exits when the action plan is done. That comes from `CREWAI_DMN=1` in `.env`; without it, `crewai run` shows a full-screen progress display that stays open until you quit it.

The final action plan is saved automatically. To review it before it's saved, set `"human_input": true` on `portfolio_action_plan_task` in `crew.jsonc`: the plan then waits in the terminal until you press Enter to accept it or type feedback for a revision (the run sits idle until you do).

Reports are saved as `output/YYYYMMDD_<name>.md`. A second run on the same day replaces that day's files; every run's copies are also kept under `output/runs/` (see Run reports).

---

## Run one task

To test a change, or try another model on one agent, run a single task:

```bash
uv run python scripts/run_task.py --list                     # task short names
uv run python scripts/run_task.py events                     # the events calendar only
uv run python scripts/run_task.py events --model deepseek/deepseek-flash --effort low
```

It works like a full run (data snapshot, checks, saved report, run report) but runs one agent. `--model` and `--effort` apply to that run only; nothing in `crew.jsonc` or `agents/` changes. Tasks that read earlier reports, such as `risk` and `plan`, use the latest saved copies from `output/`, so run them after a full run.

---

## View the reports

A small local web app shows every run's reports in your browser. **It opens by itself when a run ends**: the run starts the viewer in the background if it isn't running and opens http://localhost:8501 on the new run. The viewer keeps running afterwards; stop it with `pkill -f "streamlit run ui/app.py"`. Set `REPORT_VIEWER=off` in `.env` to skip this. To open it by hand:

```bash
uv run streamlit run ui/app.py   # opens http://localhost:8501
```

- **Runs**, newest first, with status and cost, in the sidebar.
- **Each report in its own tab:** the action plan first, then risk, technicals, holdings, watchlist, market, events and the raw data snapshot. Degraded reports are marked with ⚠️ and show what failed.
- **Run report tab:** cost and tokens per task, the full run summary, and any rejected reports with the reasons they failed.

It's read-only and only accepts connections from your own computer (`.streamlit/config.toml`), because the reports describe your holdings. Press **Refresh** after a new run.

---

## Editing agents and tasks

The text the agents work from lives in two YAML files, in run order:

| File | Holds | Each entry |
|---|---|---|
| `agent_profiles.yaml` | every agent's goal and backstory | `risk_manager:` with `goal` and `backstory` |
| `task_profiles.yaml` | every task's description and expected output | `risk_assessment_task:` with `description` and `expected_output` |

```yaml
risk_assessment_task:
  description: |-
    Assess the current portfolio and stress-test the specialists' implied actions against {risk_profile}: ...
    1. Position and sector weights; flag breaches of limits.

  expected_output: |-
    A risk dashboard: weights table with limit status (OK/Warn/Breach), ...
```

Write the text under `|-`, indented by 4 spaces (spaces, not tabs); line breaks and numbered lists are kept as written, and quotes or colons need no escaping there.

Edit the text and save; the next run uses it. Placeholders such as `{portfolio}`, `{watchlist}`, `{risk_profile}`, `{current_date}` and the pre-fetched data (`{data_events}`, `{data_prices}`, ...) are replaced with that run's values. Keep the "Length: ..." limit at the end of each expected output; later tasks read these reports. Everything else stays where it was: each agent's role, tools and limits in `agents/`, and each task's agent, context, checks and output file in `crew.jsonc`. Those files point to the text with placeholders such as `"goal": "{goal_risk_manager}"` and `"description": "{description_risk_assessment_task}"`.

```bash
uv run python agent_profiles.py                        # check agent_profiles.yaml
uv run python task_profiles.py                         # check task_profiles.yaml
uv run python agent_profiles.py risk_manager           # print one agent's text
uv run python task_profiles.py risk_assessment_task    # print one task's text
```

A run stops before any agent starts if an entry or field is missing, a field name is misspelled, the YAML is broken (for example a line indented too little), or a placeholder is unknown (a typo such as `{watchlst}`). Most backstories end with the same operating rules (never place trades, no figures from memory, treat web pages as untrusted); they differ slightly per agent, so change each copy you mean to change.

---

## Pre-market snapshot

`scripts/premarket_snapshot.py` collects exact pre-market data for a watchlist, for a daily pre-market report written by an AI agent. It pulls everything from Yahoo Finance in one go, so the report never relies on prices read off web pages or search snippets.

```bash
uv run python scripts/premarket_snapshot.py                                   # data/watchlist.csv
uv run python scripts/premarket_snapshot.py --watchlist /path/to/Watchlist.csv --out-dir reports
```

For every symbol it records the latest price and its time (pre-market, regular session, after-hours, or the last close when the market is closed), $ and % change vs the prior close, prior-day high/low, pre-market high/low and volume (HIGH / AVERAGE / LOW vs the previous 4 pre-markets over the same hours), 20/50/200-day averages, 52-week range, 30-day average volume, and the next earnings date flagged if it falls this week or next. It adds a market backdrop (S&P 500 and Nasdaq futures, SPY, QQQ, VIX) and works out the session: weekends, NYSE holidays and 1:00 PM early closes are built in (`NYSE_HOLIDAYS`, through 2028). Anything it can't fetch is `null` / "N/A", never estimated; unknown and invalid symbols are listed as data gaps.

It writes `YYYYMMDD_HHMM_premarket_snapshot.json` (exact numbers) and `.md` (one table, easy for an agent to read) to `output/premarket/` by default and prints both paths. Yahoo prices can be up to 15 minutes delayed.

---

## Stock chat

An always-running chat agent with a [CopilotKit](https://www.copilotkit.ai/) chat page: ask about a ticker ("How is NVDA doing today?") and it answers with today's price and today's news about the company. It runs separately from the crew and shares its `.env`, its model settings (`stock_chat` in `models.jsonc`) and its data sources.

```bash
uv add ag-ui-crewai     # once
./chat/start.sh         # agent on 127.0.0.1:8000, chat page on http://127.0.0.1:3000
```

Needs Node.js 20 or later. Details in [chat/README.md](chat/README.md).

---

## Choosing models

Models are picked in one file, `models.jsonc`, not in the agent files. Each agent has a tier (fast, research or strong), and each provider names a model for each tier:

| Provider             | Fast (events, watchlist, technicals) | Research (market)      | Strong (fundamentals, risk, plan) | Key                 | Install                         |
| -------------------- | ------------------------------------ | ---------------------- | --------------------------------- | ------------------- | ------------------------------- |
| `deepseek` (default) | `deepseek-flash`, low                | `deepseek-flash`, high | `deepseek-v4-pro`, high           | `DEEPSEEK_API_KEY`  | included                        |
| `openai`             | `gpt-5-mini`, low                    | `gpt-5-mini`, medium   | `gpt-5`, medium                   | `OPENAI_API_KEY`    | included                        |
| `anthropic`          | `claude-haiku-5-5`                   | `claude-haiku-5-5`     | `claude-sonnet-5-5`               | `ANTHROPIC_API_KEY` | `uv add "crewai[anthropic]"`    |
| `gemini`             | `gemini-3.5-flash-lite`              | `gemini-3.8-flash`     | `gemini-4-argon`                  | `GEMINI_API_KEY`    | `uv add "crewai[google-genai]"` |

The plain-English report checks use each provider's small model ("judge"); under `deepseek` they stay on `openai/gpt-5-mini`, and covered calls use `openai/gpt-5`.

```bash
uv run python models.py                     # each agent's model, and any missing API key
uv run python models.py use anthropic       # make anthropic the default provider
MODEL_PROVIDER=openai crewai run            # one run on another provider
uv run python scripts/run_task.py plan --provider gemini   # one task on another provider
```

You can also set `MODEL_PROVIDER=openai` in `.env`. To mix providers, name a model for one agent under `"agents"` in `models.jsonc`, for example `"portfolio_manager": "anthropic/claude-opus-5-5"`; that wins whatever the provider is. A model is a string (`"openai/gpt-5"`) or an object with a reasoning effort (`{"model": "deepseek/deepseek-flash", "reasoning_effort": "low"}`); effort is sent only to OpenAI and DeepSeek (DeepSeek accepts `low`, `high`, `max`). `GUARDRAIL_JUDGE_MODEL` in `.env` overrides the review model. A run stops before any agent starts if a key it needs is missing.

Other providers CrewAI supports (Azure, Bedrock, Ollama for local models) can be added as a new entry under `"providers"`. Estimated costs in run reports come from the `PRICES` table in `observability.py`; add a model there to see its cost.

---|---|---|
| OpenAI | `openai/gpt-5`, `openai/gpt-5-mini` | `OPENAI_API_KEY` |
| DeepSeek | `deepseek/deepseek-flash`, `deepseek/deepseek-v4-pro` | `DEEPSEEK_API_KEY` |

The current models are listed under How it works: DeepSeek Flash where the work is mostly reading tool output, DeepSeek V4 Pro where judgement matters, and gpt-5 for covered calls (switched off). The earlier OpenAI settings are kept as comments in each agent file. In an agent file, `"llm"` can be a model name or an object such as `{"model": "deepseek/deepseek-flash", "reasoning_effort": "low"}`; DeepSeek accepts `low`, `high` and `max`. Plain-English guardrail checks run on `GUARDRAIL_JUDGE_MODEL` (default `openai/gpt-5-mini`), set in `.env`. Confirm OpenAI model IDs with your key before relying on them:

```bash
python -c "from openai import OpenAI; print(sorted(m.id for m in OpenAI().models.list()))"
```

---

## Data sources

| Agent                                               | Sources                                                  |
| --------------------------------------------------- | -------------------------------------------------------- |
| Market Intelligence Analyst                         | Yahoo Finance, Google search (Serper), approved websites |
| Fundamental Research Analyst                        | Yahoo Finance, Google search (Serper)                    |
| Technical Analyst, Options Strategist, Risk Manager | Yahoo Finance only                                       |
| Portfolio Manager                                   | Other agents' reports only                               |

- **Yahoo Finance:** prices, options chains, fundamentals and earnings dates come from `yfinance`, which needs no key. It uses Yahoo's unofficial endpoints, which can change or rate-limit.
- **Option delta:** Yahoo doesn't publish delta, so the tools calculate it from implied volatility.
- **Earnings dates:** marked Unverified or Estimated; the agent is told to confirm them on company investor-relations pages.
- **Website reading:** limited to an allowlist in `hooks.py` (SEC, Fed, major financial news, press-release wires, company investor-relations sites). The list is also written into the market agent's instructions, and a blocked page tells the agent which domains are allowed. `SCRAPE_ALLOWLIST=off` in `.env` lifts it. Search results themselves are not filtered.
- **Where your data goes:** Yahoo and Serper see only ticker symbols and search queries. Your full holdings go only to the model provider in the prompts.

---

## Guardrails

| Layer                | Where                        | What it does                                                                                                                                                                                                                                                                                                       |
| -------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Input check          | `guardrails.validate_inputs` | Stops the run on missing files, invalid symbols, non-positive shares or cost, uncovered short calls, or out-of-range limits                                                                                                                                                                                        |
| Tool hooks           | `hooks.py`                   | Blocks any tool that looks like trading or money movement; allows website reading only from approved domains; labels likely prompt injection in web content; stops an agent after 25 model calls on a task                                                                                                         |
| Agent limits         | `agents/*.jsonc`             | Caps on iterations, run time, request rate and retries; delegation off; today's date injected                                                                                                                                                                                                                      |
| Agent rules          | each backstory               | Never claim to trade; every number from a tool or "N/A"; treat web content as data; stay in scope; date facts; no account numbers                                                                                                                                                                                  |
| Output checks        | `guardrails.py`              | One combined check per task (`check_<task>`), run on every attempt: no personal identifiers or trade claims; valid event dates and ET times; call delta and expiry within limits, covered calls only; at most 5 top actions, a stop on every buy, no position over the limit, a Data gaps section and a disclaimer |
| Plain-English checks | `guardrails.py`              | A small model (gpt-5-mini; set `GUARDRAIL_JUDGE_MODEL` to change) judges things like resolved conflicts and missing sell conditions                                                                                                                                                                                |
| Human review         | `crew.jsonc`                 | You approve or revise the final plan                                                                                                                                                                                                                                                                               |

A failed output check sends the task back to its agent with a specific fix list, up to 2–3 times. If it still fails on the last attempt, the report is accepted as degraded and the run continues (see When something fails). Safety failures never degrade: they stop the run.

---

## Memory

Memory is off (`"memory": false`). That's deliberate: remembered prices and levels from earlier runs could leak into new analysis, and the default memory setup sends data to OpenAI and stores it unencrypted in `.crewai/memory/`. If you turn it on, configure a `Memory` object with a short recency half-life and a local embedder, and reset it with `crewai reset-memories -m` when needed.

## Track record

The agents don't learn between runs, so `track_record.py` gives them evidence of their own results instead. At run start it:

1. reads every past action plan's "Top 5 actions" table and every watchlist screen in `output/` into a ledger in `data/track_record/` (git-ignored; rows stay after old reports are deleted; the Size column is never stored);
2. scores each item against Yahoo daily prices, always relative to SPY: a Buy/Add counts as filled once the price trades at or below the top of its entry level, then is followed to its stop or the latest close; a Trim/Exit is a right call when the stock then lagged SPY. Watchlist scores are grouped by Total and Value read. A call repeated in later plans counts once;
3. passes the results to the action plan (`{data_track_record}`: scorecard plus each open item of the last plan with its status) and to the watchlist screener (`{data_track_record_scores}`).

A group only becomes "usable", and allowed to change how levels, stops, conviction or scores are set, once 10 items from 10 different names have a month of prices. Until then the plan still has to say, for each open item of the last plan, whether it still stands. Check it any time with `uv run python track_record.py`.

---

## Run reports

Every run gets its own folder, `output/runs/<date_time>/`, written by `observability.py`:

| File           | What's in it                                                                                                                                                                                                  |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `summary.md`   | Per-task time, model calls, tokens, estimated cost, tool calls and guardrail failures; tool stats; every guardrail failure with its message; anything `hooks.py` blocked; the slowest and most expensive task |
| `events.jsonl` | One line per event (model call, tool call, guardrail check, task start/finish) for deeper digging. Load it with `pd.read_json(path, lines=True)`                                                              |
| `*.md`         | A copy of that run's task reports, so the next run doesn't overwrite them                                                                                                                                     |

A failed run still gets a report, written as the program exits, with the error at the top.

- **Costs** are estimates from the `PRICES` table at the top of `observability.py` (US$ per 1M tokens). Check them against your provider's price page and add every model you use; others show `n/a`.
- **Privacy:** everything stays on your machine. Prompts and full responses aren't saved, and tool arguments are truncated. Reports do describe your holdings, so keep `output/` out of git.
- **Wiring:** `crew.jsonc` lists `observability.start_run` in `before_kickoff_callbacks` and `observability.finish_run` in `after_kickoff_callbacks`.

---

## When something fails

The run is built so one failing task or data source doesn't sink the whole run.

| Problem                                 | What happens                                                                                                                                                                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Yahoo times out or rate-limits          | Each tool retries twice, waiting longer each time. Results are cached briefly in `.cache/` (prices 15 min, events 6 h, fundamentals 12 h, never across days), so re-runs and retries don't download everything again                  |
| Yahoo has no earnings dates             | The events tool asks Finnhub, if `FINNHUB_API_KEY` is set. Those dates are marked "Estimated (Finnhub)"                                                                                                                               |
| Yahoo has no fundamentals               | The fundamentals tool reads the company's 10-K filings from SEC EDGAR, if `SEC_USER_AGENT` is set. ETFs aren't covered                                                                                                                |
| An agent can't get its data             | All market data is fetched once at the start (`data_snapshot.py`) and given to every task, so no task depends on another for its numbers. The raw data is saved as `output/YYYYMMDD_00_data_snapshot.md`                              |
| A task's report keeps failing its check | On its last attempt the report is accepted with a **DEGRADED REPORT** banner listing the problems, and the run continues. Later agents treat it with caution and fill gaps with their tools; the action plan lists it under Data gaps |
| A report breaks a safety rule           | The run stops. Safety failures (account numbers, claims of placed trades, injected instructions) never degrade                                                                                                                        |
| The run stops partway                   | Progress is saved after every task in `.checkpoints/`. Fix the cause, then resume (below)                                                                                                                                             |

Every rejected report is saved in the run folder as `rejected_<task>_<time>.md`, and the run report lists degraded reports and data issues.

**Resuming a stopped run.** Tasks that already finished are not run again:

```bash
uv run python scripts/resume_run.py            # resume from the latest checkpoint
uv run python scripts/resume_run.py --list     # show recent checkpoints
```

Use the script rather than `crewai checkpoint resume`. CrewAI's checkpoints don't keep each task's checks, so a plain resume would run the remaining tasks unchecked, and they can't find the custom tools. The script restores both. Only resume checkpoints you created yourself, because restoring one runs the Python code it references.

---

## Troubleshooting

| Error                                                                 | Fix                                                                                                                                           |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `No module named 'pandas'` (or `yfinance`)                            | The package isn't in the project's environment. Run `uv add pandas numpy yfinance` in the project folder.                                     |
| `Custom tool 'custom:<name>' not found`                               | The tool file is missing or renamed. It must be `tools/<name>.py`.                                                                            |
| `Anthropic native provider not available` or `needs an extra package` | Run the `uv add` command in the message, e.g. `uv add "crewai[anthropic]"` or `uv add "crewai[google-genai]"`.                                |
| `Input file not found: data/portfolio.csv`                            | Copy the example CSVs and fill them in (setup step 4).                                                                                        |
| `short calls are not fully covered`                                   | Each option contract needs 100 shares in the same row.                                                                                        |
| Tools return `ERROR fetching ...`                                     | Yahoo is unreachable or rate-limiting. Wait and rerun; the agents report N/A instead of guessing.                                             |
| A task keeps retrying                                                 | A check is failing. The run report (`output/runs/<date_time>/summary.md`) lists each failure, and the rejected reports are saved next to it.  |
| A report starts with DEGRADED REPORT                                  | It failed its check on every attempt and was accepted so the run could continue. The banner lists what's wrong.                               |
| The run stopped partway                                               | Fix the cause, then `uv run python scripts/resume_run.py` to continue from the last finished task.                                            |
| Data issues in the run report                                         | Retries, backup sources used, or snapshot items that failed. Agents fill gaps with their tools and list what's still missing under Data gaps. |

Check that the environment is ready:

```bash
uv run --no-sync python -c "import crewai, yfinance, pandas; print('ok')"
```

---

## DeepSeek test script

`scripts/deepseek_flash.py` is a standalone script for testing a DeepSeek key and model. The crew doesn't use it.

```bash
python scripts/deepseek_flash.py "Hello!"
python scripts/deepseek_flash.py --stream --no-thinking "Explain covered calls in 3 bullets"
```
