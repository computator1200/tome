"""Translation against a single OpenAI-compatible endpoint.

Replaces the old gemini / gemini_ocr / ollama trio. Two entry points:
  - translate_text(): text-in / text-out, for the local OCR (ocr_text) engine.
  - vision_ocr_translate(): image-in, for the one-pass vision engine.

Both inject series context (from NovelUpdates) and the running glossary into the
prompt so character names and terminology stay consistent.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from contextvars import ContextVar

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from logging_config import get_logger
from services import language, provider

log = get_logger("llm")

_client: OpenAI | None = None
_client_key: tuple | None = None
_supports_json_format = True  # auto-disabled if the endpoint rejects response_format


class LLMError(Exception):
    """A translation/vision request failed in a way worth showing the user.

    Carries a human-readable, honest message (model name + what went wrong) so
    the pipeline can store it on the Job and the frontend can display it.
    """


def _get_client() -> OpenAI:
    """Build (and cache) the client from the current provider config, rebuilding
    when the endpoint/key/timeout changes (e.g. updated from the Settings UI)."""
    global _client, _client_key
    cfg = provider.get()
    key = (cfg["base_url"], cfg["api_key"], cfg["timeout"])
    if _client is None or key != _client_key:
        # api_key may be empty for keyless local servers; the SDK requires a
        # non-empty string, so pass a placeholder in that case.
        _client = OpenAI(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"] or "sk-no-key-required",
            timeout=cfg["timeout"],
        )
        _client_key = key
    return _client


# Usage of the model calls made under track_usage(). The calls run in worker
# threads (asyncio.to_thread copies the context), so the shared counter is
# guarded by a lock.
_usage: ContextVar[dict | None] = ContextVar("llm_usage", default=None)
_usage_lock = threading.Lock()


def track_usage() -> dict:
    """Start counting the model calls made from this context. Returns the counter
    ({calls, prompt_tokens, completion_tokens, seconds}), which fills in place."""
    acc = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}
    _usage.set(acc)
    return acc


def _record(resp, seconds: float) -> None:
    acc = _usage.get()
    if acc is None:
        return
    u = getattr(resp, "usage", None)
    with _usage_lock:
        acc["calls"] += 1
        acc["seconds"] += seconds
        acc["prompt_tokens"] += int(getattr(u, "prompt_tokens", 0) or 0)
        acc["completion_tokens"] += int(getattr(u, "completion_tokens", 0) or 0)


# Cap the glossary injected into each prompt so it can't grow unbounded across
# a long series and bloat tokens/cost.
_MAX_GLOSSARY = 200


def _context_block(series_ctx: dict, glossary: list[dict]) -> str:
    """Build the shared prompt preamble describing the series + glossary."""
    lines: list[str] = []
    if series_ctx:
        if series_ctx.get("title"):
            lines.append(f"Series: {series_ctx['title']}")
        if series_ctx.get("author"):
            lines.append(f"Author: {series_ctx['author']}")
        if series_ctx.get("genres"):
            lines.append(f"Genres: {', '.join(series_ctx['genres'])}")
        if series_ctx.get("associated_names"):
            lines.append(f"Also known as: {', '.join(series_ctx['associated_names'][:5])}")
        if series_ctx.get("description"):
            desc = series_ctx["description"][:1200]
            lines.append(f"Synopsis (for context, do not translate this): {desc}")
    block = ""
    if lines:
        block += "Series context:\n" + "\n".join(lines) + "\n\n"
    if series_ctx.get("previous_summary"):
        block += ("Story so far (summaries of earlier volumes, for context only):\n"
                  + series_ctx["previous_summary"][:4000] + "\n\n")
    if series_ctx.get("memory"):
        block += str(series_ctx["memory"])
    if glossary:
        block += "Use this glossary for consistency (source -> target):\n"
        for term in glossary[:_MAX_GLOSSARY]:
            block += f"  {term['jp']} -> {term['en']}\n"
        block += "\n"
    return block


_GUIDELINES = (
    "Guidelines:\n"
    "- Maintain each character's distinct voice and personality\n"
    "- Preserve honorifics and culturally specific terms where appropriate\n"
    "- Translate naturally and fluently, not overly literally\n"
    "- Keep the narrative tone of the original\n"
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


def _server_detail(e: Exception) -> str:
    """Pull the server's own error text out of an OpenAI SDK exception."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    return getattr(e, "message", None) or str(e)


