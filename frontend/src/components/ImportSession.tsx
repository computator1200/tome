import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, BookText, Sparkles } from "lucide-react";
import type { AgentSession } from "@/types";
import { Button } from "@/components/ui/button";
import AgentChat from "./AgentChat";

/** Live view of an "add files" import run: the agent's activity feed, its
 *  questions, and a link to the series it created/attached. */
export default function ImportSession() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<AgentSession | null>(null);

  if (!sessionId) return null;

  return (
    <div className="mx-auto flex h-screen max-w-3xl flex-col px-6 py-6">
      <div className="mb-4 flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
          <ArrowLeft className="mr-1 size-4" /> Library
        </Button>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <Sparkles className="size-5 shrink-0 text-primary" />
          <h1 className="truncate text-lg font-semibold">
            {session?.title || "Import"}
          </h1>
        </div>
        {session?.series_id && (
          <Button size="sm" variant="outline" asChild>
            <Link to={`/series/${session.series_id}`}>
              <BookText className="mr-1 size-4" /> Open series
            </Link>
          </Button>
        )}
      </div>
      <AgentChat
        sessionId={sessionId}
        onSession={setSession}
        placeholder="Reply to the agent (e.g. corrections, follow-up instructions)…"
        className="min-h-0 flex-1"
      />
    </div>
  );
}
