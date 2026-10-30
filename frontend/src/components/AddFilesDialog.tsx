import { useRef, useState } from "react";
import { FileArchive, Loader2, Sparkles, Upload, X } from "lucide-react";
import { toast } from "sonner";
import { ingestFiles } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

/** The AI-first "add files" entry point: drop messy tracker archives (nested
 *  zips, Shift-JIS names, multi-volume bundles) and let the master agent
 *  curate them. With a seriesId the upload is media for that existing series;
 *  without one the agent also identifies/creates the series. */
export default function AddFilesDialog({
  open,
  onOpenChange,
  seriesId,
  onStarted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  seriesId?: string;
  onStarted: (sessionId: string) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [note, setNote] = useState("");
  const [source, setSource] = useState(""); // URL or server-side path
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  function pick(list: FileList | null) {
    if (!list) return;
    setFiles((prev) => [...prev, ...Array.from(list)]);
  }

  const canStart = files.length > 0 || source.trim().length > 0;

  async function start() {
    if (!canStart) return;
    setBusy(true);
    try {
      const s = source.trim();
      const isUrl = /^https?:\/\//i.test(s);
      const { session_id } = await ingestFiles(
        files, seriesId, note,
        isUrl ? s : undefined,
        !isUrl && s ? s : undefined
      );
      setFiles([]);
      setNote("");
      setSource("");
      onOpenChange(false);
      onStarted(session_id);
    } catch (e) {
      const detail =
        (e as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      toast.error(detail ?? "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !busy && onOpenChange(v)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="size-5 text-primary" /> Add files
          </DialogTitle>
          <DialogDescription>
            Throw anything at it: raw scan archives (nested zips/rars, any language
            filenames), digital books (epub/azw3/pdf/txt/html), a webnovel URL, or a
            server folder path. The agent inspects the contents,{" "}
            {seriesId ? "organizes them into volumes of this series" :
              "identifies the series and organizes volumes"},
            and asks you before anything is translated.
          </DialogDescription>
        </DialogHeader>

        <div
          className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed py-8 text-sm text-muted-foreground hover:bg-muted/40"
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            pick(e.dataTransfer.files);
          }}
        >
          <Upload className="size-6" />
          Drop archives (zip/rar/7z), books (epub/azw3/pdf/txt/html) or images — or click
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".zip,.rar,.7z,.cbz,.cbr,.epub,.azw3,.mobi,.pdf,.txt,.html,image/*"
            className="hidden"
            onChange={(e) => pick(e.target.files)}
          />
        </div>

        <Input
          value={source}
          placeholder="…or paste a webnovel URL (Syosetu etc.) or a server folder path"
          onChange={(e) => setSource(e.target.value)}
        />

        {files.length > 0 && (
          <ul className="max-h-32 space-y-1 overflow-y-auto text-sm">
            {files.map((f, i) => (
              <li key={`${f.name}-${i}`} className="flex items-center gap-2">
                <FileArchive className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{f.name}</span>
                <span className="text-xs text-muted-foreground">
                  {(f.size / 1024 / 1024).toFixed(1)} MB
                </span>
                <button onClick={() => setFiles(files.filter((_, j) => j !== i))}>
                  <X className="size-3.5 text-muted-foreground hover:text-destructive" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <Textarea
          placeholder="Optional note for the agent (e.g. “this is volumes 4–6”, “use ocr_text engine”)…"
          value={note}
          rows={2}
          onChange={(e) => setNote(e.target.value)}
        />

        <Button disabled={!canStart || busy} onClick={() => void start()}>
          {busy ? (
            <>
              <Loader2 className="mr-1 size-4 animate-spin" /> Uploading & extracting…
            </>
          ) : (
            <>
              <Sparkles className="mr-1 size-4" /> Let the agent curate
            </>
          )}
        </Button>
      </DialogContent>
    </Dialog>
  );
}