def _describe(model: str, e: Exception) -> str:
    """Turn an SDK exception into an honest, user-facing message."""
    if isinstance(e, AuthenticationError):
        return f"Model '{model}': the endpoint rejected the API key (401). Check OPENAI_API_KEY."
    if isinstance(e, PermissionDeniedError):
        return f"Model '{model}': access denied (403). {_server_detail(e)}"
    if isinstance(e, NotFoundError):
        return (f"Model '{model}' was not found (404). Make sure it is pulled and "
                f"available on the endpoint at {provider.get()['base_url']}.")
    if isinstance(e, BadRequestError):
        return f"Model '{model}' rejected the request: {_server_detail(e)}"
    if isinstance(e, RateLimitError):
        return f"Model '{model}': rate limited (429). {_server_detail(e)}"
    if isinstance(e, APITimeoutError):
        return f"Model '{model}': request timed out after {provider.get()['timeout']:.0f}s."
    if isinstance(e, APIConnectionError):
        return f"Could not reach the LLM endpoint at {provider.get()['base_url']}. Is it running?"
    return f"Model '{model}': {_server_detail(e)}"


def _create(client: OpenAI, model: str, messages: list):
    """One chat call. Requests JSON output, transparently dropping the
    response_format option if the endpoint says it is unsupported."""
    global _supports_json_format
    kwargs = dict(model=model, messages=messages, temperature=0.3)
    if _supports_json_format:
        try:
            return client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except BadRequestError as e:
            detail = _server_detail(e).lower()
            # Only treat this as "format unsupported" if that is what it is about;
            # otherwise it is a real bad request (e.g. no image support) and must
            # propagate rather than be retried.
            if "response_format" in detail or "format" in detail or "json" in detail:
                log.info("endpoint rejected response_format; retrying without it")
                _supports_json_format = False
            else:
                raise
    return client.chat.completions.create(**kwargs)


# Errors that will never succeed on retry — fail fast with a clear message.
_PERMANENT = (BadRequestError, NotFoundError, AuthenticationError, PermissionDeniedError)


def _chat(model: str, content, max_retries: int = 3) -> dict:
    """Call chat completions and parse a JSON object. Raises LLMError with an
    honest message on permanent errors (no retry) or after exhausting retries
    on transient ones, so the failure surfaces instead of being swallowed."""
    client = _get_client()
    messages = [{"role": "user", "content": content}]
    last_error: LLMError | None = None
    for attempt in range(max_retries):
        try:
            t0 = time.time()
            resp = _create(client, model, messages)
            _record(resp, time.time() - t0)
            raw = (resp.choices[0].message.content or "").strip()
            log.info(f"llm {model} returned {len(raw)} chars in {time.time()-t0:.1f}s")
            return json.loads(_strip_fences(raw))
        except _PERMANENT as e:
            raise LLMError(_describe(model, e)) from e
        except json.JSONDecodeError:
            last_error = LLMError(f"Model '{model}' did not return valid JSON.")
            log.warning(f"llm attempt {attempt+1}: invalid JSON from {model}")
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            last_error = LLMError(_describe(model, e))
            log.warning(f"llm attempt {attempt+1}: {last_error}")
        except Exception as e:  # noqa: BLE001
            last_error = LLMError(_describe(model, e))
            log.warning(f"llm attempt {attempt+1}: {last_error}")
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    raise last_error or LLMError(f"Model '{model}': request failed.")


