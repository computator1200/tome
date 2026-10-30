import { useState } from "react";
import { toast } from "sonner";
import { ChevronDown, Merge, Scissors, Trash2 } from "lucide-react";
import {
  deleteMemoryEdge, deleteMemoryEntity, getMemoryEntities, mergeMemoryEntities, splitMemoryAlias,
  type MemoryEntity, type SeriesMemory,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";

/**
 * The series graph, and the controls to correct it. Extraction is a model's
 * reading of the page, so it can merge two people, split one, or invent a
 * relation; each of those has a one-click fix here.
 */
export default function MemoryPanel({
  seriesId,
  memory,
  onChanged,
}: {
  seriesId: string;
  memory: SeriesMemory;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [entities, setEntities] = useState<MemoryEntity[] | null>(null);
  const [mergeTarget, setMergeTarget] = useState<Record<string, string>>({});

  async function loadEntities() {
    try {
      setEntities(await getMemoryEntities(seriesId));
    } catch {
      toast.error("Could not load the series memory.");
    }
  }

  async function act(label: string, fn: () => Promise<void>) {
    try {
      await fn();
      toast.success(label);
      await loadEntities();
      onChanged();
    } catch {
      toast.error(`Could not ${label.toLowerCase()}.`);
    }
  }

  function startEditing() {
    setEditing(true);
    void loadEntities();
  }

  return (
    <div className="mt-8 rounded-lg border">
      <button
        className="flex w-full items-center justify-between px-4 py-3 text-sm"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="font-medium">
          Series memory
          <span className="ml-2 font-normal text-muted-foreground">
            {memory.stats.entities} things · {memory.stats.edges} relations
          </span>
        </span>
        <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="space-y-4 border-t px-4 py-3 text-sm">
          <p className="text-muted-foreground">
            Built while translating. Each name and relation is used only from the page that
            established it onward, so later pages and books cannot spoil earlier ones.
          </p>
          <div>
            <p className="mb-1 font-medium">Relations</p>
            <ul className="space-y-0.5 text-muted-foreground">
              {memory.edges.slice(0, 20).map((e) => (
                <li key={e.id} className="group flex items-center gap-2">
                  <span>
                    {e.src} {e.relation.replace(/_/g, " ")} {e.dst}
                    <span className="ml-2 opacity-60">vol {e.volume}</span>
                  </span>
                  <button
                    className="opacity-0 transition-opacity group-hover:opacity-100"
                    title="Delete this relation"
                    onClick={() => void act("Relation deleted", () => deleteMemoryEdge(seriesId, e.id))}
                  >
                    <Trash2 className="size-3.5 text-destructive" />
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <p className="mb-1 font-medium">Known by the latest volume</p>
            <p className="text-muted-foreground">
              {memory.entities.slice(0, 40).map((e) => (e.rendering ? `${e.name} (${e.rendering})` : e.name)).join(", ")}
            </p>
          </div>
          {!editing ? (
            <Button size="sm" variant="outline" onClick={startEditing}>
              Correct the memory
            </Button>
          ) : (
            <div className="space-y-2">
              <p className="font-medium">Correct the memory</p>
              <p className="text-xs text-muted-foreground">
                Merge two entries that are one person or thing, split off a name that was merged by
                mistake, or delete an entry. Changes apply to the next translation run.
              </p>
              {!entities && <p className="text-muted-foreground">Loading...</p>}
              <ul className="divide-y rounded-md border">
                {entities?.map((e) => (
                  <li key={e.id} className="space-y-1.5 px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{e.name}</span>
                      <span className="text-xs text-muted-foreground">
                        {e.kind}{e.rendering ? ` · ${e.rendering}` : ""} · vol {e.volume}
                      </span>
                      <div className="ml-auto flex items-center gap-1">
                        <Select
                          value={mergeTarget[e.id] ?? ""}
                          onValueChange={(v) => setMergeTarget((m) => ({ ...m, [e.id]: v }))}
                        >
                          <SelectTrigger className="h-7 w-40 text-xs">
                            <SelectValue placeholder="Merge into..." />
                          </SelectTrigger>
                          <SelectContent>
                            {entities
                              .filter((o) => o.id !== e.id)
                              .map((o) => (
                                <SelectItem key={o.id} value={o.id}>{o.name}</SelectItem>
                              ))}
                          </SelectContent>
                        </Select>
                        <Button
                          size="icon" variant="ghost" className="size-7" title="Merge"
                          disabled={!mergeTarget[e.id]}
                          onClick={() => void act("Merged", () => mergeMemoryEntities(seriesId, mergeTarget[e.id], e.id))}
                        >
                          <Merge className="size-3.5" />
                        </Button>
                        <Button
                          size="icon" variant="ghost" className="size-7 text-destructive" title="Delete"
                          onClick={() => void act("Deleted", () => deleteMemoryEntity(seriesId, e.id))}
                        >
                          <Trash2 className="size-3.5" />
                        </Button>
                      </div>
                    </div>
                    {e.aliases.length > 1 && (
                      <div className="flex flex-wrap gap-1.5">
                        {e.aliases.map((a) => (
                          <span key={a.id} className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-xs">
                            {a.alias}
                            <button
                              title="Split this name into its own entry"
                              onClick={() => void act("Split", () => splitMemoryAlias(seriesId, a.id))}
                            >
                              <Scissors className="size-3" />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
