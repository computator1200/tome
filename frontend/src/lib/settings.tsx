import { createContext, useCallback, useContext, useEffect, useState } from "react";

export interface Settings {
  /** Page turn direction. LTR is the default since most readers expect it. */
  readingDirection: "ltr" | "rtl";
  /** Whether the translation overlay starts visible when opening the reader. */
  showTranslationByDefault: boolean;
  /** Opacity of the dark scrim drawn over the scan behind the translation (0.5 to 1). */
  overlayDim: number;
  /** Translation text size in pixels. */
  textSize: number;
  /** Set each translation where its source text sits, when the page has boxes. */
  placeText: boolean;
  /** Theme accent color as a CSS color (any valid value). Empty = theme default. */
  accentColor: string;
  /** Light or dark theme. */
  themeMode: "light" | "dark";
}

// Default accent is white (matches the favicon, looks clean in dark mode).
export const DEFAULT_ACCENT = "oklch(1 0 0)";

export const DEFAULT_SETTINGS: Settings = {
  readingDirection: "ltr",
  showTranslationByDefault: true,
  overlayDim: 0.85,
  textSize: 15,
  placeText: true,
  accentColor: DEFAULT_ACCENT,
  themeMode: "dark",
};

/** A readable foreground (text) color for a given accent, by luminance. Keeps
 *  e.g. a white accent's button text dark (visible) regardless of theme. */
export function contrastFor(color: string): string {
  let l: number | null = null;
  const ok = color.match(/oklch\(\s*([\d.]+)/i);
  if (ok) l = parseFloat(ok[1]);
  else {
    const hex = color.match(/^#?([0-9a-f]{6})$/i);
    if (hex) {
      const n = parseInt(hex[1], 16);
      l = (0.2126 * (n >> 16 & 255) + 0.7152 * (n >> 8 & 255) + 0.0722 * (n & 255)) / 255;
    }
  }
  return l !== null && l > 0.6 ? "oklch(0.205 0 0)" : "oklch(0.985 0 0)";
}

/** Apply the accent color (and a matching foreground) to the theme variables. */
function applyAccent(color: string) {
  const root = document.documentElement;
  const value = color || DEFAULT_ACCENT;
  root.style.setProperty("--primary", value);
  root.style.setProperty("--ring", value);
  root.style.setProperty("--primary-foreground", contrastFor(value));
}

/** Toggle the `dark` class that switches the theme token set in index.css. */
function applyThemeMode(mode: "light" | "dark") {
  document.documentElement.classList.toggle("dark", mode === "dark");
}

const STORAGE_KEY = "ln-reader-settings";

function load(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
  } catch {
    // ignore malformed storage
  }
  return DEFAULT_SETTINGS;
}

interface SettingsContextValue {
  settings: Settings;
  update: (patch: Partial<Settings>) => void;
  reset: () => void;
}

const SettingsContext = createContext<SettingsContextValue | null>(null);

export function SettingsProvider({ children }: { children: React.ReactNode }) {
  const [settings, setSettings] = useState<Settings>(load);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch {
      // storage may be unavailable; settings still apply for the session
    }
  }, [settings]);

  // Keep the theme accent in sync with the saved setting.
  useEffect(() => {
    applyAccent(settings.accentColor);
  }, [settings.accentColor]);

  // Keep light/dark in sync.
  useEffect(() => {
    applyThemeMode(settings.themeMode);
  }, [settings.themeMode]);

  const update = useCallback((patch: Partial<Settings>) => {
    setSettings((s) => ({ ...s, ...patch }));
  }, []);

  const reset = useCallback(() => setSettings(DEFAULT_SETTINGS), []);

  return (
    <SettingsContext.Provider value={{ settings, update, reset }}>
      {children}
    </SettingsContext.Provider>
  );
}

export function useSettings() {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within SettingsProvider");
  return ctx;
}
