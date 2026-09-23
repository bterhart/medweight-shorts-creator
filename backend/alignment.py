"""Slide-to-transcript alignment with Claude.

One vision request per job (or per MAX_SLIDES_PER_REQUEST slides for a very
large deck): the whole transcript as numbered cues, plus every slide as an
image with its extracted text. Replaces the Manus-based alignment with the
same output shape (job["alignment"]) but a fundamentally better input: the
model sees the entire deck and the entire talk at once - no transcript
windows, no chunk boundaries to strand a slide's narration across - and it
returns cue ranges rather than re-emitting transcript text, so the excerpt
is sliced exactly from the SRT and the reply is small and fast.

Official anthropic SDK (>=1, which needs Python 3.10+): streaming so the
image payload can't trip the HTTP timeout, structured output so the JSON is
schema-valid by construction, adaptive thinking with a tunable effort, and
server-side refusal fallbacks on by default (config.ALIGNMENT_FALLBACKS)."""
from __future__ import annotations

import base64
import json
import os

import anthropic
import pymupdf

import config
from pipeline import parse_srt_cues

# Slides are re-rendered from the PDF at this DPI just for the request - a
# 16:9 slide comes out ~1280x720, which reads slide text fine at roughly a
# quarter of the image tokens the 150-DPI review images would cost.
ALIGN_DPI = 96
# A deck bigger than this is split across requests that each still get the
# full transcript, so no request is ever aligning against a window.
MAX_SLIDES_PER_REQUEST = 60
SLIDE_TEXT_HINT_CHARS = 1500

ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "alignment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slide_id": {"type": "string"},
                    "start_cue": {"type": "integer"},
                    "end_cue": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": ["slide_id", "start_cue", "end_cue", "confidence", "notes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["alignment"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You align a presentation's slides to the transcript of the talk that was given over them. You are "
    "given the full transcript as numbered cues, and every slide as an image preceded by its slide_id, "
    "source deck, role, and extracted text. Decide which slides were on screen for which stretches of "
    "the talk."
)


def _fmt_time(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def _instructions(cues: list, chunk_note: str) -> str:
    listing = "\n".join(f"[{i}] {_fmt_time(c['start'])} {c['text']}" for i, c in enumerate(cues))
    return (
        f"{chunk_note}\n\n"
        "Rules:\n"
        "- Work in narrative order. For each slide that has corresponding narrated content, give the "
        "inclusive cue range [start_cue, end_cue] the speaker covered while it was on screen. Ranges must "
        "not overlap, and together should account for the narrated content of the talk.\n"
        "- Build the main sequence from the primary deck; use a supplementary-deck slide only where it "
        "covers content the primary deck does not show.\n"
        "- Skip slides with no corresponding narrated content (title, agenda, section dividers, blank).\n"
        "- A slide may appear more than once if the speaker returns to it.\n"
        "- Use slide_id values exactly as given; never invent one.\n"
        "- confidence is 0 to 1. notes is one short phrase saying why the passage matches the slide.\n\n"
        f"Transcript ({len(cues)} cues):\n{listing}"
    )


def _slide_blocks(slide: dict, doc, role: str) -> list:
    page = doc[slide["pageNumber"] - 1]
    png = page.get_pixmap(dpi=ALIGN_DPI).tobytes("png")
    header = f"slide_id: {slide['slideId']} - deck {slide['pdfId']} ({role}), page {slide['pageNumber']}."
    text_path = slide.get("textPath")
    if text_path and os.path.isfile(text_path):
        hint = open(text_path, encoding="utf-8", errors="replace").read().strip()[:SLIDE_TEXT_HINT_CHARS]
        if hint:
            header += f"\nExtracted text:\n{hint}"
    return [
        {"type": "text", "text": header},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode("utf-8"),
            },
        },
    ]


