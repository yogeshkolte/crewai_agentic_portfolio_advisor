// CopilotKit runtime: the chat page talks to this route, and it forwards each chat turn to the
// Python agent (chat/server.py) over AG-UI. It runs on the Next.js server, never in the browser.
import { CopilotRuntime, InMemoryAgentRunner, createCopilotEndpointSingleRoute } from "@copilotkit/runtime/v2";
import { CrewAIAgent } from "@ag-ui/crewai";
import { handle } from "hono/vercel";

// Where the Python agent listens. Override with STOCK_AGENT_URL in chat/web/.env.local.
const agentUrl = process.env.STOCK_AGENT_URL ?? "http://127.0.0.1:8000/stock_chat";

const runtime = new CopilotRuntime({
  agents: {
    stock_chat: new CrewAIAgent({ url: agentUrl }),
  },
  runner: new InMemoryAgentRunner(),
});

// Single-route mode: every CopilotKit request goes to this one path, which matches this route file.
// (The multi-route mode would need a catch-all app/api/copilotkit/[[...path]] route.)
const app = createCopilotEndpointSingleRoute({ runtime, basePath: "/api/copilotkit" });

const handler = handle(app);
export const GET = handler;
export const POST = handler;
