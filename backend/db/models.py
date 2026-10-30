from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Enums (stored as plain strings) ---------------------------------------

class SeriesSource(str, Enum):
    NOVELUPDATES = "novelupdates"
    MANUAL = "manual"


class VolumeStatus(str, Enum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"


class EngineMode(str, Enum):
    VISION = "vision"        # multimodal model does OCR + translation in one pass
    OCR_TEXT = "ocr_text"    # local local OCR extracts text, endpoint translates it


class VolumeKind(str, Enum):
    SCANS = "scans"  # page images -> OCR/vision -> overlay reader / PDF export
    TEXT = "text"    # digital source (epub/txt/webnovel) -> text reader / EPUB export


class PageType(str, Enum):
    TEXT = "text"
    ILLUSTRATION = "illustration"
    MIXED = "mixed"
    OTHER = "other"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class AgentKind(str, Enum):
    INGEST = "ingest"  # curates an "add files" upload into series/volumes
    CHAT = "chat"      # per-series Q&A over translations/summaries/glossary


class AgentStatus(str, Enum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"  # suspended on an ask_user question
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


# --- Tables -----------------------------------------------------------------

class Series(Base):
    __tablename__ = "series"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default=SeriesSource.MANUAL.value)
    nu_url: Mapped[str | None] = mapped_column(String, nullable=True)
    poster_key: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    genres: Mapped[list] = mapped_column(JSON, default=list)
    associated_names: Mapped[list] = mapped_column(JSON, default=list)
    nu_status: Mapped[str | None] = mapped_column(String, nullable=True)
    year: Mapped[str | None] = mapped_column(String, nullable=True)
    source_language: Mapped[str] = mapped_column(String, default="auto", server_default="auto")
    # Filled in from the first page whose text we can read, when source_language
    # is "auto". Kept separate so the user's explicit choice is never overwritten.
    detected_language: Mapped[str] = mapped_column(String, default="", server_default="")
    target_language: Mapped[str] = mapped_column(String, default="English", server_default="English")
    created_at: Mapped[datetime] = mapped_column(default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    volumes: Mapped[list[Volume]] = relationship(
        back_populates="series", cascade="all, delete-orphan", order_by="Volume.order"
    )
    glossary: Mapped[list[GlossaryTerm]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )


class Volume(Base):
    __tablename__ = "volumes"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String, default="Volume 1")
    order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default=VolumeStatus.UPLOADED.value)
    engine_mode: Mapped[str] = mapped_column(String, default=EngineMode.VISION.value)
    kind: Mapped[str] = mapped_column(String, default=VolumeKind.SCANS.value,
                                      server_default="scans")
    # original digital source (epub) in the object store — enables epub repack
    source_key: Mapped[str] = mapped_column(String, default="", server_default="")
    batch_size: Mapped[int] = mapped_column(Integer, default=5)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=_now, server_default=func.now())

    series: Mapped[Series] = relationship(back_populates="volumes")
    pages: Mapped[list[Page]] = relationship(
        back_populates="volume", cascade="all, delete-orphan", order_by="Page.index"
    )
    job: Mapped[Job | None] = relationship(
        back_populates="volume", cascade="all, delete-orphan", uselist=False
    )


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    volume_id: Mapped[str] = mapped_column(ForeignKey("volumes.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer, default=0)
    image_key: Mapped[str] = mapped_column(String, nullable=False)  # "" for text chapters
    chapter_title: Mapped[str] = mapped_column(Text, default="", server_default="")
    page_type: Mapped[str] = mapped_column(String, default=PageType.OTHER.value)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)

    volume: Mapped[Volume] = relationship(back_populates="pages")
    regions: Mapped[list[Region]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="Region.order"
    )


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    order: Mapped[int] = mapped_column(Integer, default=0)
    # bbox stored as percentages of page dimensions (resolution-independent overlays)
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[float] = mapped_column(Float, default=0.0)
    height: Mapped[float] = mapped_column(Float, default=0.0)
    japanese_text: Mapped[str] = mapped_column(Text, default="")
    english_text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    page: Mapped[Page] = relationship(back_populates="regions")


class Job(Base):
    """Single source of truth for processing progress — survives page refresh
    and process restart, unlike the old in-memory dict."""
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    volume_id: Mapped[str] = mapped_column(
        ForeignKey("volumes.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String, default=JobStatus.QUEUED.value)
    step: Mapped[str] = mapped_column(String, default="")
    current: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    # What processing this volume has cost so far, across runs and resumes.
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    wall_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    volume: Mapped[Volume] = relationship(back_populates="job")


class AppSetting(Base):
    """Key-value store for app-level settings configurable at runtime (e.g. the
    LLM provider config), so the UI can change them without editing .env."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AgentSession(Base):
    """One agent conversation/run: an 'add files' ingest curation or a series
    chat. Like Job, this is the durable source of truth — the UI streams it and
    a restart never loses the transcript. Raw OpenAI-shaped messages live in
    AgentMessage so a suspended run resumes losslessly."""
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    series_id: Mapped[str | None] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String, default=AgentKind.CHAT.value)
    status: Mapped[str] = mapped_column(String, default=AgentStatus.RUNNING.value)
    title: Mapped[str] = mapped_column(String, default="")
    upload_id: Mapped[str | None] = mapped_column(String, nullable=True)  # ingest staging dir
    # JSON {"question": str, "options": [str]} while status == waiting_user.
    pending_question: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="session_rel", cascade="all, delete-orphan", order_by="AgentMessage.seq"
    )


class AgentMessage(Base):
    """One turn in an agent session, stored in the OpenAI wire shape (role +
    content + tool_calls/tool_call_id) so history replays into the next LLM
    call without translation. Also doubles as the UI activity feed."""
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String, default="assistant")  # user|assistant|tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)  # assistant requests
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)  # tool results
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now, server_default=func.now())

    session_rel: Mapped[AgentSession] = relationship(back_populates="messages")


class GlossaryTerm(Base):
    """Series-scoped translation glossary (replaces glossary.json). Keeps
    character/term renderings consistent across volumes of the same series."""
    __tablename__ = "glossary_terms"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String, default="term")  # character | term
    jp: Mapped[str] = mapped_column(String, nullable=False)
    en: Mapped[str] = mapped_column(String, default="")
    # Where the term was first seen (volume order, page index). A prompt only
    # gets terms seen by its own position; -1 means not dated yet.
    first_volume: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    first_page: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")

    series: Mapped[Series] = relationship(back_populates="glossary")

# --- Graph memory -------------------------------------------------------------
# The glossary is a flat name->name table: it keeps a term consistent but knows
# nothing about who the term is, and nothing about when the reader is allowed to
# know it. These three tables are the graph the glossary is a view over.


class MemoryEntity(Base):
    """One thing in a series' world: a person, a place, an organisation, a term."""
    __tablename__ = "memory_entities"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String, default="term")  # person|place|org|term|thing
    canonical: Mapped[str] = mapped_column(String, nullable=False)
    # The volume that first introduces this entity. Nothing established later may
    # be shown to a reader who has not reached it.
    first_volume: Mapped[int] = mapped_column(Integer, default=1)
    first_page: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    # How the translation renders the name, once it has settled on one.
    rendering: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)

    aliases: Mapped[list["MemoryAlias"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", lazy="selectin")


class MemoryAlias(Base):
    """A surface form that resolves to an entity: リゼット, Lizette, リズ."""
    __tablename__ = "memory_aliases"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("memory_entities.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    # Normalised form (NFKC, casefolded, honorifics stripped) - the matching key.
    norm: Mapped[str] = mapped_column(String, index=True, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=1)
    # Page index within `volume` where this form was first seen; -1 = not dated yet.
    page: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    script: Mapped[str] = mapped_column(String, default="")  # ja|ko|zh|latn|cyrl|arab|other

    entity: Mapped[MemoryEntity] = relationship(back_populates="aliases")


class MemoryEdge(Base):
    """A typed relation between two entities, tagged with the volume that proves it."""
    __tablename__ = "memory_edges"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), index=True)
    src_id: Mapped[str] = mapped_column(ForeignKey("memory_entities.id", ondelete="CASCADE"))
    dst_id: Mapped[str] = mapped_column(ForeignKey("memory_entities.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String, nullable=False)  # serves|born_in|member_of|...
    established_volume: Mapped[int] = mapped_column(Integer, default=1)
    # Page index within that volume; -1 = not dated yet.
    established_page: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    page_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

