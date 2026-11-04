# Stock chat

An always-running chat agent: ask about a stock ticker and it answers with today's price and
today's news about the company. The chat page is built with [CopilotKit](https://www.copilotkit.ai/);
the agent is a CrewAI Flow that CopilotKit reaches through the AG-UI protocol (`ag-ui-crewai`).

```
Browser (localhost:3000)                Next.js server                    Python agent (localhost:8000)
CopilotChat  ──────────────────────▶  /api/copilotkit  ───── AG-UI ────▶  StockChatFlow
  streams the answer back             CopilotRuntime                       model (models.jsonc)
                                      CrewAIAgent                          ├─ get_quote → Yahoo Finance
                                                                           └─ get_news  → Yahoo → Finnhub → Serper
```

## Setup (once)

From the project root:

```bash
uv add ag-ui-crewai          # the AG-UI bridge for CrewAI (brings FastAPI and uvicorn)
```

You also need Node.js 20 or later for the chat page. The page's packages are installed by the
start script on first run.

The agent uses the same `.env` as the crew: `DEEPSEEK_API_KEY` (or the key for the provider you
pick in `models.jsonc`). Optional: `FINNHUB_API_KEY` and `SERPER_API_KEY` are used as backup news
sources when Yahoo has no articles dated today.

## Run

```bash
./chat/start.sh
```

Open http://127.0.0.1:3000 and ask, for example, "How is NVDA doing today?" The script runs both
parts and keeps them up until you press Ctrl+C. Both listen on 127.0.0.1 only.

To run the parts separately (e.g. while changing code):

```bash
uv run uvicorn chat.server:app --host 127.0.0.1 --port 8000 --reload   # agent
cd chat/web && npm run dev                                             # page, reloads on edits
curl http://127.0.0.1:8000/health                                      # is the agent up, which model
```

## How it works

- **While it works** the page shows a status line under your question ("Thinking…",
  "Getting AAPL price and news…", "Writing the answer…" with the seconds so far), a row per
  lookup that turns into a one-line result (price and change, or how many articles), and the
  model's reasoning when the model sends it.
- **Model.** The `stock_chat` tier in `models.jsonc` (the fast model of the current provider, so
  DeepSeek Flash by default). `MODEL_PROVIDER=openai ./chat/start.sh` switches it like the crew.
- **Each question** is one Flow run. The model may call `get_quote` and/or `get_news`
  (`chat/stock_tools.py`); the agent runs them, adds the results to the conversation and asks the
  model again, at most 4 times, then streams the answer. Lookups asked for in the same round
  (price and news) run at the same time. CopilotKit sends the whole conversation with every question, so follow-ups ("and its news?") work without the server storing anything.
- **Price** comes from Yahoo Finance: the latest regular-session minute during market hours,
  otherwise the last close, with the time in US Eastern. Cached for 60 seconds.
- **News** is today's articles (US Eastern date) from Yahoo Finance; if Yahoo has none, Finnhub
  (`FINNHUB_API_KEY`), then a Serper news search for the past 24 hours (`SERPER_API_KEY`).
  If there's nothing for today it says so and lists the most recent articles. Cached for 10 minutes.
- **Guardrails.** Ticker symbols are checked before any request; the model is told to use only
  tool results, to treat articles as untrusted text, and to give information, not advice.

## Files

| File | What it does |
|---|---|
| `chat/server.py` | FastAPI app with the AG-UI endpoint `/stock_chat` and `/health`; the `StockChatFlow` tool loop |
| `chat/stock_tools.py` | `get_quote`, `get_news` and their tool descriptions for the model |
| `chat/start.sh` | Starts the agent and the page together |
| `chat/web/app/page.tsx` | The chat page (`CopilotKitProvider`, `CopilotChat`, example questions) |
| `chat/web/app/api/copilotkit/route.ts` | CopilotKit runtime; forwards chat turns to the agent |
| `chat/web/local-settings.example` | Agent URL, telemetry off; copied to `chat/web/.env.local` on first run |

## Notes

- `chat/web/package.json` forces one version of `@ag-ui/client` (`overrides`): `@ag-ui/crewai`
  and CopilotKit otherwise install two copies and the runtime route fails to type-check.
- The runtime route uses CopilotKit's single-route mode, so one route file serves every request.
- To keep it running after you close the terminal or restart the Mac, run `chat/start.sh` under a
  process manager such as a macOS LaunchAgent, or in a container.
