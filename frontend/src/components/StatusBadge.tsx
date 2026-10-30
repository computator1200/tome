import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { VolumeStatus } from "@/types";

const LABELS: Record<VolumeStatus, string> = {
  uploaded: "Not processed",
  queued: "Queued",
  processing: "Processing",
  paused: "Paused",
  done: "Ready",
  error: "Error",
};

const STYLES: Record<VolumeStatus, string> = {
  uploaded: "bg-muted text-muted-foreground",
  queued: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  processing: "bg-primary/15 text-primary border-primary/30",
  paused: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  done: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  error: "bg-destructive/15 text-destructive border-destructive/30",
};

export default function StatusBadge({
  status,
  className,
}: {
  status: VolumeStatus;
  className?: string;
}) {
  return (
    <Badge variant="outline" className={cn(STYLES[status], className)}>
      {(status === "processing" || status === "queued") && (
        <span className="mr-1 inline-block size-1.5 animate-pulse rounded-full bg-current" />
      )}
      {LABELS[status]}
    </Badge>
  );
}
