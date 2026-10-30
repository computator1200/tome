import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  BookOpen,
  MessageCircleQuestion,
  Plus,
  Settings as SettingsIcon,
  Sparkles,
} from "lucide-react";
import { useSeriesList } from "@/hooks/useSeries";
import { getAgentAlerts } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import SeriesCard from "./SeriesCard";
import AddSeriesWizard from "./AddSeriesWizard";
import AddFilesDialog from "./AddFilesDialog";

export default function Home() {
  const { series, loading, error, refresh } = useSeriesList();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const navigate = useNavigate();

  // Agent runs that are suspended on a question for the user.
  const { data: alerts = [] } = useQuery({
    queryKey: ["agent-alerts"],
    queryFn: getAgentAlerts,
    refetchInterval: 8000,
  });

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-8 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <BookOpen className="size-12 text-foreground" strokeWidth={1.75} />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Tome</h1>
            <p className="text-sm text-muted-foreground">Your translated library</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" asChild>
            <Link to="/settings" aria-label="Settings">
              <SettingsIcon className="size-5" />
            </Link>
          </Button>
          <Button variant="outline" onClick={() => setFilesOpen(true)}>
            <Sparkles className="mr-1 size-4" /> Add files
          </Button>
          <Button onClick={() => setWizardOpen(true)}>
            <Plus className="mr-1 size-4" /> Add series
          </Button>
        </div>
      </header>

      {alerts.length > 0 && (
        <div className="mb-6 space-y-2">
          {alerts.map((a) => (
            <Link
              key={a.id}
              to={a.kind === "ingest" ? `/import/${a.id}` : `/series/${a.series_id}`}
              className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm hover:bg-amber-500/20"
            >
              <MessageCircleQuestion className="mt-0.5 size-4 shrink-0 text-amber-400" />
              <span className="min-w-0">
                <span className="font-medium">{a.title || "Agent"}</span> needs your input:{" "}
                <span className="text-muted-foreground">
                  {a.pending_question?.question}
                </span>
              </span>
            </Link>
          ))}
        </div>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}

      {loading ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {Array.from({ length: 10 }).map((_, i) => (
            <Skeleton key={i} className="aspect-[2/3] w-full rounded-lg" />
          ))}
        </div>
      ) : series.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed py-24 text-center">
          <BookOpen className="size-12 text-muted-foreground/40" />
          <div>
            <p className="font-medium">Your library is empty</p>
            <p className="text-sm text-muted-foreground">
              Add a book to start translating.
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setFilesOpen(true)}>
              <Sparkles className="mr-1 size-4" /> Add files
            </Button>
            <Button onClick={() => setWizardOpen(true)}>
              <Plus className="mr-1 size-4" /> Add your first series
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {series.map((s) => (
            <SeriesCard key={s.id} series={s} />
          ))}
        </div>
      )}

      <AddSeriesWizard open={wizardOpen} onOpenChange={setWizardOpen} onCreated={refresh} />
      <AddFilesDialog
        open={filesOpen}
        onOpenChange={setFilesOpen}
        onStarted={(id) => navigate(`/import/${id}`)}
      />
    </div>
  );
}
