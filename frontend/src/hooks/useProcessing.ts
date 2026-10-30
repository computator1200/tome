import { useEffect, useState } from "react";
import type { Progress } from "@/types";
import { createProgressStream } from "@/lib/api";

/** Subscribes to a volume's SSE progress stream. Because the backend serves
 *  the current Job row on connect, a page refresh resumes the live bar
 *  instead of going blank. */
export function useProcessing(volumeId: string | undefined, enabled: boolean) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!volumeId || !enabled) return;
    setDone(false);
    const close = createProgressStream(
      volumeId,
      (p) => setProgress(p),
      () => setDone(true)
    );
    return close;
  }, [volumeId, enabled]);

  return { progress, done };
}
