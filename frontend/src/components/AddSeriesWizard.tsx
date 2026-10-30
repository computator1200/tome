import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { BookText, FileArchive, Loader2, Search, Upload } from "lucide-react";
import { toast } from "sonner";
import type { EngineMode, MetaResult } from "@/types";
import {
  createManualSeries,
  createSeriesFromNU,
  deleteSeries,
  ingestFiles,
  linkSeriesMetadata,
  searchMetadata,
  uploadVolume,
} from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type Step = "search" | "manual" | "upload";

/** Surface the server's own explanation instead of a generic "upload failed". */
function uploadError(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (typeof detail === "object" && detail) return "That file could not be read.";
  return "Upload failed. Check the file and try again.";
}

const ENGINE_LABELS: Record<EngineMode, string> = {
  vision: "Vision — model reads the page directly (needs a multimodal endpoint)",
  ocr_text: "local OCR + text — local OCR, then the endpoint translates text",
};

// Common source/target languages; the value is sent verbatim into the prompt.
// "auto" means: work it out from the page. It is the default so a series
// never has to be described before its first page is read.
const LANGUAGES = ["auto", "Japanese", "Korean", "Chinese", "English", "French", "German", "Spanish"];
const languageLabel = (v: string) => (v === "auto" ? "Auto-detect" : v);;