def chat_with_tools(messages: list[dict], tools: list[dict],
                    model: str | None = None, max_retries: int = 3) -> dict:
    """One tool-enabled chat call for the agent runner (services/agent.py).

    Returns the assistant message as a plain dict: {"role", "content",
    "tool_calls": [{"id","type","function":{"name","arguments"}}]?}. Same error
    policy as _chat: permanent errors fail fast with an honest LLMError (e.g.
    the endpoint/model not supporting `tools`), transient ones retry.
    """
    client = _get_client()
    mdl = model or provider.get()["model"]
    last_error: LLMError | None = None
    for attempt in range(max_retries):
        try:
            t0 = time.time()
            resp = client.chat.completions.create(
                model=mdl, messages=messages, tools=tools, temperature=0.2,
            )
            msg = resp.choices[0].message
            out: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                out["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in msg.tool_calls
                ]
            log.info(f"agent llm {mdl}: {len(out.get('tool_calls') or [])} tool call(s), "
                     f"{len(out['content'])} chars in {time.time()-t0:.1f}s")
            return out
        except _PERMANENT as e:
            raise LLMError(_describe(mdl, e)) from e
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            last_error = LLMError(_describe(mdl, e))
            log.warning(f"agent llm attempt {attempt+1}: {last_error}")
        except Exception as e:  # noqa: BLE001
            last_error = LLMError(_describe(mdl, e))
            log.warning(f"agent llm attempt {attempt+1}: {last_error}")
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    raise last_error or LLMError(f"Model '{mdl}': request failed.")


def describe_page(image_bytes: bytes, model: str | None = None) -> dict:
    """Cheap content sanity-check for the ingest agent: what IS this image?
    Returns {"content_type", "language", "description"}."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = """Look at this scanned page and classify it.
Return ONLY a JSON object (no markdown, no prose):
{"content_type": "<novel_text|manga|illustration|cover|table_of_contents|document|photo|other>",
 "language": "<main language of any text, or 'none'>",
 "description": "<one sentence: what this page is>"}"""
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    return _chat(model or provider.get()["vision_model"], content)


# The memory instructions shared by both translate prompts. The verb list must
# match services/memory.RELATIONS: a verb the model is not offered is never
# used, and one outside it is dropped.
_MEMORY_RULES = (
    "An alias is any other way this text refers to the same entity: a short name,\n"
    "a nickname, or a title or description used for it (for example the title a\n"
    "character is known by). List one only when the text makes clear it is the same\n"
    "entity. Use only these relations: serves, works_for, member_of, sibling_of,\n"
    "parent_of, child_of, married_to, lives_in, born_in, located_in, owns, knows,\n"
    "leads, attends, teaches, friend_of, rival_of, kills, betrays.")


def translate_text(series_ctx: dict, glossary: list[dict], pages_payload: list[dict],
                   model: str | None = None) -> dict:
    """Translate a batch of pages of already-OCR'd source text.

    pages_payload: [{"page_index": int, "regions": [{"id", "japanese_text"}]}]
    Returns: {"results": [{"page_index", "translations": {id: english}}],
              "new_terms": {"characters": {...}, "terms": {...}}}
    """
    payload = [
        {
            "page_index": p["page_index"],
            "regions": [{"id": r["id"], "text": r["japanese_text"]} for r in p["regions"]],
        }
        for p in pages_payload
    ]
    src = language.describe_source(series_ctx.get("source_language"),
                                  series_ctx.get("detected_language"))
    tgt = series_ctx.get("target_language") or "English"
    prompt = f"""You are a professional book translator working from {src} into {tgt}.
Translate the following book text to natural, fluent {tgt}.

{_GUIDELINES}
{_context_block(series_ctx, glossary)}Input (JSON array of pages with text regions):
```json
{json.dumps(payload, ensure_ascii=False)}
```

