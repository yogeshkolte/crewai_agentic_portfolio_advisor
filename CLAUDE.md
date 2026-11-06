# CLAUDE.md

Claude Code loads this file and ignores `AGENTS.md`. The import below pulls in the
shared CrewAI guidance so every coding assistant works from the same instructions.
Keep shared conventions in `AGENTS.md`; add Claude-specific notes under the import.

@AGENTS.md

---

# Project context: CrewAI Portfolio Advisor

Read this before changing anything; it replaces re-deriving the design from the code.
Keep it current when behavior changes (one line per fact, no history).

## Product requirements

**Purpose.** A local, multi-agent research tool that reviews the user's stock holdings and
watchlist and writes a dated, prioritized action plan (hold / add / trim / exit, entry levels,
stops). Research only: it never places trades and every report ends "not investment advice".

**Inputs.** `data/portfolio.csv` (symbol, shares, avg_cost, sector, option_type, strike, expiry,
contracts), `data/watchlist.csv` (symbol, reason), `data/cash.csv` (account label, amount; summed, missing file stops the run), risk limits in `crew.jsonc` → `inputs.risk_profile`
(the single source of truth; guardrails read the same numbers), `events_days` (14).

**Must do**
1. Fetch all market data once at run start (data snapshot) and give each task its slice.
2. Produce these reports, named `output/YYYYMMDD_<name>.md`: events calendar, market context
   (breadth/credit/leadership readings computed in `market_internals.py`; news classified Thesis-changing/-confirming/Noise/None;
   "Environment: Changing/Unchanged"; the plan acts only on Thesis-changing news),
   holdings fundamentals (five questions per holding: business trend, capital allocation judged on ROIC/incremental ROIC,
   FCF sustainability, prospective return vs `hurdle_return_pct`, permanent impairment), watchlist screen (best risk-adjusted additions to this portfolio: separate 1–5 quality/valuation/catalyst/diversification
   scores, Total = sum, ≤5 shortlist, "Cheap for a reason" never valued 4–5 or shortlisted), technicals (every symbol), risk, action
   plan (≤5 top actions, stop on every buy/add, no position over `max_position_pct`, Data gaps
   section, disclaimer, a still-stands/changed/dropped line per open item of the last plan). Covered calls exist but are **switched off** (`scripts/toggle_covered_calls.py`).
3. Events calendar: earnings (Confirmed/Estimated), dividends, macro (FOMC/CPI/jobs/GDP/PCE) only;
   no open short calls while covered calls are off; all times ET; at most 4 macro searches.
4. Check every report (safety + content rules + plain-English review); a report still failing on its
   last attempt is accepted with a `DEGRADED REPORT` banner and the run continues; safety failures stop it.
5. Write a run report per run in `output/runs/<YYYY-MM-DD_HHMMSS>/` (summary.md with time, tokens,
   estimated cost, check failures, degraded reports, data issues; events.jsonl; rejected reports).
6. Resume a stopped run from its last checkpoint; run any single task on its own.
7. `crewai run` exits when the action plan is done (`CREWAI_DMN=1` in `.env`); no human-review prompt.
8. Local Streamlit viewer for reports (`ui/app.py`), read-only, localhost only; `observability._open_viewer` starts it
   (detached, port 8501, log `output/viewer.log`) and opens the browser when a run ends (`REPORT_VIEWER=off` skips).

**Must not**
- Place or claim trades, touch broker accounts, or store/echo account numbers or SSNs.
- Estimate numbers from memory: figures come from this run's tools or earlier reports, else "N/A (unavailable)".
- Read websites outside the `hooks.py` allowlist (`SCRAPE_ALLOWLIST=off` lifts it); scrape sites whose terms forbid it (e.g. stockanalysis.com).
- Commit personal data: `.env`, `data/portfolio.csv`, `data/watchlist.csv`, `data/cash.csv`, `data/track_record/`, `output/`, `.cache/`,
  `.checkpoints/` are git-ignored. Never put holdings or keys in docs, examples or this file.

**Quality targets.** A full run in roughly 10–15 min; cheap models for table-reading work, stronger
models only for judgement; every failure visible in the run report.

## Architecture

Run order (Process.sequential + `async_execution`):
`guardrails.validate_inputs` (CSV checks → `data_snapshot.build`) → `observability.start_run` →
**events** → **market, holdings, watchlist, technicals in parallel** → **risk** → **plan** →
`observability.finish_run`.

