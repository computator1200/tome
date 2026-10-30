export type EngineMode = "vision" | "ocr_text";

export type VolumeStatus =
  | "uploaded"
  | "queued"
  | "processing"
  | "paused"
  | "done"
  | "error";

export interface Region {
  id: string;
  order: number;
  x: number;
  y: number;
  width: number;
  height: number;
  japanese_text: string;
  english_text: string;
  confidence: number;
}

export interface Page {
  id: string;
  index: number;
  page_type: "text" | "illustration" | "mixed" | "other";
  chapter_title?: string;
  width: number;
  height: number;
  image_url: string;
  regions: Region[];
}

export interface Job {
  status: "queued" | "running" | "done" | "error";
  step: string;
  current: number;
  total: number;
  message: string;
  error: string;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  llm_seconds: number;
  wall_seconds: number;
  /** Estimated from the prices in Settings; null when they are not set. */
  cost_usd: number | null;
}

export interface VolumeSummary {
  id: string;
  label: string;
  order: number;
  status: VolumeStatus;
  engine_mode: EngineMode;
  kind: "scans" | "text";
  batch_size: number;
  page_count: number;
  summary: string;
  job?: Job | null;
}

export interface VolumeDetail extends VolumeSummary {
  pages: Page[];
}

export interface SeriesSummary {
  id: string;
  title: string;
  source: string; // the catalogue a series was linked from, or "manual"
  nu_url?: string | null;
  description: string;
  author?: string | null;
  genres: string[];
  nu_status?: string | null;
  year?: string | null;
  source_language: string;
  /** Filled in from the pages when source_language is "auto". */
  detected_language?: string;
  target_language: string;
  poster_url?: string | null;
  volume_count: number;
  page_count: number;
  status: VolumeStatus;
}

export interface SeriesDetail extends SeriesSummary {
  associated_names: string[];
  volumes: VolumeSummary[];
}

export interface NUSearchResult {
  title: string;
  nu_url: string;
  image?: string | null;
  description: string;
  genres: string[];
}

// Unified metadata search across the book catalogues
export interface MetaResult {
  /** Which catalogue answered: openlibrary, googlebooks, internetarchive, dnb, bnf, crossref. */
  source: string;
  ref: string;
  title: string;
  description: string;
  image?: string | null;
  genres: string[];
  year?: string | null;
}

export interface MetadataSearch {
  results: MetaResult[];
  notes: string[];
}

export interface Progress {
  status: string;
  step: string;
  current: number;
  total: number;
  message: string;
  error: string;
}

// --- Master agent (ingest curation + series chat) ---------------------------

export type AgentStatus = "running" | "waiting_user" | "done" | "error" | "cancelled";

export interface AgentToolCall {
  id: string;
  type: string;
  function: { name: string; arguments: string };
}

export interface AgentMessage {
  seq: number;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_name?: string | null;
  tool_calls?: AgentToolCall[] | null;
}

export interface PendingQuestion {
  question: string;
  options: string[];
}

export interface AgentSession {
  id: string;
  kind: "ingest" | "chat";
  status: AgentStatus;
  title: string;
  series_id?: string | null;
  pending_question?: PendingQuestion | null;
  error: string;
}

export interface AgentSessionDetail extends AgentSession {
  messages: AgentMessage[];
}