Return ONLY a JSON object with this EXACT structure (no markdown, no prose):
{{
  "translations": [
    {{"page_index": <number>, "regions": [{{"id": "<region_id>", "english": "<translation>"}}]}}
  ],
  "new_terms": {{
    "characters": {{"<name in the source language>": "<rendered name>"}},
    "terms": {{"<term in the source language>": "<rendered term>"}}
  }},
  "memory": {{
    "entities": [{{"name": "<name, as written here>", "kind": "person|place|org|term",
                   "aliases": ["<other names, nicknames, titles or descriptions this text uses for it>"]}}],
    "relations": [{{"src": "<entity>", "relation": "<verb>", "dst": "<entity>"}}]
  }}
}}
Only include entities that appear in this text, and relations only where the
text states them. The memory block builds the series graph; do not invent.
{_MEMORY_RULES}"""

    data = _chat(model or provider.get()["model"], prompt)
    results = []
    try:
        translations_per_page = data["translations"]
        if not isinstance(translations_per_page, list):
            raise TypeError("translations is not a list")
        for page in translations_per_page:
            regions = page.get("regions") or []
            if not isinstance(regions, list):
                raise TypeError("regions is not a list")
            results.append({
                "page_index": int(page["page_index"]),
                "translations": {
                    str(r["id"]): str(r.get("english") or "") for r in regions
                },
            })
    except (KeyError, TypeError, ValueError) as e:
        # A malformed response is this call's problem, not the volume's: raise
        # the error type the pipeline already isolates per page.
        raise LLMError(f"model returned an unusable translation payload ({e})") from e
    new_terms = data.get("new_terms") or {}
    if not isinstance(new_terms, dict):
        new_terms = {}
    memory = data.get("memory") or {}
    if not isinstance(memory, dict):
        memory = {}
    return {"results": results, "new_terms": new_terms, "memory": memory}


def summarize(series_ctx: dict, text: str, model: str | None = None) -> str:
    """Summarize a volume's translated text for use as context in the next
    volume. Best-effort: returns "" on failure (summaries are non-critical)."""
    text = (text or "").strip()
    if not text:
        return ""
    tgt = series_ctx.get("target_language") or "English"
    title = series_ctx.get("title") or "this book"
    prompt = (
        f"Summarize this volume of {title} in 6-10 sentences of {tgt}. Cover the main plot "
        f"events and character developments so it can prime a translator for the next volume. "
        f"Write only the summary.\n\n{text[:20000]}"
    )
    try:
        client = _get_client()
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model or provider.get()["model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        _record(resp, time.time() - t0)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001 — summary is optional, never fail the volume
        log.warning(f"summary generation failed: {e}")
        return ""


_BOX_PROMPT = (
    '"box": [x, y, width, height] is where the block sits on the page, as\n'
    "percentages of the page's width and height (0 to 100), measured from the\n"
    "top-left corner. Cover the whole block.")


def _box(raw) -> dict:
    """A region's bounding box as page percentages, or all zeros when unusable.

    Models disagree on scale: some answer in percent as asked, some in 0-1 and
    some in 0-1000 (the convention several vision models were trained on). The
    scale is inferred from the largest value, the box is clamped to the page,
    and anything degenerate becomes "no box" rather than a wrong one.
    """
    zero = {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    if isinstance(raw, dict):
        raw = [raw.get("x"), raw.get("y"), raw.get("width", raw.get("w")),
               raw.get("height", raw.get("h"))]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return zero
    try:
        x, y, w, h = (float(v) for v in raw)
    except (TypeError, ValueError):
        return zero
    top = max(x, y, w, h)
    if top <= 1.0:
        scale = 100.0
    elif top > 100.5:
        scale = 0.1 if top <= 1000.5 else 0.0
    else:
        scale = 1.0
    if not scale:
        return zero
    x, y, w, h = x * scale, y * scale, w * scale, h * scale
    x, y = min(max(x, 0.0), 100.0), min(max(y, 0.0), 100.0)
    w, h = min(w, 100.0 - x), min(h, 100.0 - y)
    if w < 0.5 or h < 0.5:
        return zero
    return {"x": round(x, 2), "y": round(y, 2), "width": round(w, 2), "height": round(h, 2)}


def ocr_page_vision(series_ctx: dict, image_bytes: bytes, model: str | None = None) -> list[dict]:
    """OCR a page with the vision model (no translation). Returns region dicts
    [{id, order, japanese_text, confidence}] in reading order. This replaces the
    OpenCV column-detection + local OCR path with a layout-aware model read."""
    src = language.describe_source(series_ctx.get("source_language"),
                                  series_ctx.get("detected_language"))
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = f"""You are an OCR engine for scanned book pages.
Transcribe ALL text on the page in natural reading order, in the language it is
written in (for vertical text, read top-to-bottom, right-to-left). Do NOT
translate. One entry per block or paragraph.
{_BOX_PROMPT}
Return ONLY a JSON object (no markdown, no prose):
{{"regions": [{{"source_text": "<transcribed text>", "box": [x, y, width, height]}}]}}"""
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    data = _chat(model or provider.get()["vision_model"], content)
    regions = []
    for i, r in enumerate(data.get("regions", [])):
        text = r.get("source_text", "").strip() if isinstance(r, dict) else str(r).strip()
        if text:
            box = _box(r.get("box")) if isinstance(r, dict) else _box(None)
            regions.append({"id": f"col_{i}", "order": i, "japanese_text": text,
                            "confidence": 85.0, **box})
    return regions


def vision_ocr_translate(series_ctx: dict, glossary: list[dict], image_bytes: bytes,
                         model: str | None = None) -> dict:
    """One-pass OCR + translation of a page image via a multimodal model.

    Returns: {"regions": [{"id","order","x","y","width","height","japanese_text",
              "english_text","confidence"}], "new_terms": {...}, "memory": {...}}
    Boxes are page percentages; a region the model gave no usable box for has
    all four at 0, and the reader lays it out in reading order instead.
    """
    src = language.describe_source(series_ctx.get("source_language"),
                                  series_ctx.get("detected_language"))
    tgt = series_ctx.get("target_language") or "English"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = f"""You are a professional book translator working from {src} into {tgt}.
