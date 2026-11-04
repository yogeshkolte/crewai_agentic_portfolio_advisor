"use client";

import { useEffect, useState } from "react";
import {
  CopilotChat,
  CopilotKitProvider,
  UseAgentUpdate,
  useAgent,
  useConfigureSuggestions,
  useDefaultRenderTool,
} from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";

const AGENT = "stock_chat";

// What each agent tool fetches, for the status line and the tool rows.
const TOOL_LABEL: Record<string, string> = { get_quote: "price", get_news: "news" };

type ToolCall = { id: string; function: { name: string; arguments: string } };
type ChatMessage = { role: string; content?: unknown; toolCalls?: ToolCall[]; toolCallId?: string };

function symbolOf(args: unknown): string {
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      return "";
    }
  }
  const s = (args as { symbol?: unknown } | null)?.symbol;
  return typeof s === "string" ? s.toUpperCase() : "";
}

/** "AAPL price and news" from the tool calls still waiting for a result. */
function describe(calls: ToolCall[]): string {
  const bySymbol = new Map<string, string[]>();
  for (const c of calls) {
    const sym = symbolOf(c.function.arguments) || "the stock";
    const what = TOOL_LABEL[c.function.name] ?? c.function.name;
    bySymbol.set(sym, [...(bySymbol.get(sym) ?? []), what]);
  }
  return [...bySymbol].map(([sym, what]) => `${sym} ${what.join(" and ")}`).join(", ");
}

/** What the agent is doing right now, worked out from this turn's messages. */
function phase(messages: ChatMessage[]): string {
  const lastUser = messages.map((m) => m.role).lastIndexOf("user");
  const turn = messages.slice(lastUser + 1);
  const answered = new Set(turn.filter((m) => m.role === "tool").map((m) => m.toolCallId));
  const pending = turn.flatMap((m) => m.toolCalls ?? []).filter((c) => !answered.has(c.id));
  if (pending.length) return `Getting ${describe(pending)}…`;
  const last = turn[turn.length - 1];
  if (last?.role === "assistant" && typeof last.content === "string" && last.content.trim()) return "Writing the answer…";
  if (answered.size) return "Reading the results…";
  return "Thinking…";
}

/** Replaces CopilotKit's small pulsing dot: a spinner, what's happening, and the seconds so far. */
function StatusLine() {
  const { agent } = useAgent({
    agentId: AGENT,
    updates: [UseAgentUpdate.OnMessagesChanged, UseAgentUpdate.OnRunStatusChanged],
  });
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(started);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const secs = Math.floor((now - started) / 1000);
  return (
    <div className="status" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{phase(agent.messages as unknown as ChatMessage[])}</span>
      {secs > 0 && <span className="status-time">{secs}s</span>}
    </div>
  );
}

/** One line of the result, e.g. "189.40 USD, +1.25 (+0.66%) · 15:59 EDT". */
function summarize(name: string, result: string | undefined): { ok: boolean; text: string } {
  let r: Record<string, any>;
  try {
    r = JSON.parse(result ?? "");
  } catch {
    return { ok: true, text: "done" };
  }
  if (r.error) return { ok: false, text: String(r.error) };
  if (name === "get_quote") {
    if (r.price == null) return { ok: false, text: "no price available" };
    const sign = (n: number) => (n > 0 ? "+" : "");
    const change = r.change != null ? `, ${sign(r.change)}${r.change.toFixed(2)} (${sign(r.change_pct)}${r.change_pct}%)` : "";
    const time = typeof r.as_of_et === "string" ? ` · ${r.as_of_et.slice(11)}` : "";
    return { ok: true, text: `${Number(r.price).toFixed(2)} ${r.currency ?? ""}${change}${time}` };
  }
  if (name === "get_news") {
    const n = Array.isArray(r.articles) ? r.articles.length : 0;
    return n
      ? { ok: true, text: `${n} article${n === 1 ? "" : "s"} today${r.source ? ` (${r.source})` : ""}` }
      : { ok: true, text: "no articles today; using the most recent ones" };
  }
  return { ok: true, text: "done" };
}

/** A row per tool call in the conversation: spinner while it runs, then a one-line result. */
function ToolRow(props: { name: string; parameters: unknown; status: string; result: string | undefined }) {
  const sym = symbolOf(props.parameters);
  const what = `${sym ? sym + " " : ""}${TOOL_LABEL[props.name] ?? props.name}`;
  if (props.status !== "complete") {
    return (
      <div className="tool-row">
        <span className="spinner" aria-hidden="true" />
        <span>Getting {what}…</span>
      </div>
    );
  }
  const { ok, text } = summarize(props.name, props.result);
  return (
    <div className={`tool-row ${ok ? "" : "tool-row-error"}`}>
      <span className="tool-icon" aria-hidden="true">{ok ? "✓" : "!"}</span>
      <span className="tool-what">{what}</span>
      <span className="tool-result">{text}</span>
    </div>
  );
}

function StockChat() {
  // Example questions shown before the first message.
  useConfigureSuggestions({
    available: "before-first-message",
    consumerAgentId: AGENT,
    suggestions: [
      { title: "Apple today", message: "How is AAPL doing today?" },
      { title: "NVDA price", message: "What's NVDA trading at right now?" },
      { title: "Microsoft news", message: "Any news about Microsoft today?" },
    ],
  });
  // Show the agent's price/news lookups in the conversation (otherwise they're invisible).
  useDefaultRenderTool({ render: (p) => <ToolRow {...p} /> });

  return (
    <CopilotChat
      agentId={AGENT}
      className="chat"
      messageView={{ cursor: StatusLine }}
      labels={{
        chatInputPlaceholder: "Ask about a ticker, e.g. TSLA price and news",
      }}
    />
  );
}

export default function Page() {
  return (
    <CopilotKitProvider runtimeUrl="/api/copilotkit">
      <main className="page">
        <header className="header">
          <h1>Stock chat</h1>
          <p>Today&apos;s price and news for any ticker. Information only, not investment advice.</p>
        </header>
        <StockChat />
      </main>
    </CopilotKitProvider>
  );
}
