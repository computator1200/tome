import { useRef, useState } from "react";
import { Link2, Loader2, Search, Upload } from "lucide-react";
import { toast } from "sonner";
import type { MetaResult, SeriesDetail } from "@/types";
import { linkSeriesMetadata, patchSeries, searchMetadata, uploadPoster } from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// "auto" means: work it out from the page. It is the default so a series
// never has to be described before its first page is read.
const LANGUAGES = ["auto", "Japanese", "Korean", "Chinese", "English", "French", "German", "Spanish"];
const languageLabel = (v: string) => (v === "auto" ? "Auto-detect" : v);;

export default function EditSeriesDialog({
  series,
  open,
  onOpenChange,
  onSaved,
}: {
  series: SeriesDetail;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(series.title);
  const [description, setDescription] = useState(series.description);
  const [sourceLanguage, setSourceLanguage] = useState(series.source_language);
  const [targetLanguage, setTargetLanguage] = useState(series.target_language);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // "Match metadata": search the book catalogues and auto-fill from a pick.
  const [matchQuery, setMatchQuery] = useState(series.title);
  const [matchResults, setMatchResults] = useState<MetaResult[]>([]);
  const [matchNotes, setMatchNotes] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [linking, setLinking] = useState<string | null>(null);

  async function runMatchSearch() {
    if (!matchQuery.trim()) return;
    setSearching(true);
    try {
      const { results, notes } = await searchMetadata(matchQuery.trim());
      setMatchResults(results);
      setMatchNotes(notes);
      if (results.length === 0) toast.info("No matches found.");
    } catch {
      toast.error("Metadata search failed.");
    } finally {
      setSearching(false);
    }
  }

  async function link(r: MetaResult) {
    setLinking(r.ref);
    try {
      const updated = await linkSeriesMetadata(series.id, r.source, r.ref);
      setTitle(updated.title);
      setDescription(updated.description);
      setMatchResults([]);
      toast.success(`Matched to "${updated.title}" (${r.source}).`);
      onSaved();
    } catch {
      toast.error("Failed to apply metadata.");
    } finally {
      setLinking(null);
    }
  }

  async function save() {
    setBusy(true);
    try {
      await patchSeries(series.id, {
        title,
        description,
        source_language: sourceLanguage,
        target_language: targetLanguage,
      });
      const f = fileRef.current?.files?.[0];
      if (f) await uploadPoster(series.id, f);
      toast.success("Series updated.");
      onSaved();
      onOpenChange(false);
    } catch {
      toast.error("Failed to save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit series</DialogTitle>
          <DialogDescription>Update the title, synopsis, or cover.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Match to a book catalogue</label>
            <div className="flex gap-2">
              <Input
                value={matchQuery}
                placeholder="Search by title (any language)…"
                onChange={(e) => setMatchQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void runMatchSearch()}
              />
              <Button type="button" variant="outline" disabled={searching}
                      onClick={() => void runMatchSearch()}>
                {searching ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
              </Button>
            </div>
            {matchNotes.map((n) => (
              <p key={n} className="text-xs text-amber-400">{n}</p>
            ))}
            {matchResults.length > 0 && (
              <ul className="max-h-44 space-y-1 overflow-y-auto rounded-md border p-1">
                {matchResults.map((r, i) => (
                  <li key={`${r.source}-${r.ref}-${i}`}
                      className="flex items-center gap-2 rounded p-1.5 hover:bg-muted/50">
                    {r.image ? (
                      <img src={r.image} alt="" className="h-12 w-8 rounded object-cover" />
                    ) : (
                      <div className="h-12 w-8 rounded bg-muted" />
                    )}
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{r.title}</p>
                      <p className="text-xs text-muted-foreground">
                        {r.source}{r.year ? ` · ${r.year}` : ""}
                        {r.genres.length > 0 && ` · ${r.genres.slice(0, 3).join(", ")}`}
                      </p>
                    </div>
                    <Button type="button" size="sm" variant="outline"
                            disabled={linking !== null} onClick={() => void link(r)}>
                      {linking === r.ref
                        ? <Loader2 className="size-3.5 animate-spin" />
                        : <Link2 className="mr-1 size-3.5" />}
                      Use
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Title</label>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Description</label>
            <Textarea
              rows={5}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Translate from</label>
              <Select value={sourceLanguage} onValueChange={setSourceLanguage}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {LANGUAGES.map((l) => <SelectItem key={l} value={l}>{languageLabel(l)}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Translate to</label>
              <Select value={targetLanguage} onValueChange={setTargetLanguage}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {LANGUAGES.map((l) => <SelectItem key={l} value={l}>{languageLabel(l)}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Cover image</label>
            <Button
              type="button"
              variant="outline"
              className="w-full"
              onClick={() => fileRef.current?.click()}
            >
              <Upload className="mr-1 size-4" /> Choose poster image
            </Button>
            <input ref={fileRef} type="file" accept="image/*" className="hidden" />
          </div>
        </div>
        <DialogFooter>
          <Button onClick={save} disabled={busy || !title.trim()}>
            {busy && <Loader2 className="mr-1 size-4 animate-spin" />}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
