import { useLayoutEffect, useRef, useState } from "react";
import type { Page, Region } from "@/types";
import { cn } from "@/lib/utils";

/** Text that shrinks until it fits its box, from `max` down to 7px. */
function FitText({ text, max }: { text: string; max: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState(max);
  useLayoutEffect(() => setSize(max), [text, max]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && size > 7 && el.scrollHeight > el.clientHeight + 1) setSize((s) => s - 0.5);
  }, [size, text]);
  return (
    <div ref={ref} className="h-full w-full overflow-hidden leading-tight" style={{ fontSize: `${size}px` }}>
      {text}
    </div>
  );
}

/** Translated regions with boxes, or none unless every translated region has one. */
function placedRegions(regions: Region[]): Region[] {
  const translated = regions.filter((r) => r.english_text.trim());
  if (!translated.length || translated.some((r) => r.width <= 0 || r.height <= 0)) return [];
  return translated;
}

export default function PageView({
  page,
  showTranslation,
  overlayDim = 0.85,
  textSize = 15,
  placeText = true,
}: {
  page: Page;
  showTranslation: boolean;
  overlayDim?: number;
  textSize?: number;
  placeText?: boolean;
}) {
  const isTextPage = page.page_type === "text" || page.page_type === "mixed";
  // Read regions in OCR order, which follows the source language reading direction.
  const lines = isTextPage
    ? page.regions.map((r) => r.english_text.trim()).filter(Boolean)
    : [];
  const showOverlay = showTranslation && lines.length > 0;
  const placed = showOverlay && placeText ? placedRegions(page.regions) : [];

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

  if (placed.length) {
    // Each translation covers its source block, so the art around it stays visible.
    return (
      <div className="relative inline-block max-h-full">
        <img
          src={page.image_url}
          alt={`Page ${page.index + 1}`}
          className="max-h-[calc(100vh-7rem)] w-auto select-none object-contain"
          draggable={false}
        />
        {placed.map((r) => (
          <div
            key={r.id}
            className="absolute rounded-sm p-0.5 text-foreground"
            style={{
              left: `${r.x}%`,
              top: `${r.y}%`,
              width: `${r.width}%`,
              height: `${r.height}%`,
              fontFamily: "var(--font-serif)",
              backgroundColor: `color-mix(in srgb, var(--background) ${Math.round(overlayDim * 100)}%, transparent)`,
            }}
          >
            <FitText text={r.english_text} max={textSize} />
          </div>
        ))}
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
        // No boxes for this page: dim the scan and lay the translation on top
        // as one readable column.
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
