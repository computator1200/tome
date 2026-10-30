import axios from "axios";
import type {
  AgentMessage,
  AgentSession,
  AgentSessionDetail,
  EngineMode,
  MetadataSearch,
  NUSearchResult,
  Progress,
  SeriesDetail,
  SeriesSummary,
  VolumeDetail,
} from "@/types";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

// The app may be deployed under a subpath (e.g. /tome), so every
// request is built from Vite's BASE_URL rather than an absolute /api.
const api = axios.create({ baseURL: `${BASE}/api` });

// --- NovelUpdates -----------------------------------------------------------

export async function searchNovelUpdates(q: string): Promise<NUSearchResult[]> {
  const { data } = await api.get<NUSearchResult[]>("/novelupdates/search", {
    params: { q },
  });
  return data;
}

// --- Unified metadata across the book catalogues -------------------------------

export async function searchMetadata(q: string): Promise<MetadataSearch> {
  const { data } = await api.get<MetadataSearch>("/metadata/search", { params: { q } });
  return data;
}

export async function linkSeriesMetadata(
  seriesId: string,
  source: string,
  ref: string
): Promise<SeriesDetail> {
  const { data } = await api.post<SeriesDetail>(`/series/${seriesId}/metadata/link`, {
    source,
    ref,
  });
  return data;
}

// --- Series -----------------------------------------------------------------

export async function listSeries(): Promise<SeriesSummary[]> {
  const { data } = await api.get<SeriesSummary[]>("/series");
  return data;
}

export async function getSeries(id: string): Promise<SeriesDetail> {
  const { data } = await api.get<SeriesDetail>(`/series/${id}`);
  return data;
}

export async function createSeriesFromNU(nu_url: string): Promise<SeriesDetail> {
  const { data } = await api.post<SeriesDetail>("/series", { nu_url });
  return data;
}

export async function createManualSeries(
  title: string,
  description: string,
  sourceLanguage: string,
  targetLanguage: string
): Promise<SeriesDetail> {
  const { data } = await api.post<SeriesDetail>("/series", {
    manual: {
      title,
      description,
      source_language: sourceLanguage,
      target_language: targetLanguage,
    },
  });
  return data;
}

export async function patchSeries(
  id: string,
  patch: {
    title?: string;
    description?: string;
    source_language?: string;
    target_language?: string;
  }
): Promise<SeriesDetail> {
  const { data } = await api.patch<SeriesDetail>(`/series/${id}`, patch);
  return data;
}

export async function uploadPoster(id: string, file: File): Promise<SeriesDetail> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<SeriesDetail>(`/series/${id}/poster`, form);
  return data;
}

export async function deleteSeries(id: string): Promise<void> {
  await api.delete(`/series/${id}`);
}

// --- Volumes ----------------------------------------------------------------

export async function uploadVolume(
  seriesId: string,
  file: File,
  engineMode: EngineMode,
  batchSize: number,
  number?: number,
  label?: string
): Promise<{ volume_id: string; series_id: string }> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post(`/series/${seriesId}/volumes`, form, {
    params: { engine_mode: engineMode, batch_size: batchSize, number, label },
  });
  return data;
}

export async function getVolume(volumeId: string): Promise<VolumeDetail> {
  const { data } = await api.get<VolumeDetail>(`/volumes/${volumeId}`);
  return data;
}

export async function reprocessVolume(
  volumeId: string,
  engineMode: EngineMode,
  batchSize: number
): Promise<void> {
  await api.post(`/volumes/${volumeId}/process`, {
    engine_mode: engineMode,
    batch_size: batchSize,
  });
}

export type SeriesProcessResult = {
  queued: { volume_id: string; label: string; order: number }[];
  skipped: { volume_id: string; label: string; reason: string }[];
  total: number;
};

/** Queue every volume of a series that is not already running or finished. */
export async function processSeries(
  seriesId: string,
  engineMode: EngineMode,
  batchSize: number
): Promise<SeriesProcessResult> {
  const { data } = await api.post<SeriesProcessResult>(`/series/${seriesId}/process`, {
    engine_mode: engineMode,
    batch_size: batchSize,
  });
  return data;
}

export async function cancelVolume(volumeId: string): Promise<void> {
  await api.post(`/volumes/${volumeId}/cancel`);
}

export async function resumeVolume(volumeId: string): Promise<void> {
  await api.post(`/volumes/${volumeId}/resume`);
}

export async function deleteVolume(volumeId: string): Promise<void> {
  await api.delete(`/volumes/${volumeId}`);
}

// --- Settings (AI provider) -------------------------------------------------

export interface ProviderSettings {
  base_url: string;
  model: string;
  vision_model: string;
  timeout: number;
  concurrency: number;
  ocr_backend: string;
  price_in: number | null;
  price_out: number | null;
  volume_concurrency: number;
  api_key_set: boolean;
  api_key_hint: string;
}

export interface ProviderUpdate {
  base_url?: string;
  model?: string;
  vision_model?: string;
  timeout?: number;
  concurrency?: number;
  ocr_backend?: string;
  price_in?: number | null;
  price_out?: number | null;
  volume_concurrency?: number;
  api_key?: string;
}

export async function getProviderSettings(): Promise<ProviderSettings> {
  const { data } = await api.get<ProviderSettings>("/settings");
  return data;
}

export async function updateProviderSettings(patch: ProviderUpdate): Promise<ProviderSettings> {
  const { data } = await api.put<ProviderSettings>("/settings", patch);
  return data;
}

export async function testProvider(patch?: ProviderUpdate): Promise<{ ok: boolean; message: string }> {
  const { data } = await api.post<{ ok: boolean; message: string }>("/settings/test", patch ?? {});
  return data;
}

