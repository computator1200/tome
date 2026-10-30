import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  BookText,
  Download,
  ExternalLink,
  Loader2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import type { Job, VolumeSummary } from "@/types";
import { deleteSeries, deleteVolume, getSeriesMemory, processSeries, reprocessVolume, resumeVolume,
         type SeriesMemory } from "@/lib/api";
import { useSeries } from "@/hooks/useSeries";
import { useSettings } from "@/lib/settings";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { AlertTriangle, ChevronDown, FileText, ListChecks, MessageCircle, MessageCirclePlus, Sparkles } from "lucide-react";
import type { AgentSession } from "@/types";
import { getVolumeQuality, type VolumeQuality } from "@/lib/api";
import { getSeriesChat, listSeriesAgentSessions, newChatSession } from "@/lib/api";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import StatusBadge from "./StatusBadge";
import ProcessingPanel from "./ProcessingPanel";
import MemoryPanel from "./MemoryPanel";
import EditSeriesDialog from "./EditSeriesDialog";
import AddVolumeDialog from "./AddVolumeDialog";
import AddFilesDialog from "./AddFilesDialog";
import AgentChat from "./AgentChat";

export default function SeriesDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { series, loading, error, refresh } = useSeries(id);
  const [editOpen, setEditOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  // All agent sessions of this series (imports + chats) — the history tabs.
  const [sessions, setSessions] = useState<AgentSession[]>([]);

  const [memory, setMemory] = useState<SeriesMemory | null>(null);
  const reloadMemory = useCallback(() => {
    if (!id) return;
    void getSeriesMemory(id)
      .then(setMemory)
      .catch(() => setMemory(null));
  }, [id]);
  useEffect(reloadMemory, [reloadMemory]);

  async function toggleChat(open: boolean) {
    setChatOpen(open);
    if (!open || !id) return;
    const all = await listSeriesAgentSessions(id);
    if (!chatSessionId) {
      // Default to the latest chat; getSeriesChat creates one if none exists.
      const lastChat = [...all].reverse().find((s) => s.kind === "chat");
      if (lastChat) {
        setChatSessionId(lastChat.id);
        setSessions(all);
      } else {
        const chat = await getSeriesChat(id);
        setChatSessionId(chat.id);
        setSessions(await listSeriesAgentSessions(id));
      }
    } else {
      setSessions(all);
    }
  }

  async function startNewChat() {
    if (!id) return;
    const chat = await newChatSession(id);
    setChatSessionId(chat.id);
    setSessions(await listSeriesAgentSessions(id));
  }

  function sessionLabel(s: AgentSession, i: number): string {
    if (s.kind === "ingest") return s.title || `Import #${i + 1}`;
    return `Chat #${sessions.filter((x) => x.kind === "chat").indexOf(s) + 1}`;
  }

  if (loading)
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    );

  if (error || !series)
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4">
        <p className="text-destructive">{error ?? "Series not found"}</p>
        <Button variant="outline" onClick={() => navigate("/")}>
          Back to library
        </Button>
      </div>
    );

  const pending = series.volumes.filter(
    (v) => v.status !== "done" && v.status !== "processing" && v.status !== "queued"
  );

  async function processAll() {
    if (!series) return;
    // One request: the server queues them in reading order, so volume 1's
    // memory is available to volume 2 rather than five independent runs.
    const result = await processSeries(series.id, "vision", 5);
    if (result.queued.length === 0) {
      toast.info("Nothing to process — every volume is done or already running.");
    } else {
      toast.success(
        `Queued ${result.queued.length} volume${result.queued.length === 1 ? "" : "s"}.`
      );
    }
    refresh();
  }

  async function removeSeries() {
    if (!series || !confirm(`Delete "${series.title}" and all its volumes?`)) return;
    await deleteSeries(series.id);
    toast.success("Series deleted.");
    navigate("/");
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <Button variant="ghost" size="sm" className="mb-4" onClick={() => navigate("/")}>
        <ArrowLeft className="mr-1 size-4" /> Library
      </Button>

      <div className="flex flex-col gap-6 sm:flex-row">
        <div className="mx-auto w-44 shrink-0 sm:mx-0">
          <div className="aspect-[2/3] overflow-hidden rounded-lg border bg-muted">
            {series.poster_url ? (
              <img src={series.poster_url} alt={series.title} className="size-full object-cover" />
            ) : (
              <div className="flex size-full items-center justify-center">
                <BookText className="size-12 text-muted-foreground/40" />
              </div>
            )}
          </div>
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex items-start gap-2">
            <h1 className="flex-1 text-2xl font-bold tracking-tight">{series.title}</h1>
            <StatusBadge status={series.status} />
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
            {series.author && <span>{series.author}</span>}
            {series.year && <span>· {series.year}</span>}
            {series.nu_status && <span>· {series.nu_status}</span>}
          </div>

          {series.genres.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {series.genres.map((g) => (
                <Badge key={g} variant="secondary" className="font-normal">
                  {g}
                </Badge>
              ))}
            </div>
          )}

          {series.description && (
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
              {series.description}
            </p>
          )}

          <div className="flex flex-wrap gap-2 pt-1">
            <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
              <Pencil className="mr-1 size-3.5" /> Edit
            </Button>
            {series.nu_url && (
              <Button variant="outline" size="sm" asChild>
                <a href={series.nu_url} target="_blank" rel="noreferrer">
                  <ExternalLink className="mr-1 size-3.5" /> {sourceLabel(series.nu_url)}
                </a>
              </Button>
            )}
            <Button variant="ghost" size="sm" className="text-destructive" onClick={removeSeries}>
              <Trash2 className="mr-1 size-3.5" /> Delete
            </Button>
          </div>
        </div>
      </div>

      <Separator className="my-6" />

      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Volumes</h2>
        <div className="flex gap-2">
          <Button
            size="sm"
            className="bg-emerald-600 text-white hover:bg-emerald-600/90"
            onClick={processAll}
            disabled={pending.length === 0}
            title={
              pending.length === 0
                ? "Every volume is finished or already running"
                : `Queue ${pending.length} volume${pending.length === 1 ? "" : "s"} in reading order`
            }
          >
            <Play className="mr-1 size-4" /> Process all
          </Button>
          <Button size="sm" variant="outline" onClick={() => setFilesOpen(true)}>
            <Sparkles className="mr-1 size-4" /> Add files
          </Button>
          <Button size="sm" onClick={() => setAddOpen(true)}>
            <Plus className="mr-1 size-4" /> Add volume
          </Button>
        </div>
      </div>

      {series.volumes.length === 0 ? (
        <p className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
          No volumes yet. Add one to start translating.
        </p>
      ) : (
        <div className="space-y-3">
          {series.volumes.map((v) => (
            <VolumeRow key={v.id} volume={v} onChanged={refresh} />
          ))}
        </div>
      )}

        {memory && memory.stats.entities > 0 && id && (
          <MemoryPanel seriesId={id} memory={memory} onChanged={reloadMemory} />
        )}

      {/* Per-series librarian chat: a floating launcher (bottom-right, like any
          site chat widget) opening a side panel. Session lazily created on open. */}
      <Button
        size="icon"
        className="fixed bottom-6 right-6 z-40 size-12 rounded-full shadow-lg"
        aria-label="Ask the librarian"
        onClick={() => void toggleChat(true)}
      >
        <MessageCircle className="size-6" />
      </Button>
      <Sheet open={chatOpen} onOpenChange={(v) => void toggleChat(v)}>
        <SheetContent side="right" className="flex w-full flex-col gap-3 p-4 sm:max-w-md">
          <SheetHeader className="p-0">
            <SheetTitle className="flex items-center gap-2">
              <MessageCircle className="size-5" /> Ask the librarian
            </SheetTitle>
            <SheetDescription className="truncate">
              Questions about {series.title} — answered from the translated text.
            </SheetDescription>
          </SheetHeader>
          {sessions.length > 0 && (
            <div className="flex items-center gap-2">
              <Select value={chatSessionId ?? undefined} onValueChange={setChatSessionId}>
                <SelectTrigger className="min-w-0 flex-1">
                  <SelectValue placeholder="Session…" />
                </SelectTrigger>
                <SelectContent>
                  {sessions.map((s, i) => (
                    <SelectItem key={s.id} value={s.id}>
                      {sessionLabel(s, i)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button size="icon" variant="outline" title="New chat"
                      onClick={() => void startNewChat()}>
                <MessageCirclePlus className="size-4" />
              </Button>
            </div>
          )}
          {chatSessionId ? (
            <AgentChat
              key={chatSessionId}
              sessionId={chatSessionId}
              placeholder={`Ask about ${series.title}…`}
              className="min-h-0 flex-1"
            />
          ) : (
            <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
              <Loader2 className="mr-2 size-4 animate-spin" /> Opening chat…
            </div>
          )}
        </SheetContent>
      </Sheet>

      <EditSeriesDialog
        series={series}
        open={editOpen}
        onOpenChange={setEditOpen}
        onSaved={refresh}
      />
      <AddVolumeDialog
        seriesId={series.id}
        nextNumber={Math.max(0, ...series.volumes.map((v) => v.order)) + 1}
        open={addOpen}
        onOpenChange={setAddOpen}
        onAdded={refresh}
      />
      <AddFilesDialog
        open={filesOpen}
        onOpenChange={setFilesOpen}
        seriesId={series.id}
        onStarted={(sid) => navigate(`/import/${sid}`)}
      />
    </div>
  );
}

function VolumeRow({ volume, onChanged }: { volume: VolumeSummary; onChanged: () => void }) {
  const busy = volume.status === "processing" || volume.status === "queued";

  async function process() {
    // Fresh run for a never-processed volume.
    await reprocessVolume(volume.id, volume.engine_mode, volume.batch_size);
    toast.success("Queued.");
    onChanged();
  }
  async function resume() {
    // Continue a paused or failed volume from its last finished page.
    await resumeVolume(volume.id);
    toast.success("Resuming.");
    onChanged();
  }
  async function retranslate() {
    if (!confirm(`Retranslate ${volume.label} from scratch? This replaces the current translation.`)) return;
    await reprocessVolume(volume.id, volume.engine_mode, volume.batch_size);
    toast.success("Retranslating from scratch.");
    onChanged();
  }
  async function remove() {
    if (!confirm(`Delete ${volume.label}?`)) return;
    await deleteVolume(volume.id);
    toast.success("Volume deleted.");
    onChanged();
  }

  const { settings } = useSettings();
  const themeMode = settings.themeMode;
  const [showSummary, setShowSummary] = useState(false);
  const [showQuality, setShowQuality] = useState(false);
  const [quality, setQuality] = useState<VolumeQuality | null>(null);
  function toggleQuality(open: boolean) {
    setShowQuality(open);
    if (open) {
      void getVolumeQuality(volume.id)
        .then(setQuality)
        .catch(() => toast.error("Could not load the consistency report."));
    }
  }

  const emerald = "bg-emerald-600 text-white hover:bg-emerald-600/90";
  const canRetranslate = ["done", "error", "paused"].includes(volume.status);

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{volume.label}</p>
          <p className="text-xs text-muted-foreground">
            {volume.kind === "text"
              ? `${volume.page_count} ${volume.page_count === 1 ? "chapter" : "chapters"} · digital`
              : `${volume.page_count} pages · ${volume.engine_mode === "vision" ? "Vision" : "local OCR + text"}`}
          </p>
        </div>
        {/* Actions wrap to a second line on narrow screens instead of overlapping. */}
        <div className="flex flex-wrap items-center gap-2">
          {volume.status === "done" && (
            <>
              <Button size="sm" asChild>
                <Link to={`/read/${volume.id}`}>
                  <BookText className="mr-1 size-4" /> Read
                </Link>
              </Button>
              {volume.kind === "text" ? (
                <Button size="sm" variant="outline" asChild
                        title="Download the translated EPUB (original layout preserved)">
                  <a href={`${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/volumes/${volume.id}/export.epub`} download>
                    <Download className="mr-1 size-4" /> EPUB
                  </a>
                </Button>
              ) : (
                <Button size="sm" variant="outline" asChild title="Download as PDF">
                  <a href={`${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/volumes/${volume.id}/export.pdf?theme=${themeMode}`} download>
                    <Download className="mr-1 size-4" /> PDF
                  </a>
                </Button>
              )}
            </>
          )}
          {volume.status === "uploaded" && (
            <Button size="sm" className={emerald} onClick={process}>
              <RefreshCw className="mr-1 size-4" /> Process
            </Button>
          )}
          {(volume.status === "paused" || volume.status === "error") && (
            <Button size="sm" className={emerald} onClick={resume}>
              <Play className="mr-1 size-4" />
              {volume.status === "error" ? "Retry" : "Resume"}
            </Button>
          )}
          {canRetranslate && (
            <Button
              size="sm"
              variant="outline"
              className="border-amber-500/30 text-amber-400 hover:bg-amber-500/10"
              onClick={retranslate}
            >
              <RefreshCw className="mr-1 size-4" /> Retranslate
            </Button>
          )}
          {/* Delete is locked while processing; available once paused/done/error. */}
          {!busy && (
            <Button size="icon" variant="ghost" className="text-destructive" onClick={remove}>
              <Trash2 className="size-4" />
            </Button>
          )}
        </div>
      </div>
      {busy && (
        <div className="px-4 pb-4">
          <ProcessingPanel volume={volume} onDone={onChanged} />
        </div>
      )}
      {!busy && volume.job && volume.job.llm_calls > 0 && (
        <div className="px-4 pb-3 text-xs tabular-nums text-muted-foreground">{usageLine(volume.job)}</div>
      )}
      {volume.status === "paused" && volume.job && (
        <div className="px-4 pb-4 text-xs text-muted-foreground">
          Stopped at {volume.job.step || "start"} ({volume.job.current}/{volume.job.total}). Resume to continue, or delete to start over.
        </div>
      )}
      {volume.status === "error" && volume.job?.error && (
        <div className="px-4 pb-4">
          <Alert variant="destructive">
            <AlertTriangle className="size-4" />
            <AlertTitle>{volume.job.message || "Processing failed"}</AlertTitle>
            <AlertDescription className="break-words">{volume.job.error}</AlertDescription>
          </Alert>
        </div>
      )}
      {volume.summary && (
        <Collapsible open={showSummary} onOpenChange={setShowSummary} className="border-t">
          <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-4 py-2 text-sm text-muted-foreground hover:text-foreground">
            <FileText className="size-3.5" />
            Summary
            <ChevronDown className={cn("ml-auto size-4 transition-transform", showSummary && "rotate-180")} />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <p className="whitespace-pre-wrap px-4 pb-4 text-sm leading-relaxed text-muted-foreground">
              {volume.summary}
            </p>
          </CollapsibleContent>
        </Collapsible>
      )}
      {(volume.status === "done" || volume.status === "error") && (
        <Collapsible open={showQuality} onOpenChange={toggleQuality} className="border-t">
          <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-4 py-2 text-sm text-muted-foreground hover:text-foreground">
            <ListChecks className="size-3.5" />
            Consistency
            {quality?.consistency != null && (
              <span className="tabular-nums">· {Math.round(quality.consistency * 100)}%</span>
            )}
            <ChevronDown className={cn("ml-auto size-4 transition-transform", showQuality && "rotate-180")} />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="space-y-2 px-4 pb-4 text-sm text-muted-foreground">
              {!quality && <p>Checking...</p>}
              {quality && (
                <>
                  <p>
                    {quality.uses === 0
                      ? "No glossary term appears in this volume yet."
                      : `Glossary terms appeared ${quality.uses} times; ${quality.misses} translated differently from the glossary.`}
                    {quality.new_term_count > 0 && ` ${quality.new_term_count} new terms first appear here.`}
                  </p>
                  {quality.drift.length > 0 && (
                    <ul className="space-y-0.5">
                      {quality.drift.map((d) => (
                        <li key={d.jp}>
                          <span className="text-foreground">{d.jp}</span> should be "{d.en}": differs on
                          {" "}{d.misses} of {d.uses} pages (p. {d.pages.join(", ")})
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}


/** Name the catalogue a series came from instead of assuming one provider. */
/** "312 model calls · 1.4M tokens · about $0.82 · 18 min", from the job's counters. */
function usageLine(job: Job): string {
  const tokens = job.prompt_tokens + job.completion_tokens;
  const fmt = (n: number) =>
    n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
  const mins = Math.max(1, Math.round(job.wall_seconds / 60));
  const parts = [`${job.llm_calls} model call${job.llm_calls === 1 ? "" : "s"}`];
  if (tokens) parts.push(`${fmt(tokens)} tokens`);
  if (job.cost_usd != null) parts.push(`about $${job.cost_usd.toFixed(2)}`);
  parts.push(`${mins} min`);
  return parts.join(" · ");
}

function sourceLabel(url: string): string {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    if (host.includes("openlibrary")) return "Open Library";
    if (host.includes("books.google")) return "Google Books";
    if (host.includes("archive.org")) return "Internet Archive";
    if (host.includes("dnb.de")) return "German National Library";
    if (host.includes("bnf.fr")) return "BnF";
    if (host.includes("crossref")) return "Crossref";
    if (host.includes("novelupdates")) return "NovelUpdates";
  } catch {
    /* a malformed URL should not break the page */
  }
  return "Source";
}
