import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Check, Loader2, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { DEFAULT_ACCENT, contrastFor, useSettings } from "@/lib/settings";
import {
  getProviderSettings,
  testProvider,
  updateProviderSettings,
  type ProviderSettings,
  type ProviderUpdate,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

const ACCENTS = [
  { name: "White", value: DEFAULT_ACCENT },
  { name: "Violet", value: "oklch(0.72 0.18 305)" },
  { name: "Blue", value: "oklch(0.68 0.16 245)" },
  { name: "Emerald", value: "oklch(0.70 0.15 160)" },
  { name: "Cyan", value: "oklch(0.72 0.13 200)" },
  { name: "Rose", value: "oklch(0.70 0.18 15)" },
  { name: "Amber", value: "oklch(0.78 0.15 75)" },
];

export default function Settings() {
  const navigate = useNavigate();
  const { settings, update, reset } = useSettings();

  return (
    <div className="mx-auto max-w-2xl space-y-6 px-6 py-6">
      <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
        <ArrowLeft className="mr-1 size-4" /> Library
      </Button>

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <Button variant="outline" size="sm" onClick={reset}>
          <RotateCcw className="mr-1 size-3.5" /> Reset reader defaults
        </Button>
      </div>

      {/* --- Appearance --- */}
      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
          <CardDescription>Theme and accent color. Saved on this device.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <Label>Theme</Label>
              <p className="text-sm text-muted-foreground">Light or dark. Also sets the PDF export.</p>
            </div>
            <Select
              value={settings.themeMode}
              onValueChange={(v) => update({ themeMode: v as "light" | "dark" })}
            >
              <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="dark">Dark</SelectItem>
                <SelectItem value="light">Light</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Separator />
          <Label className="text-sm">Accent color</Label>
          <div className="flex flex-wrap items-center gap-2">
            {ACCENTS.map((a) => (
              <button
                key={a.name}
                title={a.name}
                onClick={() => update({ accentColor: a.value })}
                className={cn(
                  "flex size-9 items-center justify-center rounded-full border transition",
                  settings.accentColor === a.value ? "border-foreground" : "border-border"
                )}
                style={{ backgroundColor: a.value }}
              >
                {settings.accentColor === a.value && (
                  <Check className="size-4" style={{ color: contrastFor(a.value) }} />
                )}
              </button>
            ))}
            <label className="ml-2 flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
              <input
                type="color"
                className="size-9 cursor-pointer rounded border bg-transparent"
                onChange={(e) => update({ accentColor: e.target.value })}
              />
              Custom
            </label>
          </div>
        </CardContent>
      </Card>

      {/* --- AI Provider --- */}
      <ProviderCard />

      {/* --- Reader --- */}
      <Card>
        <CardHeader>
          <CardTitle>Reader</CardTitle>
          <CardDescription>
            Defaults for the reader (also adjustable inside the reader).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="flex items-center justify-between gap-4">
            <div>
              <Label>Reading direction</Label>
              <p className="text-sm text-muted-foreground">Which way the pages turn.</p>
            </div>
            <Select
              value={settings.readingDirection}
              onValueChange={(v) => update({ readingDirection: v as "ltr" | "rtl" })}
            >
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ltr">Left to right</SelectItem>
                <SelectItem value="rtl">Right to left</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Separator />
          <div className="flex items-center justify-between gap-4">
            <div>
              <Label>Show translation by default</Label>
              <p className="text-sm text-muted-foreground">Start volumes with the overlay visible.</p>
            </div>
            <Switch
              checked={settings.showTranslationByDefault}
              onCheckedChange={(v) => update({ showTranslationByDefault: v })}
            />
          </div>
          <Separator />
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label>Overlay dimming</Label>
              <span className="text-sm tabular-nums text-muted-foreground">
                {Math.round(settings.overlayDim * 100)}%
              </span>
            </div>
            <Slider
              min={50}
              max={100}
              step={5}
              value={[Math.round(settings.overlayDim * 100)]}
              onValueChange={([v]) => update({ overlayDim: v / 100 })}
            />
          </div>
          <Separator />
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label>Translation text size</Label>
              <span className="text-sm tabular-nums text-muted-foreground">{settings.textSize}px</span>
            </div>
            <Slider
              min={12}
              max={22}
              step={1}
              value={[settings.textSize]}
              onValueChange={([v]) => update({ textSize: v })}
            />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function ProviderCard() {
  const [loaded, setLoaded] = useState<ProviderSettings | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [visionModel, setVisionModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [concurrency, setConcurrency] = useState(4);
  const [volumeConcurrency, setVolumeConcurrency] = useState(1);
  const [priceIn, setPriceIn] = useState("");
  const [priceOut, setPriceOut] = useState("");
  const [ocrBackend, setOcrBackend] = useState("vision");
  const [version, setVersion] = useState<{ version: string; commit: string } | null>(null);
  const [localOcr, setLocalOcr] = useState(true);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    void fetch(`${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/health`)
      .then((r) => (r.ok ? r.json() : null))
      .then((h) => h?.capabilities && setLocalOcr(!!h.capabilities.local_ocr))
      .catch(() => {
        /* assume it is there; the server will say so if it is not */
      });
    void fetch(`${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/version`)
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => v && setVersion(v))
      .catch(() => {
        /* the version line is a nicety; never break settings over it */
      });
  }, []);

  useEffect(() => {
    getProviderSettings()
      .then((s) => {
        setLoaded(s);
        setBaseUrl(s.base_url);
        setModel(s.model);
        setVisionModel(s.vision_model);
        setConcurrency(s.concurrency);
        setVolumeConcurrency(s.volume_concurrency);
        setPriceIn(s.price_in == null ? "" : String(s.price_in));
        setPriceOut(s.price_out == null ? "" : String(s.price_out));
        setOcrBackend(s.ocr_backend);
      })
      .catch(() => toast.error("Could not load provider settings."));
  }, []);

  function patch(): ProviderUpdate {
    const price = (v: string) => (v.trim() === "" || Number.isNaN(Number(v)) ? null : Number(v));
    const p: ProviderUpdate = {
      base_url: baseUrl, model, vision_model: visionModel, concurrency, ocr_backend: ocrBackend,
      volume_concurrency: volumeConcurrency, price_in: price(priceIn), price_out: price(priceOut),
    };
    if (apiKey) p.api_key = apiKey; // blank = keep existing
    return p;
  }

  async function save() {
    setBusy(true);
    try {
      const s = await updateProviderSettings(patch());
      setLoaded(s);
      setApiKey("");
      toast.success("Provider saved.");
    } catch {
      toast.error("Failed to save provider.");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setTesting(true);
    try {
      const r = await testProvider(patch());
      if (r.ok) toast.success(r.message);
      else toast.error(r.message);
    } finally {
      setTesting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>AI Provider</CardTitle>
        <CardDescription>
          OpenAI-compatible endpoint used for translation. Overrides the .env defaults.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label>Base URL</Label>
          <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://ollama.com/v1" />
        </div>
        <div className="space-y-1.5">
          <Label>API key</Label>
          <Input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={loaded?.api_key_set ? `Saved (${loaded.api_key_hint}) — leave blank to keep` : "Set an API key"}
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label>Text model</Label>
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="minimax-m3:cloud" />
          </div>
          <div className="space-y-1.5">
            <Label>Vision model</Label>
            <Input value={visionModel} onChange={(e) => setVisionModel(e.target.value)} placeholder="qwen3.5:cloud" />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>Concurrency</Label>
          <Input
            type="number"
            min={1}
            max={16}
            value={concurrency}
            onChange={(e) => setConcurrency(Math.max(1, Math.min(16, Number(e.target.value) || 1)))}
          />
          <p className="text-xs text-muted-foreground">
            Pages/batches translated in parallel. Higher is faster but uses more of the endpoint's
            rate limit.
          </p>
        </div>
        <div className="space-y-1.5">
          <Label>Volumes at once</Label>
          <Input
            type="number"
            min={1}
            max={8}
            value={volumeConcurrency}
            onChange={(e) => setVolumeConcurrency(Math.max(1, Math.min(8, Number(e.target.value) || 1)))}
          />
          <p className="text-xs text-muted-foreground">
            Volumes processed in parallel. Never two of the same series: each volume builds on the
            ones before it.
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Price per million input tokens (USD)</Label>
            <Input inputMode="decimal" value={priceIn} onChange={(e) => setPriceIn(e.target.value)} placeholder="unknown" />
          </div>
          <div className="space-y-1.5">
            <Label>Price per million output tokens (USD)</Label>
            <Input inputMode="decimal" value={priceOut} onChange={(e) => setPriceOut(e.target.value)} placeholder="unknown" />
          </div>
          <p className="text-xs text-muted-foreground sm:col-span-2">
            Optional. Token use is always counted per volume; with both prices set, each volume also
            shows an estimated cost.
          </p>
        </div>
        <div className="space-y-1.5">
          <Label>OCR method (local OCR + text engine)</Label>
          <Select value={ocrBackend} onValueChange={setOcrBackend}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="vision">Vision model (recommended)</SelectItem>
              {localOcr && <SelectItem value="manga_ocr">Local OCR</SelectItem>}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            How the "local OCR + text" engine reads pages: a vision model (accurate, no local
            install) or local local OCR (needs torch). Translation always uses the text model.
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={save} disabled={busy}>
            {busy && <Loader2 className="mr-1 size-4 animate-spin" />} Save
          </Button>
          <Button variant="outline" onClick={test} disabled={testing}>
            {testing && <Loader2 className="mr-1 size-4 animate-spin" />} Test connection
          </Button>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          {version
            ? `Tome ${version.version}${version.commit ? ` · ${version.commit.slice(0, 7)}` : ""}`
            : "Tome"}
        </p>
      </CardContent>
    </Card>
  );
}