def _request(client: anthropic.Anthropic, content: list) -> list:
    kwargs = dict(
        model=config.ALIGNMENT_MODEL,
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={
            "effort": config.ALIGNMENT_EFFORT,
            "format": {"type": "json_schema", "schema": ALIGNMENT_SCHEMA},
        },
    )
    # Server-side refusal fallbacks: if a safety classifier declines the
    # request, the API re-runs it on Anthropic's recommended substitute
    # inside the same call instead of handing back a refusal. A benign deck
    # tripping a classifier would otherwise fail the whole job.
    if config.ALIGNMENT_FALLBACKS:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
        stream_cm = client.beta.messages.stream(**kwargs)
    else:
        stream_cm = client.messages.stream(**kwargs)

    with stream_cm as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise RuntimeError(
            "alignment: Claude declined the request "
            f"(category={getattr(details, 'category', None)!r}: {getattr(details, 'explanation', None)})"
        )
    if message.stop_reason == "max_tokens":
        raise RuntimeError("alignment: Claude's reply was cut off at max_tokens")

    usage = message.usage
    print(f"  alignment request: model={message.model} input_tokens={usage.input_tokens} "
          f"output_tokens={usage.output_tokens}")

    text_block = next((b for b in message.content if b.type == "text"), None)
    if text_block is None:
        raise RuntimeError(f"alignment: reply had no text block (stop_reason={message.stop_reason})")
    return json.loads(text_block.text)["alignment"]


def align_job(job: dict) -> None:
    """Populates job["alignment"] in transcript order. Every entry carries
    the cue range it came from (startCue/endCue, plus seconds) alongside the
    sliced transcriptExcerpt the rest of the pipeline reads."""
    cues = parse_srt_cues(job["sources"]["srt"]["path"])
    if not cues:
        raise RuntimeError("alignment: the transcript has no cues")

    slides = job["slides"]
    roles = {p["id"]: p["role"] for p in job["sources"]["pdfs"]}
    known_ids = {s["slideId"] for s in slides}
    docs = {p["id"]: pymupdf.open(p["path"]) for p in job["sources"]["pdfs"]}
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, base_url=config.ANTHROPIC_BASE_URL)

    raw = []
    try:
        chunks = [slides[i:i + MAX_SLIDES_PER_REQUEST] for i in range(0, len(slides), MAX_SLIDES_PER_REQUEST)]
        for ci, chunk in enumerate(chunks):
            if len(chunks) == 1:
                note = "Every slide in the deck is included below."
            else:
                first = ci * MAX_SLIDES_PER_REQUEST + 1
                note = (
                    f"These are slides {first}-{first + len(chunk) - 1} of {len(slides)} in deck order. "
                    "Align only these; the rest of the deck is handled by separate calls over this same "
                    "transcript, so leave the parts of the talk they cover unassigned."
                )
            content = [{"type": "text", "text": _instructions(cues, note)}]
            for slide in chunk:
                content.extend(_slide_blocks(slide, docs[slide["pdfId"]], roles.get(slide["pdfId"], "primary")))
            content.append({"type": "text", "text": "Return the alignment as JSON."})
            raw.extend(_request(client, content))
    finally:
        for doc in docs.values():
            doc.close()

    # Transcript order is narrative order by definition, and it also
    # interleaves the results of split requests correctly.
    raw.sort(key=lambda a: (a["start_cue"], a["end_cue"]))

    alignment = []
    last = len(cues) - 1
    for a in raw:
        slide_id = a["slide_id"]
        if slide_id not in known_ids:
            stripped = os.path.splitext(slide_id)[0]
            if stripped not in known_ids:
                raise RuntimeError(f"alignment: Claude returned slide_id {slide_id!r}, which is not in the deck")
            slide_id = stripped
        start = max(0, min(int(a["start_cue"]), last))
        end = max(0, min(int(a["end_cue"]), last))
        if end < start:
            start, end = end, start
        alignment.append({
            "sequenceIndex": len(alignment),
            "slideId": slide_id,
            "startCue": start,
            "endCue": end,
            "startSeconds": cues[start]["start"],
            "endSeconds": cues[end]["end"],
            "transcriptExcerpt": " ".join(c["text"] for c in cues[start:end + 1]),
            "confidence": a.get("confidence"),
            "notes": a.get("notes", ""),
        })

    if not alignment:
        raise RuntimeError("alignment: Claude returned no aligned slides")
    job["alignment"] = alignment
