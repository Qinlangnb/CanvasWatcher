import { useEffect, useRef, useState } from "react";
import { api, json } from "./api";
import { useDrawerFocus } from "./useDrawerFocus";
import { ChatMarkdown } from "./ChatMarkdown";
import { chatErrorMessage, parseChatResponse } from "./chatResponse";
import "katex/dist/katex.min.css";

type Confirmation = {
  id: string;
  tool_name: string;
  summary: string;
  arguments: Record<string, unknown>;
  status: string;
  result?: unknown;
};
type ChatItem = {
  id?: number;
  role: "user" | "assistant" | "tool";
  content: string;
  kind?: string;
  tool_name?: string | null;
  payload?: Record<string, unknown>;
};

const STORAGE_KEY = "academic-watcher.ai-conversation";

export function AIChatDrawer({
  open,
  close,
  changed,
  route,
  courseId,
}: {
  open: boolean;
  close: () => void;
  changed: () => void;
  route: string;
  courseId: number | null;
}) {
  const composer = useRef<HTMLTextAreaElement>(null);
  useDrawerFocus(open, close, composer);
  const end = useRef<HTMLDivElement>(null);
  const [conversation, setConversation] = useState(() =>
    localStorage.getItem(STORAGE_KEY),
  );
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!conversation) return;
    void api<{ messages: ChatItem[] }>(
      `/api/ai/chat/conversations/${conversation}`,
    )
      .then((value) => setItems(value.messages))
      .catch(() => {
        localStorage.removeItem(STORAGE_KEY);
        setConversation(null);
      });
  }, []);
  useEffect(() => {
    if (open) end.current?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [open, items, busy]);
  async function send() {
    const content = input.trim();
    if (!content || busy) return;
    setInput("");
    setItems((old) => [...old, { role: "user", content }]);
    setBusy(true);
    try {
      const result = parseChatResponse(await api<unknown>(
        "/api/ai/chat",
        json("POST", {
          conversation_id: conversation,
          messages: [{ role: "user", content }],
          page_context: { route, course_id: courseId },
        }),
      ));
      setConversation(result.conversation_id);
      localStorage.setItem(STORAGE_KEY, result.conversation_id);
      setItems((old) => [
        ...old,
        ...result.tool_results.map((card) => ({
          role: "tool" as const,
          content: `${card.tool_name} completed`,
          kind: "tool_result",
          tool_name: card.tool_name,
          payload: card as unknown as Record<string, unknown>,
        })),
        ...(result.confirmation
          ? [
              {
                role: "assistant" as const,
                content: result.confirmation.summary,
                kind: "confirmation",
                tool_name: result.confirmation.tool_name,
                payload: result.confirmation as unknown as Record<
                  string,
                  unknown
                >,
              },
            ]
          : []),
        ...(result.message
          ? [{ role: "assistant" as const, content: result.message }]
          : []),
        ...(result.error
          ? [
              {
                role: "assistant" as const,
                content: result.error.message,
                kind: "error",
                payload: result.error as unknown as Record<string, unknown>,
              },
            ]
          : []),
      ]);
    } catch (error) {
      setItems((old) => [
        ...old,
        {
          role: "assistant",
          content: chatErrorMessage(error),
          kind: "error",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }
  async function resolve(card: Confirmation, action: "confirm" | "cancel") {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api<{ message: string; confirmation: Confirmation }>(
        "/api/ai/chat/confirm",
        json("POST", { confirmation_id: card.id, action }),
      );
      setItems((old) =>
        old
          .map((item) =>
            item.kind === "confirmation" && item.payload?.id === card.id
              ? {
                  ...item,
                  payload: result.confirmation as unknown as Record<
                    string,
                    unknown
                  >,
                }
              : item,
          )
          .concat({
            role: "assistant",
            content: result.message,
            kind: "mutation_result",
            payload: result.confirmation as unknown as Record<string, unknown>,
          }),
      );
      if (action === "confirm") changed();
    } catch (error) {
      setItems((old) => [
        ...old,
        {
          role: "assistant",
          content:
            error instanceof Error
              ? error.message
              : "The action could not be resolved.",
          kind: "error",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }
  function reset() {
    localStorage.removeItem(STORAGE_KEY);
    setConversation(null);
    setItems([]);
  }
  if (!open) return null;
  return (
    <div
      className="drawer-backdrop"
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <aside
        className="chat-drawer general-chat"
        role="dialog"
        aria-modal="true"
        aria-label="AI Chat"
      >
        <div className="dialog-head">
          <div>
            <p className="eyebrow">ACADEMIC WATCHER</p>
            <h2>AI Chat</h2>
          </div>
          <div className="chat-head-actions">
            <button
              className="ghost"
              onClick={reset}
              disabled={busy || !items.length}
            >
              New chat
            </button>
            <button
              className="icon-button"
              aria-label="Close AI Chat"
              onClick={close}
            >
              ×
            </button>
          </div>
        </div>
        <p className="chat-intro">
          Ask about courses, tasks, files, changes, Today, Timeline, or your
          calendar. Changes always require confirmation.
        </p>
        <div className="chat-history" aria-live="polite">
          {!items.length ? (
            <div className="chat-empty">
              <strong>What do you need to know?</strong>
              <span>
                I will use local Academic Watcher evidence instead of guessing.
              </span>
            </div>
          ) : (
            items.map((item, index) => (
              <ChatRow
                key={item.id ?? index}
                item={item}
                resolve={resolve}
                disabled={busy}
              />
            ))
          )}
          {busy ? (
            <div className="chat-working">
              <span className="button-spinner" />
              Working with your local data…
            </div>
          ) : null}
          <div ref={end} />
        </div>
        <div className="chat-composer">
          <textarea
            ref={composer}
            aria-label="Message AI Chat"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
            placeholder="Ask about a deadline, file, course, or plan…"
          />
          <button
            className="primary"
            disabled={!input.trim() || busy}
            onClick={() => void send()}
          >
            Send
          </button>
          <small>Enter to send · Shift+Enter for a new line</small>
        </div>
      </aside>
    </div>
  );
}

function ChatRow({
  item,
  resolve,
  disabled,
}: {
  item: ChatItem;
  resolve: (value: Confirmation, action: "confirm" | "cancel") => Promise<void>;
  disabled: boolean;
}) {
  if (item.kind === "tool_result")
    return (
      <article className="chat-tool-card">
        <span>Evidence · {item.tool_name?.replaceAll("_", " ")}</span>
        <ToolResult value={item.payload?.result} />
      </article>
    );
  if (item.kind === "confirmation") {
    const value = item.payload as unknown as Confirmation;
    return (
      <article className="chat-confirmation">
        <p className="eyebrow">CONFIRMATION REQUIRED</p>
        <strong>{value.summary}</strong>
        <dl>
          {Object.entries(value.arguments ?? {}).map(([key, val]) => (
            <div key={key}>
              <dt>{key.replaceAll("_", " ")}</dt>
              <dd>{String(val ?? "—")}</dd>
            </div>
          ))}
        </dl>
        {value.status === "PENDING" ? (
          <div className="card-actions">
            <button
              className="primary"
              disabled={disabled}
              onClick={() => void resolve(value, "confirm")}
            >
              Confirm
            </button>
            <button
              className="secondary"
              disabled={disabled}
              onClick={() => void resolve(value, "cancel")}
            >
              Cancel
            </button>
          </div>
        ) : (
          <small>{value.status === "APPLIED" ? "Applied" : "Cancelled"}</small>
        )}
      </article>
    );
  }
  return (
    <div
      className={`chat-bubble ${item.role} ${item.kind === "error" ? "error" : ""}`}
    >
      <span>{item.role === "user" ? "You" : "AI Chat"}</span>
      {item.kind === "error" ? <p role="alert">{item.content}</p> : <ChatMarkdown content={item.content} />}
    </div>
  );
}

function ToolResult({ value }: { value: unknown }) {
  if (!value || typeof value !== "object")
    return <p>{String(value ?? "No result")}</p>;
  const row = value as Record<string, unknown>;
  const route =
    typeof row.open_route === "string"
      ? row.open_route
      : typeof row.route === "string"
        ? row.route
        : null;
  return (
    <>
      <p>
        {String(
          row.title ??
            row.filename ??
            row.summary ??
            row.message ??
            "Local data retrieved",
        )}
      </p>
      {route ? (
        <a
          href={route}
          target={route.startsWith("/api/files/") ? "_blank" : undefined}
          rel="noreferrer"
        >
          Open evidence
        </a>
      ) : null}
    </>
  );
}