export default function AddSeriesWizard({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
}) {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("search");

  // search: every book catalogue the backend knows about, merged
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MetaResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // manual
  const [mTitle, setMTitle] = useState("");
  const [mDesc, setMDesc] = useState("");
  const [mSrc, setMSrc] = useState("auto");
  const [mTgt, setMTgt] = useState("English");

  // creation / upload
  const [busy, setBusy] = useState(false);
  const [seriesId, setSeriesId] = useState<string | null>(null);
  const [seriesTitle, setSeriesTitle] = useState("");
  const [engine, setEngine] = useState<EngineMode>("vision");
  const [batchSize, setBatchSize] = useState(5);
  const fileRef = useRef<File[]>([]);
  const [fileNames, setFileNames] = useState<string[]>([]);
  // The series row is created before the user has committed to uploading
  // anything, so a wizard that is closed at the upload step used to leave an
  // empty series in the library. Remember whether anything was actually
  // added, and roll the shell back when it wasn't.
  const keptRef = useRef(false);

  // reset on close
  useEffect(() => {
    if (!open) {
      if (seriesId && !keptRef.current) {
        void deleteSeries(seriesId).catch(() => {
          /* the shell is empty; failing to remove it must not break the dialog */
        });
      }
      keptRef.current = false;
      setStep("search");
      setQuery("");
      setResults([]);
      setSearchError(null);
      setMTitle("");
      setMDesc("");
      setMSrc("auto");
      setMTgt("English");
      setSeriesId(null);
      setSeriesTitle("");
      setEngine("vision");
      setBatchSize(5);
      fileRef.current = [];
      setFileNames([]);
      setBusy(false);
    }
  }, [open]);

  // debounced metadata search
  useEffect(() => {
    if (step !== "search" || query.trim().length < 2) {
      setResults([]);
      return;
    }
    const t = setTimeout(async () => {
      setSearching(true);
      setSearchError(null);
      try {
        const { results: rs, notes } = await searchMetadata(query.trim());
        setResults(rs);
        // Provider plumbing ("openlibrary: unreachable") is for us, not the
        // reader: only surface it when there is nothing to show instead.
        setSearchError(rs.length === 0 && notes.length > 0 ? notes.join(" ") : null);
      } catch {
        setSearchError(
          "Metadata sources couldn't be reached. You can add the series manually."
        );
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, 450);
    return () => clearTimeout(t);
  }, [query, step]);

  async function discardDraft() {
    if (seriesId && !keptRef.current) {
      try {
        await deleteSeries(seriesId);
      } catch {
        /* ignore */
      }
      setSeriesId(null);
    }
  }

  async function pickResult(r: MetaResult) {
    setBusy(true);
    await discardDraft();
    try {
      let s;
      if (r.source === "novelupdates") {
        s = await createSeriesFromNU(r.ref);
      } else {
        // catalogue hit: create the shell, then fill it from
        // the metadata link endpoint (poster, author, genres, names, …).
        s = await createManualSeries(r.title, "", "auto", "English");
        s = await linkSeriesMetadata(s.id, r.source, r.ref);
      }
      setSeriesId(s.id);
      setSeriesTitle(s.title);
      setStep("upload");
    } catch {
      toast.error("Failed to fetch that series' metadata.");
    } finally {
      setBusy(false);
    }
  }

  async function submitManual() {
    if (!mTitle.trim()) return;
    setBusy(true);
    await discardDraft();
    try {
      const s = await createManualSeries(mTitle.trim(), mDesc.trim(), mSrc, mTgt);
      setSeriesId(s.id);
      setSeriesTitle(s.title);
      setStep("upload");
    } catch {
      toast.error("Failed to create series.");
    } finally {
      setBusy(false);
    }
  }

  async function submitUpload() {
    const files = fileRef.current;
    if (!seriesId || files.length === 0) return;
    setBusy(true);
    try {
      const archives = files.filter((f) => f.name.toLowerCase().endsWith(".zip"));
      const documents = files.filter((f) => !f.name.toLowerCase().endsWith(".zip"));

      for (const file of archives) {
        await uploadVolume(seriesId, file, engine, batchSize);
      }
      if (documents.length > 0) {
        // The librarian reads documents itself: it detects chapters, organises
        // them into volumes and can be told what the batch actually is.
        const { session_id } = await ingestFiles(documents, seriesId);
        keptRef.current = true;
        onCreated();
        onOpenChange(false);
        toast.success(`Reading ${documents.length} file(s) — the librarian will organise them.`);
        navigate(`/import/${session_id}`);
        return;
      }
      keptRef.current = true;
      onCreated();
      onOpenChange(false);
      toast.success(archives.length > 1
        ? `Added ${archives.length} volumes — processing started.`
        : `Added "${seriesTitle}" — processing started.`);
      navigate(`/series/${seriesId}`);
    } catch (err) {
      toast.error(uploadError(err));
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {step === "upload" ? `Add files — ${seriesTitle}` : "Add a series"}
          </DialogTitle>
          <DialogDescription>
            {step === "search" && "Search the book catalogues to pull in the cover, synopsis and genres. Titles in any language work."}
            {step === "manual" && "Add a series that isn't in any catalogue. You can edit it later."}
            {step === "upload" && "Drop anything — a folder of scans zipped, an epub, a PDF, plain text, a whole archive. The librarian reads it, works out what it is and organises it into volumes."}
          </DialogDescription>
        </DialogHeader>

        {step === "search" && (
          <div className="space-y-3">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                autoFocus
                placeholder="Search by title, in any language…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="pl-9"
              />
              {searching && (
                <Loader2 className="absolute right-3 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted-foreground" />
              )}
            </div>

            {searchError && (
              <p className="text-sm text-amber-400">{searchError}</p>
            )}

            <ScrollArea className="h-72 rounded-md border">
              {results.length === 0 && !searching ? (
                <div className="flex h-72 items-center justify-center px-6 text-center text-sm text-muted-foreground">
                  {query.trim().length < 2
                    ? "Start typing to search (works in any language)."
                    : "No results."}
                </div>
              ) : (
                <div className="divide-y">
                  {results.map((r, i) => (
                    <button
                      key={`${r.source}-${r.ref}-${i}`}
                      disabled={busy}
                      onClick={() => pickResult(r)}
                      className="flex w-full gap-3 p-3 text-left transition-colors hover:bg-accent disabled:opacity-50"
                    >
                      <div className="h-24 w-16 shrink-0 overflow-hidden rounded bg-muted">
                        {r.image ? (
                          <img src={r.image} alt="" className="size-full object-cover" />
                        ) : (
                          <div className="flex size-full items-center justify-center">
                            <BookText className="size-6 text-muted-foreground/40" />
                          </div>
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="line-clamp-1 text-sm font-medium">{r.title}</p>
                        <p className="text-xs text-muted-foreground">
                          {r.source}{r.year ? ` · ${r.year}` : ""}
                        </p>
                        <p className="mt-0.5 line-clamp-3 text-xs text-muted-foreground">
                          {r.description}
                        </p>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </ScrollArea>

            <div className="flex justify-between">
              <Button variant="ghost" onClick={() => setStep("manual")}>
                Can't find it? Add manually
              </Button>
              {busy && <Loader2 className="size-5 animate-spin text-primary" />}
            </div>
          </div>
        )}

        {step === "manual" && (
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Title</label>
              <Input
                autoFocus
                value={mTitle}
                onChange={(e) => setMTitle(e.target.value)}
                placeholder="Series title"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Description</label>
              <Textarea
                value={mDesc}
                onChange={(e) => setMDesc(e.target.value)}
                placeholder="A short synopsis (helps the translation engine with context)"
                rows={4}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-sm font-medium">Translate from</label>
                <Select value={mSrc} onValueChange={setMSrc}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {LANGUAGES.map((l) => <SelectItem key={l} value={l}>{languageLabel(l)}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium">Translate to</label>
                <Select value={mTgt} onValueChange={setMTgt}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {LANGUAGES.map((l) => <SelectItem key={l} value={l}>{languageLabel(l)}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="flex justify-between">
              <Button variant="ghost" onClick={() => setStep("search")}>
                Back to search
              </Button>
              <Button onClick={submitManual} disabled={busy || !mTitle.trim()}>
                {busy && <Loader2 className="mr-1 size-4 animate-spin" />}
                Continue
              </Button>
            </div>
          </div>
        )}

        {step === "upload" && (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Translation engine</label>
              <Select value={engine} onValueChange={(v) => setEngine(v as EngineMode)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="vision">Vision (one-pass)</SelectItem>
                  <SelectItem value="ocr_text">local OCR + text</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">{ENGINE_LABELS[engine]}</p>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">Batch size (pages per request)</label>
              <Select
                value={String(batchSize)}
                onValueChange={(v) => setBatchSize(Number(v))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {[1, 5, 10, 15, 20].map((n) => (
                    <SelectItem key={n} value={String(n)}>
                      {n} pages
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <label
              className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-border p-8 text-center transition-colors hover:border-primary/50"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const fs = Array.from(e.dataTransfer.files);
                if (fs.length) {
                  fileRef.current = fs;
                  setFileNames(fs.map((x) => x.name));
                }
              }}
            >
              <div className="flex size-12 items-center justify-center rounded-full bg-accent">
                <Upload className="size-6 text-primary" />
              </div>
              {fileNames.length > 0 ? (
                <div className="space-y-1 text-sm font-medium">
                  {fileNames.slice(0, 5).map((n) => (
                    <p key={n} className="flex items-center gap-2">
                      <FileArchive className="size-4" /> {n}
                    </p>
                  ))}
                  {fileNames.length > 5 && (
                    <p className="text-xs text-muted-foreground">
                      and {fileNames.length - 5} more
                    </p>
                  )}
                </div>
              ) : (
                <>
                  <p className="text-sm font-medium">Drop any files, or click</p>
                  <p className="text-xs text-muted-foreground">
                    zips, ebooks, PDFs, text, raw images — the librarian sorts them out
                  </p>
                </>
              )}
              <input
                type="file"
                accept=".zip,.epub,.pdf,.txt,.html,.cbz,.rar,.7z"
                multiple
                className="hidden"
                onChange={(e) => {
                  const fs = Array.from(e.target.files ?? []);
                  if (fs.length) {
                    fileRef.current = fs;
                    setFileNames(fs.map((x) => x.name));
                  }
                }}
              />
            </label>

            <div className="flex justify-end">
              <Button onClick={submitUpload} disabled={busy || fileNames.length === 0}>
                {busy && <Loader2 className="mr-1 size-4 animate-spin" />}
                Start processing
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
