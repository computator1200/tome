"""Pydantic schemas for the HTTP API (request bodies + response shapes).

These are deliberately separate from the SQLAlchemy ORM models in db/models.py:
the ORM is the persistence layer, these describe the wire format.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from db.models import EngineMode


# --- NovelUpdates search ----------------------------------------------------

class NUSearchResult(BaseModel):
    title: str
    nu_url: str
    image: str | None = None
    description: str = ""
    genres: list[str] = []


# --- Unified metadata search (NovelUpdates + AniList fallback) ---------------

class MetaResultOut(BaseModel):
    source: str  # novelupdates | anilist
    ref: str     # nu_url or AniList media id
    title: str
    description: str = ""
    image: str | None = None
    genres: list[str] = []
    year: str | None = None


class MetadataSearchOut(BaseModel):
    results: list[MetaResultOut] = []
    notes: list[str] = []  # e.g. "NovelUpdates search failed (…); using AniList"


class MetadataLinkIn(BaseModel):
    source: str
    ref: str


# --- Series -----------------------------------------------------------------

class ManualSeriesIn(BaseModel):
    title: str
    description: str = ""
    source_language: str = "auto"
    target_language: str = "English"


class CreateSeriesIn(BaseModel):
    # Provide exactly one of these:
    nu_url: str | None = None
    manual: ManualSeriesIn | None = None


class SeriesPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None
    source_language: str | None = None
    target_language: str | None = None


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order: int
    x: float
    y: float
    width: float
    height: float
    japanese_text: str
    english_text: str
    confidence: float


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    index: int
    page_type: str
    chapter_title: str = ""
    width: int
    height: int
    image_url: str = ""
    regions: list[RegionOut] = []


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    step: str
    current: int
    total: int
    message: str
    error: str


class VolumeSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    label: str
    order: int
    status: str
    engine_mode: str
    kind: str = "scans"
    batch_size: int
    page_count: int
    summary: str = ""
    job: JobOut | None = None


class VolumeDetail(VolumeSummary):
    pages: list[PageOut] = []


class SeriesSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    source: str
    nu_url: str | None = None
    description: str = ""
    author: str | None = None
    genres: list = []
    nu_status: str | None = None
    year: str | None = None
    source_language: str = "auto"
    # Set from the pages themselves when source_language is "auto".
    detected_language: str = ""
    target_language: str = "English"
    poster_url: str | None = None
    # aggregate, computed in the router:
    volume_count: int = 0
    page_count: int = 0
    status: str = "uploaded"


class SeriesDetail(SeriesSummary):
    associated_names: list = []
    volumes: list[VolumeSummary] = []


# --- Volume creation / processing ------------------------------------------

class CreateVolumeOut(BaseModel):
    volume_id: str
    series_id: str


class ProcessIn(BaseModel):
    engine_mode: EngineMode = EngineMode.VISION
    batch_size: int = 5
    # Whole-series runs skip finished volumes by default: pressing "Process all"
    # twice should not re-bill 20 volumes of finished work.
    force: bool = False


# --- Agent sessions (master agent: ingest curation + series chat) -----------

class PendingQuestion(BaseModel):
    question: str
    options: list[str] = []


class AgentMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    seq: int
    role: str
    content: str
    tool_name: str | None = None
    tool_calls: list | None = None


class AgentSessionOut(BaseModel):
    id: str
    kind: str
    status: str
    title: str
    series_id: str | None = None
    pending_question: PendingQuestion | None = None
    error: str = ""


class AgentSessionDetail(AgentSessionOut):
    messages: list[AgentMessageOut] = []


class AgentMessageIn(BaseModel):
    content: str


class IngestOut(BaseModel):
    session_id: str
