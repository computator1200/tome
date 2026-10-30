import { useEffect, useState } from "react";
import { Loader2, Square } from "lucide-react";
import { toast } from "sonner";
import type { VolumeSummary } from "@/types";
import { cancelVolume } from "@/lib/api";
import { useProcessing } from "@/hooks/useProcessing";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

const STEP_LABELS: Record<string, string> = {
  classifying: "Classifying pages",
  ocr: "Reading text (OCR)",
  vision: "Vision OCR + translating",
  translating: "Translating",
  summarizing: "Summarizing volume",
  done: "Complete",
  error: "Error",
};

export default function ProcessingPanel({
  volume,
  onDone,
}: {
  volume: VolumeSummary;
  onDone: () => void;
}) {
  // Seed from the persisted job so a fresh load (refresh) shows progress
  // immediately, then the SSE stream takes over.
  const { progress, done } = useProcessing(volume.id, true);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (done) onDone();
  }, [done, onDone]);

  async function stop() {
    setStopping(true);
    try {
      await cancelVolume(volume.id);
      toast.info("Stopping after the current page. Progress is kept.");
    } catch {
      toast.error("Could not stop.");
      setStopping(false);
    }
  }

  const job = progress ?? volume.job ?? null;
  const step = job?.step || (volume.status === "queued" ? "queued" : "");
  const pct = job && job.total > 0 ? Math.round((job.current / job.total) * 100) : 0;

  return (
    <div className="space-y-3 rounded-lg border bg-card p-4">
      <div className="flex items-center gap-2">
        <Loader2 className="size-4 animate-spin text-primary" />
        <span className="text-sm font-medium">{STEP_LABELS[step] ?? "Queued"}</span>
        {job && job.total > 0 && (
          <span className="ml-auto text-sm tabular-nums text-muted-foreground">
            {job.current} / {job.total}
          </span>
        )}
        <Button
          size="sm"
          variant="outline"
          className={cn(
            "border-amber-500/30 text-amber-400 hover:bg-amber-500/10",
            job && job.total > 0 ? "ml-2" : "ml-auto"
          )}
          onClick={stop}
          disabled={stopping}
        >
          <Square className="mr-1 size-3.5" />
          {stopping ? "Stopping..." : "Stop"}
        </Button>
      </div>

      <Progress value={pct} />

      {job?.message && (
        <p className="truncate text-xs text-muted-foreground">{job.message}</p>
      )}
    </div>
  );
}