// --- Master agent (ingest curation + series chat) ----------------------------

export async function ingestFiles(
  files: File[],
  seriesId?: string,
  note?: string,
  url?: string,
  localPath?: string
): Promise<{ session_id: string }> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  if (seriesId) form.append("series_id", seriesId);
  if (note) form.append("note", note);
  if (url) form.append("url", url);
  if (localPath) form.append("local_path", localPath);
  const { data } = await api.post<{ session_id: string }>("/ingest", form);
  return data;
}

export async function listSeriesAgentSessions(seriesId: string): Promise<AgentSession[]> {
  const { data } = await api.get<AgentSession[]>(`/series/${seriesId}/agent/sessions`);
  return data;
}

export async function newChatSession(seriesId: string): Promise<AgentSessionDetail> {
  const { data } = await api.post<AgentSessionDetail>(`/series/${seriesId}/chat/new`);
  return data;
}

export async function getAgentSession(sessionId: string): Promise<AgentSessionDetail> {
  const { data } = await api.get<AgentSessionDetail>(`/agent/sessions/${sessionId}`);
  return data;
}

export async function sendAgentMessage(
  sessionId: string,
  content: string
): Promise<AgentSession> {
  const { data } = await api.post<AgentSession>(`/agent/sessions/${sessionId}/message`, {
    content,
  });
  return data;
}

export async function cancelAgentSession(sessionId: string): Promise<void> {
  await api.post(`/agent/sessions/${sessionId}/cancel`);
}

export async function getSeriesChat(seriesId: string): Promise<AgentSessionDetail> {
  const { data } = await api.get<AgentSessionDetail>(`/series/${seriesId}/chat`);
  return data;
}

export async function getAgentAlerts(): Promise<AgentSession[]> {
  const { data } = await api.get<AgentSession[]>("/agent/alerts");
  return data;
}

/** SSE feed of an agent run: new messages + session status. The server closes
 *  the stream once the run is no longer `running`. */
export function createAgentStream(
  sessionId: string,
  after: number,
  onMessage: (m: AgentMessage) => void,
  onStatus: (s: AgentSession) => void,
  onClose: () => void
): () => void {
  const es = new EventSource(`${BASE}/api/agent/sessions/${sessionId}/stream?after=${after}`);
  es.addEventListener("message", (e) => {
    onMessage(JSON.parse((e as MessageEvent).data) as AgentMessage);
  });
  es.addEventListener("status", (e) => {
    const s = JSON.parse((e as MessageEvent).data) as AgentSession;
    onStatus(s);
    if (s.status !== "running") {
      es.close();
      onClose();
    }
  });
  es.onerror = () => {
    es.close();
    onClose();
  };
  return () => es.close();
}

// --- Progress (SSE) ---------------------------------------------------------

export function createProgressStream(
  volumeId: string,
  onEvent: (p: Progress) => void,
  onDone: () => void
): () => void {
  const es = new EventSource(`${BASE}/api/volumes/${volumeId}/progress`);
  es.addEventListener("progress", (e) => {
    const data = JSON.parse((e as MessageEvent).data) as Progress;
    onEvent(data);
    if (data.status === "done" || data.status === "error" || data.status === "cancelled") {
      es.close();
      onDone();
    }
  });
  es.onerror = () => es.close();
  return () => es.close();
}

export type SeriesMemory = {
  stats: { entities: number; edges: number; aliases: number };
  scoped_to_volume: number;
  entities: {
    id: string; name: string; kind: string; volume: number;
    aliases: string[]; rendering: string;
  }[];
  edges: { id: string; src: string; relation: string; dst: string; volume: number }[];
};

/** How consistently a volume used the series glossary (GET /volumes/{id}/quality). */
export type VolumeQuality = {
  volume: number;
  uses: number;
  misses: number;
  consistency: number | null;
  drift: { jp: string; en: string; kind: string; uses: number; misses: number; pages: number[] }[];
  new_terms: { jp: string; en: string; kind: string }[];
  new_term_count: number;
};

export async function getVolumeQuality(volumeId: string): Promise<VolumeQuality> {
  const { data } = await api.get<VolumeQuality>(`/volumes/${volumeId}/quality`);
  return data;
}

/** One entity with every name it has, for correcting the graph. */
export type MemoryEntity = {
  id: string; name: string; kind: string; rendering: string; volume: number;
  aliases: { id: string; alias: string; volume: number; page: number }[];
};

export async function getMemoryEntities(seriesId: string): Promise<MemoryEntity[]> {
  const { data } = await api.get<MemoryEntity[]>(`/series/${seriesId}/memory/entities`);
  return data;
}

export async function mergeMemoryEntities(seriesId: string, keepId: string, dropId: string) {
  await api.post(`/series/${seriesId}/memory/merge`, { keep_id: keepId, drop_id: dropId });
}

export async function splitMemoryAlias(seriesId: string, aliasId: string) {
  await api.post(`/series/${seriesId}/memory/aliases/${aliasId}/split`);
}

export async function deleteMemoryEntity(seriesId: string, entityId: string) {
  await api.delete(`/series/${seriesId}/memory/entities/${entityId}`);
}

export async function deleteMemoryEdge(seriesId: string, edgeId: string) {
  await api.delete(`/series/${seriesId}/memory/edges/${edgeId}`);
}

/** The series graph, optionally scoped to what a reader at `volume` may know. */
export async function getSeriesMemory(id: string, volume?: number): Promise<SeriesMemory> {
  const { data } = await api.get<SeriesMemory>(`/series/${id}/memory`, {
    params: volume ? { volume } : undefined,
  });
  return data;
}
