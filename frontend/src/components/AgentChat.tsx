import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, CircleStop, Loader2, MessageCircleQuestion, Send, Wrench } from "lucide-react";
import { toast } from "sonner";
import type { AgentMessage, AgentSession } from "@/types";
import {
  cancelAgentSession,
  createAgentStream,
  getAgentSession,
  sendAgentMessage,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** Live view of a master-agent session: streaming transcript (assistant text +
 *  tool activity), the pending-question card when the agent is waiting on the
 *  user, and an input box for answers / follow-ups. Used by both the import
 *  page and the per-series chat. */
export default function AgentChat({
  sessionId,
  onSession,
  placeholder = "Message the agent…",
  className,
}: {
  sessionId: string;
  onSession?: (s: AgentSession) => void;
  placeholder?: string;
  className?: string;
}) {
  const [session, setSession] = useState<AgentSession | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const closeStream = useRef<() => void>(() => {});

  const updateSession = useCallback(
    (s: AgentSession) => {
      setSession(s);
      onSession?.(s);
    },
    [onSession]
  );

  const addMessage = useCallback((m: AgentMessage) => {
    setMessages((prev) =>
      prev.some((x) => x.seq === m.seq) ? prev : [...prev, m].sort((a, b) => a.seq - b.seq)
    );
  }, []);

  const refresh = useCallback(async () => {
    const detail = await getAgentSession(sessionId);
    updateSession(detail);
    setMessages(detail.messages);
    return detail;
  }, [sessionId, updateSession]);

  // Load the transcript; while the run is live, follow it over SSE.
  useEffect(() => {
    let disposed = false;
    (async () => {
      try {
        const detail = await refresh();
        if (disposed || detail.status !== "running") return;
        const last = detail.messages.at(-1)?.seq ?? 0;
        closeStream.current = createAgentStream(
          sessionId, last, addMessage, updateSession,
          () => void refresh().catch(() => {})
        );
      } catch {
        if (!disposed) toast.error("Could not load the agent session.");
      }
    })();
    return () => {
      disposed = true;
      closeStream.current();
    };
  }, [sessionId, refresh, addMessage, updateSession]);

  async function send(text: string) {
    if (!text.trim() || sending) return;
    setSending(true);
    setInput("");
    try {
      await sendAgentMessage(sessionId, text.trim());
      const detail = await refresh();
      closeStream.current();
      closeStream.current = createAgentStream(
        sessionId, detail.messages.at(-1)?.seq ?? 0, addMessage, updateSession,
        () => void refresh().catch(() => {})
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to send message");
    } finally {
      setSending(false);
    }
  }

  async function stop() {
    await cancelAgentSession(sessionId);
    toast.info("Stopping the agent…");
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, session?.status]);

  const running = session?.status === "running";
  const waiting = session?.status === "waiting_user";
  const canType = !!session && !running && !sending;

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto rounded-lg border bg-card/50 p-4">
        {messages.length === 0 && !running && (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No messages yet — ask the agent something.
          </p>
        )}
        {messages.map((m) => (
          <MessageRow key={m.seq} message={m} />
        ))}
        {running && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Agent is working…
          </div>
        )}
        {session?.status === "error" && session.error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            {session.error}
          </div>
        )}
        {waiting && session?.pending_question && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
            <div className="mb-2 flex items-start gap-2 text-sm">
              <MessageCircleQuestion className="mt-0.5 size-4 shrink-0 text-amber-400" />
              <p className="whitespace-pre-wrap">{session.pending_question.question}</p>
            </div>
            {session.pending_question.options.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {session.pending_question.options.map((opt) => (
                  <Button key={opt} size="sm" variant="outline"
                          disabled={sending} onClick={() => void send(opt)}>
                    {opt}
                  </Button>
                ))}
              </div>
            )}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="mt-2 flex items-center gap-2">
        <Input
          value={input}
          placeholder={waiting ? "Type your answer…" : placeholder}
          disabled={!canType}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) void send(input);
          }}
        />
        {running ? (
          <Button variant="outline" onClick={() => void stop()}>
            <CircleStop className="mr-1 size-4" /> Stop
          </Button>
        ) : (
          <Button disabled={!canType || !input.trim()} onClick={() => void send(input)}>
            {sending ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
          </Button>
        )}
      </div>
    </div>
  );
}

function MessageRow({ message: m }: { message: AgentMessage }) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
          {trim(m.content, 600)}
        </div>
      </div>
    );
  }
  if (m.role === "tool") {
    return (
      <details className="group text-xs text-muted-foreground">
        <summary className="cursor-pointer select-none hover:text-foreground">
          ↳ {m.tool_name ?? "tool"} result
        </summary>
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-muted/50 p-2">
          {pretty(m.content)}
        </pre>
      </details>
    );
  }
  // assistant: prose text and/or tool-call activity lines
  return (
    <div className="space-y-1.5">
      {m.content && (
        <div className="flex items-start gap-2">
          <Bot className="mt-1 size-4 shrink-0 text-primary" />
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{m.content}</p>
        </div>
      )}
      {(m.tool_calls ?? []).map((tc) => (
        // Collapsed by default: the shape of the run matters, not a wall of JSON.
        <details key={tc.id} className="pl-6 text-xs text-muted-foreground">
          <summary className="flex cursor-pointer select-none items-center gap-1.5 hover:text-foreground">
            <Wrench className="size-3 shrink-0" />
            <span className="font-mono">{tc.function.name}</span>
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 font-mono text-[11px]">
            {pretty(tc.function.arguments)}
          </pre>
        </details>
      ))}
    </div>
  );
}

/** Tool payloads are JSON: indent them when they parse, keep them verbatim when not. */
function pretty(raw: string | undefined): string {
  if (!raw) return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function trim(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + "…" : s;
}
