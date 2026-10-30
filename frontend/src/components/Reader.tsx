import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import "swiper/css";
import {
  ArrowLeft,
  Bookmark,
  BookmarkCheck,
  ChevronLeft,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  Loader2,
  Settings2,
} from "lucide-react";
import type { VolumeDetail } from "@/types";
import { getVolume } from "@/lib/api";
import { useSettings } from "@/lib/settings";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import PageView from "./PageView";

// --- per-volume persistence (localStorage) ---------------------------------
const lastPageKey = (id: string) => `tome-progress:${id}`;
const bookmarksKey = (id: string) => `tome-bookmarks:${id}`;

function readNum(key: string, fallback = 0): number {
  const v = Number(localStorage.getItem(key));
  return Number.isFinite(v) ? v : fallback;
}
function readBookmarks(id: string): number[] {
  try {
    return JSON.parse(localStorage.getItem(bookmarksKey(id)) || "[]");
  } catch {
    return [];
  }
}

export default function Reader() {
  const { volumeId } = useParams<{ volumeId: string }>();
  const navigate = useNavigate();
  const { settings, update } = useSettings();

  const [volume, setVolume] = useState<VolumeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [current, setCurrent] = useState(0);
  const [bookmarks, setBookmarks] = useState<number[]>([]);
  const [gotoOpen, setGotoOpen] = useState(false);
  const [gotoValue, setGotoValue] = useState("");
  // reading direction is stored but the reader no longer swipes
  const showTranslation = settings.showTranslationByDefault;

  useEffect(() => {
    if (!volumeId) return;
    getVolume(volumeId)
      .then((v) => {
        setVolume(v);
        const saved = Math.min(Math.max(0, readNum(lastPageKey(volumeId))), Math.max(0, v.pages.length - 1));
        setCurrent(saved);
        setBookmarks(readBookmarks(volumeId));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [volumeId]);

  // Persist last-read page.
  useEffect(() => {
    if (volumeId && volume) localStorage.setItem(lastPageKey(volumeId), String(current));
  }, [current, volumeId, volume]);

  const goTo = useCallback((index: number) => {
    setCurrent(index);
  }, []);
  const prev = useCallback(() => setCurrent((i) => Math.max(0, i - 1)), []);
  const next = useCallback(
    () => setCurrent((i) => Math.min((volume?.pages.length ?? 1) - 1, i + 1)),
    [volume?.pages.length]);

  // Arrow-key navigation (window-level, so it works without focusing the swiper).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      if (e.key === "ArrowRight") next();
      else if (e.key === "ArrowLeft") prev();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prev, next]);

  function toggleBookmark() {
    if (!volumeId) return;
    setBookmarks((prev) => {
      const next = prev.includes(current)
        ? prev.filter((p) => p !== current)
        : [...prev, current].sort((a, b) => a - b);
      localStorage.setItem(bookmarksKey(volumeId), JSON.stringify(next));
      return next;
    });
  }

  function submitGoto() {
    const n = Number(gotoValue);
    if (Number.isFinite(n) && n >= 1 && volume && n <= volume.pages.length) {
      goTo(n - 1);
      setGotoOpen(false);
      setGotoValue("");
    }
  }

  if (loading)
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    );

  if (error || !volume)
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4">
        <p className="text-destructive">{error ?? "Volume not found"}</p>
        <Button variant="outline" onClick={() => navigate(-1)}>
          Go back
        </Button>
      </div>
    );

  const bookmarked = bookmarks.includes(current);

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-2 border-b px-3 py-2">
        <Button variant="ghost" size="icon" onClick={() => navigate(-1)}>
          <ArrowLeft className="size-5" />
        </Button>
        <span className="truncate text-sm font-medium">{volume.label}</span>
        <Separator orientation="vertical" className="mx-1 h-5" />

        {/* Go-to-page */}
        <Popover open={gotoOpen} onOpenChange={setGotoOpen}>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="sm" className="tabular-nums text-muted-foreground">
              {current + 1} / {volume.pages.length}
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-44">
            <Label className="text-xs">Go to page</Label>
            <div className="mt-1.5 flex gap-2">
              <Input
                type="number"
                min={1}
                max={volume.pages.length}
                value={gotoValue}
                onChange={(e) => setGotoValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submitGoto()}
                autoFocus
              />
              <Button size="sm" onClick={submitGoto}>Go</Button>
            </div>
          </PopoverContent>
        </Popover>

        <div className="ml-auto flex items-center gap-1">
          {/* Quick translation toggle */}
          <Button variant="ghost" size="icon" onClick={() => update({ showTranslationByDefault: !showTranslation })}
            title={showTranslation ? "Hide translation" : "Show translation"}>
            {showTranslation ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
          </Button>

          {/* Bookmark current + list */}
          <Button variant="ghost" size="icon" onClick={toggleBookmark}
            title={bookmarked ? "Remove bookmark" : "Bookmark this page"}>
            {bookmarked ? <BookmarkCheck className="size-4 text-primary" /> : <Bookmark className="size-4" />}
          </Button>
          {bookmarks.length > 0 && (
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="ghost" size="sm" disabled={bookmarks.length === 0}>
                  {bookmarks.length}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-44">
                <Label className="text-xs">Bookmarks</Label>
                <div className="mt-1.5 flex max-h-60 flex-col gap-1 overflow-y-auto">
                  {bookmarks.length === 0 && <p className="text-xs text-muted-foreground">None yet.</p>}
                  {bookmarks.map((b) => (
                    <Button key={b} variant="ghost" size="sm" className="justify-start" onClick={() => goTo(b)}>
                      Page {b + 1}
                    </Button>
                  ))}
                </div>
              </PopoverContent>
            </Popover>
          )}

          {/* Scans export as PDF (theme-matched); digital volumes export as EPUB,
              because there are no page images to draw a PDF from. */}
          <Button variant="ghost" size="icon" asChild
                  title={volume.kind === "text" ? "Download as EPUB" : "Download as PDF"}>
            <a href={`${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/volumes/${volume.id}/export.${volume.kind === "text" ? "epub" : "pdf"}?theme=${settings.themeMode}`} download>
              <Download className="size-4" />
            </a>
          </Button>

          {/* In-reader settings */}
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" title="Reader settings">
                <Settings2 className="size-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-72 space-y-4">
              <div className="flex items-center justify-between gap-2">
                <Label className="text-sm">Direction</Label>
                <Select
                  value={settings.readingDirection}
                  onValueChange={(v) => update({ readingDirection: v as "ltr" | "rtl" })}
                >
                  <SelectTrigger className="h-8 w-32"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ltr">Left to right</SelectItem>
                    <SelectItem value="rtl">Right to left</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="flex items-center justify-between gap-2">
                <Label className="text-sm">Show translation</Label>
                <Switch checked={showTranslation} onCheckedChange={(v) => update({ showTranslationByDefault: v })} />
              </div>
              <div className="flex items-center justify-between gap-2">
                <Label className="text-sm">Dark mode</Label>
                <Switch
                  checked={settings.themeMode === "dark"}
                  onCheckedChange={(v) => update({ themeMode: v ? "dark" : "light" })}
                />
              </div>
              <div className="flex items-center justify-between gap-2">
                <Label className="text-sm">Place text on the page</Label>
                <Switch checked={settings.placeText} onCheckedChange={(v) => update({ placeText: v })} />
              </div>
              <div className="space-y-1.5">
                <div className="flex justify-between"><Label className="text-sm">Overlay dimming</Label>
                  <span className="text-xs tabular-nums text-muted-foreground">{Math.round(settings.overlayDim * 100)}%</span></div>
                <Slider min={50} max={100} step={5} value={[Math.round(settings.overlayDim * 100)]}
                  onValueChange={([v]) => update({ overlayDim: v / 100 })} />
              </div>
              <div className="space-y-1.5">
                <div className="flex justify-between"><Label className="text-sm">Text size</Label>
                  <span className="text-xs tabular-nums text-muted-foreground">{settings.textSize}px</span></div>
                <Slider min={12} max={22} step={1} value={[settings.textSize]}
                  onValueChange={([v]) => update({ textSize: v })} />
              </div>
            </PopoverContent>
          </Popover>
        </div>
      </header>

      <div className="relative flex-1 overflow-hidden bg-background">
        <div className="flex h-full w-full items-center justify-center">
          {volume.pages[current] && (
            <PageView
              page={volume.pages[current]}
              showTranslation={showTranslation}
              overlayDim={settings.overlayDim}
              textSize={settings.textSize}
              placeText={settings.placeText}
            />
          )}
        </div>

        {/* Click zones / nav arrows on the edges (chevron appears on hover). */}
        <button
          aria-label="Previous page"
          onClick={prev}
          disabled={current === 0}
          className="absolute inset-y-0 left-0 z-10 flex w-[12%] items-center justify-start pl-2 text-foreground/35 transition-colors hover:text-foreground/80"
        >
          <ChevronLeft className="size-8" />
        </button>
        <button
          aria-label="Next page"
          onClick={next}
          disabled={current >= volume.pages.length - 1}
          className="absolute inset-y-0 right-0 z-10 flex w-[12%] items-center justify-end pr-2 text-foreground/35 transition-colors hover:text-foreground/80"
        >
          <ChevronRight className="size-8" />
        </button>
      </div>
    </div>
  );
}
