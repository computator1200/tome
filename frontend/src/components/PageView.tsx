import type { Page } from "@/types";
import { cn } from "@/lib/utils";

export default function PageView({
  page,
  showTranslation,
  overlayDim = 0.85,
  textSize = 15,
}: {
  page: Page;
  showTranslation: boolean;
  overlayDim?: number;
  textSize?: number;
}) {
  const isTextPage = page.page_type === "text" || page.page_type === "mixed";
  // Read regions in OCR order, which follows the source language reading direction.
  const lines = isTextPage
    ? page.regions.map((r) => r.english_text.trim()).filter(Boolean)
    : [];
  const showOverlay = showTranslation && lines.length > 0;

  // Digital (text) volumes have no page image — a "page" is a chapter. Render
  // it as a plain reading column: translation when available, source otherwise.
  if (!page.image_url) {
    const blocks = page.regions
      .map((r) => (showTranslation && r.english_text.trim()) || r.japanese_text.trim())
      .filter(Boolean);
    return (
      <div className="h-[calc(100vh-7rem)] w-[min(92vw,46rem)] overflow-y-auto rounded-lg border bg-card/40">
        <div
          className="mx-auto flex flex-col gap-3 px-8 py-10"
          style={{ fontFamily: "var(--font-serif)", fontSize: `${textSize}px` }}
        >
          {page.chapter_title && (
            <h2 className="mb-2 font-sans text-lg font-semibold">{page.chapter_title}</h2>
          )}
          {blocks.map((b, i) => (
            <p key={i} className="leading-relaxed text-foreground/90">
              {b}
            </p>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="relative inline-block max-h-full">
      <img
        src={page.image_url}
        alt={`Page ${page.index + 1}`}
        className={cn(
          "max-h-[calc(100vh-7rem)] w-auto select-none object-contain transition-opacity",
          showOverlay && "opacity-20"
        )}
        draggable={false}
      />
      {showOverlay && (
        // Simple, unfitted overlay: dim the scan and lay the translation on top
        // as a readable block. Avoids per-region bbox placement entirely.
        <div
          className="absolute inset-0 overflow-y-auto backdrop-blur-[2px]"
          // Theme-aware scrim: mixes the current background colour with the dim.
          style={{
            backgroundColor: `color-mix(in srgb, var(--background) ${Math.round(
              overlayDim * 100
            )}%, transparent)`,
          }}
        >
          <div
            className="mx-auto flex min-h-full max-w-[42ch] flex-col gap-3 px-6 py-8"
            style={{ fontFamily: "var(--font-serif)", fontSize: `${textSize}px` }}
          >
            {lines.map((line, i) => (
              <p key={i} className="leading-relaxed text-foreground/90">
                {line}
              </p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