This is a scanned page.

Read the text in natural reading order (for vertical {src} text that is
top-to-bottom, right-to-left) and translate each block to fluent {tgt}.

{_GUIDELINES}
{_context_block(series_ctx, glossary)}Return ONLY a JSON object with this EXACT structure (no markdown, no prose):
{{
  "regions": [
    {{"source_text": "<original>", "english_text": "<translation>", "box": [x, y, width, height]}}
  ],
  "new_terms": {{
    "characters": {{"<name>": "<translated_name>"}},
    "terms": {{"<term>": "<translated_term>"}}
  }},
  "memory": {{
    "entities": [{{"name": "<name, as written here>", "kind": "person|place|org|term",
                   "aliases": ["<other names, nicknames, titles or descriptions this text uses for it>"]}}],
    "relations": [{{"src": "<entity>", "relation": "<verb>", "dst": "<entity>"}}]
  }}
}}
List regions in reading order. {_BOX_PROMPT}
Only include entities that appear on this page,
and relations only where the page states them.
{_MEMORY_RULES}"""

    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    data = _chat(model or provider.get()["vision_model"], content)

    raw_regions = data.get("regions") or []
    if not isinstance(raw_regions, list):
        raise LLMError("model returned a non-list 'regions' payload")
    regions = []
    for i, r in enumerate(raw_regions):
        if not isinstance(r, dict):
            continue
        regions.append({
            "id": f"col_{i}",
            "order": i,
            "japanese_text": str(r.get("source_text") or r.get("japanese_text") or ""),
            "english_text": str(r.get("english_text") or ""),
            "confidence": 90.0,
            **_box(r.get("box")),
        })
    new_terms = data.get("new_terms") or {}
    if not isinstance(new_terms, dict):
        new_terms = {}
    memory = data.get("memory") or {}
    if not isinstance(memory, dict):
        memory = {}
    return {"regions": regions, "new_terms": new_terms, "memory": memory}
