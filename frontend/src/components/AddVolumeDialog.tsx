import { useEffect, useRef, useState } from "react";
import { FileArchive, Loader2, Upload } from "lucide-react";
import { toast } from "sonner";
import type { EngineMode } from "@/types";
import { ingestFiles, uploadVolume } from "@/lib/api";
import { useNavigate } from "react-router-dom";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export default function AddVolumeDialog({
  seriesId,
  nextNumber,
  open,
  onOpenChange,
  onAdded,
}: {
  seriesId: string;
  nextNumber: number;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onAdded: () => void;
}) {
  const navigate = useNavigate();
  const [engine, setEngine] = useState<EngineMode>("vision");
  const [batchSize, setBatchSize] = useState(5);
  const [number, setNumber] = useState(nextNumber);
  const [fileName, setFileName] = useState<string | null>(null);
  const fileRef = useRef<File[]>([]);
  const [busy, setBusy] = useState(false);

  // Keep the default in sync when the dialog opens for a series.
  useEffect(() => {
    if (open) setNumber(nextNumber);
  }, [open, nextNumber]);

  async function submit() {
    const files = fileRef.current;
    if (files.length === 0) return;
    setBusy(true);
    try {
      const archives = files.filter((f) => f.name.toLowerCase().endsWith(".zip"));
      const documents = files.filter((f) => !f.name.toLowerCase().endsWith(".zip"));
      let next = number;
      for (const file of archives) {
        await uploadVolume(seriesId, file, engine, batchSize, next);
        next += 1;
      }
      if (archives.length > 0) {
        toast.success(archives.length > 1
          ? `${archives.length} volumes added — processing started.`
          : "Volume added — processing started.");
        onAdded();
      }
      if (documents.length > 0) {
        // Documents go to the librarian, which reads them, finds the chapters
        // and decides how they become volumes of this series.
        const { session_id } = await ingestFiles(documents, seriesId);
        toast.success(`Reading ${documents.length} file(s) — the librarian will organise them.`);
        onOpenChange(false);
        navigate(`/import/${session_id}`);
        return;
      }
      onOpenChange(false);
      setFileName(null);
      fileRef.current = [];
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      toast.error(typeof detail === "string" && detail ? detail : "Upload failed. Check the file and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a volume</DialogTitle>
          <DialogDescription>Drop any files — zips of scans, ebooks, PDFs, text. The librarian attaches them to this series.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>Volume number</Label>
            <Input
              type="number"
              min={1}
              value={number}
              onChange={(e) => setNumber(Math.max(1, Number(e.target.value) || 1))}
            />
            <p className="text-xs text-muted-foreground">
              Sets the order, e.g. upload volume 5 even if 1-4 live elsewhere.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Engine</label>
              <Select value={engine} onValueChange={(v) => setEngine(v as EngineMode)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="vision">Vision (one-pass)</SelectItem>
                  <SelectItem value="ocr_text">local OCR + text</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Batch size</label>
              <Select value={String(batchSize)} onValueChange={(v) => setBatchSize(Number(v))}>
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
          </div>

          <label
            className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-8 text-center transition-colors hover:border-primary/50"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const fs = Array.from(e.dataTransfer.files);
              if (fs.length) {
                fileRef.current = fs;
                setFileName(fs.map((x) => x.name).join(", "));
              }
            }}
          >
            <div className="flex size-12 items-center justify-center rounded-full bg-accent">
              <Upload className="size-6 text-primary" />
            </div>
            {fileName ? (
              <p className="flex items-center gap-2 text-sm font-medium">
                <FileArchive className="size-4" /> {fileName}
              </p>
            ) : (
              <p className="text-sm font-medium">Drop a .zip, or click</p>
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
                  setFileName(fs.map((x) => x.name).join(", "));
                }
              }}
            />
          </label>
        </div>
        <DialogFooter>
          <Button onClick={submit} disabled={busy || !fileName}>
            {busy && <Loader2 className="mr-1 size-4 animate-spin" />}
            Start processing
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