| File | Role |
|---|---|
| `crew.jsonc` | Crew settings, inputs (incl. `data_*`, `goal_*`, `backstory_*`, `description_*`, `expected_output_*` placeholders, all need defaults), risk_profile, 7 tasks + 1 commented out; task `"description": "{description_<task>}"`, `"expected_output": "{expected_output_<task>}"` |
| `agents/*.jsonc` | One agent per file: role, tools, limits; `"goal": "{goal_<agent>}"`, `"backstory": "{backstory_<agent>}"`, `"llm": {"python": "models.<agent>"}` |
| `agent_profiles.yaml` + `agent_profiles.py` | Goal and backstory (operating rules + fallback rule) of every agent, `<agent>:` → `goal: |-` / `backstory: |-` (PyYAML, comes with CrewAI); `validate_inputs` → `fill_inputs` adds `goal_*`/`backstory_*` inputs (pre-filling `{portfolio}` etc.); also the shared YAML loader (`load_entries`: unknown fields, missing text, bad YAML → error)/placeholder helpers; CLI checks the file |
| `task_profiles.yaml` + `task_profiles.py` | Description and expected output (incl. "Length:" caps) of every task, `<task>:` → `description: |-` / `expected_output: |-`, incl. the switched-off covered call task; `validate_inputs` runs `check` before the snapshot and `fill_inputs` after it (text uses `{data_*}`); unknown placeholders stop the run |
| `models.jsonc` + `models.py` | Model provider (deepseek/openai/anthropic/gemini), tier per agent (fast/research/strong), per-agent overrides, review model; `models.py` builds each agent's LLM (lazy `__getattr__`), checks keys, CLI `models.py [use <provider>]` |
| `tools/*.py` | `custom:<name>` tools, one `BaseTool` class per file, Yahoo via yfinance; `fundamentals(capital=true)` adds ROIC/incremental ROIC/ROE, capex, SBC, dilution, buybacks, dividends, acquisitions, debt due ≤12 months, coverage, payout vs FCF (snapshot uses it for holdings only; no debt ladder beyond 12 months, SEC backup has none of it) |
| `market_internals.py` | Breadth (RSP/SPY, QQQE/QQQ, MAGS/RSP, IWM/SPY, % above 50/200 DMA), credit (FRED HY/IG OAS, no key; HYG/IEF fallback), relative leadership vs SPY (last month vs month before → Improving/Steady/Deteriorating); one bulk yfinance download, appended to `{data_market}` |
| `track_record.py` | Self-improvement loop: at run start parses every past action plan (Top 5 table) and watchlist screen in `output/` into a ledger `data/track_record/*.csv` (no Size column), scores them vs SPY on Yahoo daily prices (Buy/Add: filled at the level? stop hit? result vs SPY; Trim/Exit: right when the stock then lagged), counts a repeated call once, and marks a group "usable" only with 10 items from 10 names that have a month of prices → `{data_track_record}` (plan: scorecard + open items of the last plan) and `{data_track_record_scores}` (watchlist: return by Total and Value read); CLI `uv run python track_record.py` |
| `market_data.py` | Retries, `.cache/` (prices 15 min, events 6 h, fundamentals 12 h, same day only), Finnhub + SEC EDGAR backups |
| `data_snapshot.py` | Fetches everything at start → `{data_events}`, `{data_market}`, `{data_prices}`, `{data_fundamentals_holdings/watchlist}`, `{data_portfolio}`, `{data_track_record(_scores)}`, `{data_status}`; saves `output/YYYYMMDD_00_data_snapshot.md` |
| `guardrails.py` | Input check; content rules; one `check_<task>` per task (`_run_task_checks`: safety → rules → review on the provider's judge model, `GUARDRAIL_JUDGE_MODEL` overrides); `validate_inputs` stops the run if a needed API key is missing; degrade logic; saves rejected reports |
| `hooks.py` | Blocks trading-like tools, website allowlist (`ALLOWED_DOMAINS`; `allowed_sources_text()` → `{allowed_domains}` in the market agent's backstory; a block returns the reason + list to the agent), flags prompt injection, caps LLM iterations |
| `observability.py` | Event listener → run folder, summary.md, events.jsonl; `PRICES` table (US$/1M tokens) for cost |
| `ui/app.py` + `.streamlit/config.toml` | Report viewer (localhost, no telemetry, Deploy hidden) |
| `chat/` | Separate always-running stock chat (see `chat/README.md`): `server.py` = FastAPI + `ag-ui-crewai` AG-UI endpoint `/stock_chat` with `StockChatFlow(Flow[CopilotKitState])` (LiteLLM tool loop ≤4 rounds, model = `models.spec("stock_chat")`), `stock_tools.py` = `get_quote`/`get_news` (Yahoo → Finnhub → Serper, cached 60 s / 10 min), `web/` = Next.js 16 + CopilotKit 1.83 (`CopilotKitProvider` + `CopilotChat`, single-route runtime, `CrewAIAgent`), `start.sh` runs both on 127.0.0.1 |
| `scripts/run_task.py` | Run one task, optional `--provider` or `--model/--effort` override for that run only |
| `scripts/premarket_snapshot.py` | Standalone (yfinance + pandas only) pre-market data for a watchlist, for the user's separate daily Claude pre-market report: price/time/basis, chg vs prior close, prior H/L, pre-market H/L/volume vs prior 4 pre-markets, SMA20/50/200, 52w, earnings this/next week, backdrop ES=F/NQ=F/SPY/QQQ/^VIX; session logic with `NYSE_HOLIDAYS`/`NYSE_EARLY_CLOSE` (through 2028, from nyse.com; extend yearly); writes `output/premarket/YYYYMMDD_HHMM_premarket_snapshot.{json,md}`; hidden `--now` for tests |
| `scripts/resume_run.py` | Resume from `.checkpoints/` (re-attaches checks, registers custom tools) |
| `scripts/toggle_covered_calls.py` | `off`/`on`: comments the task out of `crew.jsonc` and adjusts plan + events text in `task_profiles.yaml` (text swaps are indented 4 spaces to match the `|-` blocks) |
| `scripts/deepseek_flash.py` | Standalone DeepSeek API test, not used by the crew |

Other files in `scripts/` (`optimize_crew.py`, `make_robust.py`, `harden_tools.py`, `fix_tables.py`,
`readme_*.py`, `solo_support.py`) were one-off installers already applied; safe to delete.

**Models.** Chosen in `models.jsonc` only; precedence: top-level `"agents"` override → provider's `"agents"` → provider's tier
model. Provider = `MODEL_PROVIDER` env/.env, else `"provider"` (default `deepseek`). Tiers: fast = events, stock chat;
research = market, watchlist, technicals; strong = fundamentals, risk, plan, covered calls. deepseek: flash low / flash high /
v4-pro high, covered calls `openai/gpt-5`, judge `openai/gpt-5-mini`. openai: gpt-5-mini low / medium / gpt-5 medium.
anthropic: haiku-5-5 / haiku-5-5 / sonnet-5-5 (needs `crewai[anthropic]`). gemini: 3.5-flash-lite / 3.8-flash /
4-argon (needs `crewai[google-genai]`). Keys in `.env`: `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `SERPER_API_KEY`;
`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` only for those providers; optional `FINNHUB_API_KEY`, `SEC_USER_AGENT="Name email"`.

## Conventions

- Add a check: write a rule function returning `(True, result)` / `_fail(problems, result)`, add it to
  the task's `check_<task>` list. Never list separate guardrails per task (see gotchas).
- New task input from data: add a `{placeholder}` default in `crew.jsonc` inputs and fill it in `validate_inputs`/`data_snapshot`.
- Set `"context"` explicitly on every task; `[]` for none.
- Tools return text starting `ERROR ...` instead of raising; retryable messages are listed in `market_data._RETRYABLE`.
- Report length caps live in each task's expected output in `task_profiles.yaml` ("Length: …").
- Changing a limit: edit `crew.jsonc` risk_profile only.
- Changing an agent's goal/backstory: edit `agent_profiles.yaml` only. New agent → its file uses the two placeholders,
  add an entry, and add `goal_<agent>`/`backstory_<agent>` defaults to `crew.jsonc` inputs.
- Changing a task's description/expected output: edit `task_profiles.yaml` only. New task → placeholders in
  `crew.jsonc`, an entry, and `description_<task>`/`expected_output_<task>` defaults in `crew.jsonc` inputs.
- New placeholder used in a profile file → it must be a `crew.jsonc` input (else "unknown placeholder").
- Changing a model: edit `models.jsonc` only (never put a model name in an agent file); new agent → add its tier.
  New model → add its price to `observability.PRICES`.
- After any change: load test `uv run python -c "from crewai.project.crew_loader import load_crew; load_crew('crew.jsonc')"`,
  then `uv run python scripts/run_task.py <task>` before a full `crewai run`.

## Gotchas (CrewAI 1.15, verified)

- Each listed guardrail has its own retry loop and a retried output is only re-checked by the current and
  later guardrails, so an earlier safety check is skipped on retries. Hence one combined check per task.
- Failed guardrail errors say "guardrail N" counting from 0. Retries re-run the whole agent task.
- A task with no `context` key receives every earlier task's output; use `[]`.
- Async (parallel) tasks: consecutive `async_execution: true` tasks run together until the next sync task.
- Checkpoints drop task guardrails and can't import `custom_tools.*`; resume only via `scripts/resume_run.py`
  (sets `CREWAI_DESERIALIZE_CALLBACKS=1`; only for the user's own checkpoints). On resume the
  before-kickoff callbacks get `{}` inputs and must pass through.
- Parallel tasks finishing together can print "dictionary changed size during iteration" from checkpoint
  writing; harmless (a checkpoint may be skipped).
- Custom tool files: no `from __future__ import annotations` (pydantic can't build the class); CrewAI
  takes the first `BaseTool` subclass found, so keep one per file; shared code goes in the project root.
- `crewai run` uses the project's uv environment: add packages with `uv add`, not pip.
- `CREWAI_DMN=1` disables the full-screen display so the run exits; it also disables input prompts.
- Agent `"llm"` can be a string, `{"model": ..., "reasoning_effort": ...}`, or a `{"python": "module.attr"}` object ref (used here).
  `reasoning_effort` reaches OpenAI and DeepSeek; `models.make_llm` drops it for Anthropic/Gemini.
- A failed import in `models.py` surfaces as "failed to import Python reference 'models.<agent>'"; the real cause
  (missing package, unknown provider) is earlier in the traceback.
- `crewai run` scans agents/tasks for `{placeholders}` and exits (or prompts) if one has no default in `crew.jsonc`
  inputs, even when a before-kickoff callback fills it. Kickoff callbacks run before interpolation; values
  inserted by interpolation are not re-scanned (hence `agent_profiles` pre-fills `{portfolio}` etc.).
- Resume passes no inputs; the checkpoint restores each task's template and the saved run inputs
  (`checkpoint_inputs`), so restored agents and tasks keep this run's text.
- ScrapeWebsiteTool's runtime name is "Read website content" (the hook matches "website").
- A blocked tool call returns "Tool execution blocked by hook" to the agent, but after-tool hooks still run on it
  and their return value replaces that text (used by `hooks.explain_blocked_scrape`).
- yfinance often returns empty data instead of raising when Yahoo is unreachable ("not enough price history" is retried).
- Models write Markdown tables with or without outer pipes; `guardrails._tables` accepts both.
- The plain-English review never sees the inputs, so it can't check coverage or arithmetic: a watchlist report with 8 of
  76 names passed it, and it judged stop-vs-zone numbers inconsistently over 80 rows. Coverage (`watchlist_complete`, uses
  `guardrails.RUN_SYMBOLS`), levels (`technical_levels`) and Diversification (`data_snapshot.FIT`, from `portfolio_fit`,
  Yahoo sectors for holdings and candidates) are checked in code; reviews keep judgement only.
- On ~80-name tasks gpt-5-mini sometimes replies "confirm Option A or B" instead of working; `no_questions` rejects that.
- Chat page: `@ag-ui/crewai` and CopilotKit pull different `@ag-ui/client` versions → `overrides` pins one in
  `chat/web/package.json`. Multi-route `createCopilotEndpoint` needs a catch-all route; we use `createCopilotEndpointSingleRoute`.
  The root `CopilotKit` provider sends single-route requests; use v2 `CopilotKitProvider`.
- Chat: litellm is pinned `<1.103` because 1.103+ imports `mcp` 2.x while CrewAI 1.15 pins `mcp` 1.x
  (symptom: `cannot import name 'CacheableResult' from 'mcp.types'` on every chat reply). Lift it when CrewAI moves to mcp 2.
- Chat page progress: `useDefaultRenderTool` draws a row per price/news lookup; `messageView={{ cursor: StatusLine }}`
  replaces CopilotKit's tiny running dot (the cursor slot gets no props, so `StatusLine` reads `useAgent`).

## Commands

```bash
crewai run                                              # full run, exits at the end
MODEL_PROVIDER=openai crewai run                        # one run on another provider
uv run python models.py [use <provider>]                # show / switch models
uv run python agent_profiles.py [<agent>]               # check agent_profiles.yaml / print one agent
uv run python task_profiles.py [<task>]                 # check task_profiles.yaml / print one task
uv run python scripts/run_task.py --list | <task> [--provider openai | --model deepseek/deepseek-flash --effort low]
uv run python scripts/resume_run.py [--list]            # continue a stopped run
uv run python scripts/premarket_snapshot.py [--watchlist F --out-dir D]  # pre-market data for a watchlist
python3 scripts/toggle_covered_calls.py on|off
uv run streamlit run ui/app.py                          # report viewer, http://localhost:8501
./chat/start.sh                                         # stock chat, http://127.0.0.1:3000
```

## Open items

- DeepSeek effort levels are low/high/max (no "medium"); all agents use one of those.
- Plain-English reviews on DeepSeek untested (structured output); deepseek provider's judge stays gpt-5-mini until tested.
- anthropic and gemini providers load (verified) but no full run yet; reviews on them untested.
- Stock chat tested end to end with a scripted model and fake data (page → runtime → agent → tools → streamed
  answer); not yet against live Yahoo/LLM (sandbox has no access). Parsing tested for both yfinance news formats.
- DeepSeek prices in `observability.PRICES` are peak list prices (off-peak is half); verify against DeepSeek's pricing page.
- Not built: Schwab Trader API backup for prices/options; macro-data source beyond web search.
- Publishing: public at github.com/yogeshkolte/crewai_agentic_portfolio_advisor (MIT, description + topics set); still to do: a screenshot made from example data.
