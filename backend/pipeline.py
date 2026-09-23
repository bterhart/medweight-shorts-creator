"""Phase 1 pipeline steps - PDF/SRT extraction, Manus alignment, narration
condensation, voice/TTS. Each function takes and mutates a job dict; the
caller (worker.py) saves to the DB between steps so status polls see
progress as it happens, same as the checkpoint pattern in the earlier n8n
version."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import pymupdf
import requests

import config
import db
import manus_client

WORDS_PER_MINUTE = 130
# Manus caps message.content's combined text at ~5000 estimated tokens with
# no way around it by splitting - a real constraint hit during development,
# not a guess. ~4 chars/token is a conservative estimate that leaves margin.
MAX_TRANSCRIPT_CHARS = 16000
# The prompt-library entry build_short() falls back to when a short doesn't
# name one (shorts created before the Create-short form had a prompt picker).
# Referenced by name, not pasted as a literal, so editing it in the library
# takes effect immediately. Never job["params"]["narrationStyle"] - that's
# clean_narration's style-neutral field, not this stage's.
SHORT_NARRATION_PROMPT_NAME = "Second pass (CBT/MI/ACT/DBT narration)"
UPLOAD_WORKERS = 8
# clean_narration is output-bound: the cleaned text is nearly as long as the
# transcript, so one call re-emits the whole talk. Splitting the segments
# across concurrent calls divides that wall time and keeps each call well
# under max_tokens - a single call could silently truncate a long transcript.
CLEAN_SEGMENTS_PER_CALL = 10
CLEAN_CONCURRENCY = 4


def parse_srt(srt_path: str) -> dict:
    text = open(srt_path, "r", encoding="utf-8", errors="replace").read()
    blocks = [b for b in text.replace("\r", "").split("\n\n") if b.strip()]
    cues = []
    for block in blocks:
        lines = block.split("\n")
        time_line = next((l for l in lines if "-->" in l), None)
        if not time_line:
            continue
        idx = lines.index(time_line)
        cues.append({"time": time_line.strip(), "text": " ".join(lines[idx + 1:]).strip()})

    def to_seconds(t):
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    duration = to_seconds(cues[-1]["time"].split(" --> ")[1]) if cues else 0
    full_text = " ".join(c["text"] for c in cues)
    return {"cueCount": len(cues), "durationSeconds": duration, "fullText": full_text}


def extract_pdf_slides(pdf_path: str, pdf_id: str, slides_dir: str) -> list:
    """Renders each page to PNG and extracts its text, using PyMuPDF - no
    poppler-utils/system dependency, deliberately, since root/system-package
    access on the target host (cPanel/CloudLinux) isn't guaranteed."""
    os.makedirs(slides_dir, exist_ok=True)
    slides = []
    doc = pymupdf.open(pdf_path)
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            pnum = f"{page_num + 1:03d}"
            slide_id = f"{pdf_id}-p{pnum}"
            image_path = os.path.join(slides_dir, f"{slide_id}.png")
            text_path = os.path.join(slides_dir, f"{slide_id}.txt")

            pix = page.get_pixmap(dpi=150)
            pix.save(image_path)
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(page.get_text())

            slides.append({
                "slideId": slide_id, "pdfId": pdf_id, "pageNumber": page_num + 1,
                "imagePath": image_path, "textPath": text_path, "manusFileId": None,
            })
    finally:
        doc.close()
    return slides


def upload_slides_to_manus(job: dict) -> None:
    """Two HTTP round-trips per slide, all independent of each other - run
    them in parallel. pool.map returns results in slide order and re-raises
    the first failure, same as the sequential loop it replaces."""
    slides = job["slides"]
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        file_ids = list(pool.map(
            lambda s: manus_client.upload_file(s["imagePath"], os.path.basename(s["imagePath"])), slides))
    for slide, file_id in zip(slides, file_ids):
        slide["manusFileId"] = file_id


def build_alignment_schema() -> dict:
    # Manus's structured-output subset requires every property listed in
    # `required` with additionalProperties:false at every level - confirmed
    # from the real task.create spec, not the usual JSON Schema default.
    return {
        "type": "object",
        "properties": {
            "alignment": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slide_id": {"type": "string"},
                        "transcript_excerpt": {"type": "string"},
                        "confidence": {"type": "number"},
                        "notes": {"type": "string"},
                    },
                    "required": ["slide_id", "transcript_excerpt", "confidence", "notes"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["alignment"],
        "additionalProperties": False,
    }


SLIDES_PER_MANUS_CHUNK = 12
# Confirmed live, not assumed: a real 33-slide/33-file-attachment task.create
# call reproducibly failed with Manus's own internal error ("node server
# request failed"), while the exact same job's transcript+schema succeeded
# fine at 14 slides. The ceiling is somewhere between 14 and 33; 12 is a
# conservative choice under that confirmed-working number.
MANUS_CHUNK_OVERLAP_CHARS = 2500


def run_alignment(job: dict) -> None:
    """Aligns the full transcript to the full slide deck, one Manus task per
    chunk of SLIDES_PER_MANUS_CHUNK slides. Two independent per-call limits
    are both confirmed live: the ~5,000-estimated-token text cap (handled by
    windowing, same as before) and the file-attachment ceiling above
    (handled by chunking) - a single call still can't just send everything
    even for a small-enough slide count, since the full transcript alone can
    exceed the text cap regardless of file count.

    The transcript is split into per-chunk windows proportional to each
    chunk's position in the slide deck, padded with overlap on both sides so
    uneven presenter pacing doesn't strand content right at a chunk
    boundary. This is a heuristic, not exact: a slide whose real narration
    falls outside its chunk's window (even with overlap) will just be
    skipped, the same graceful behavior Manus already has for slides with no
    corresponding content. worker.py doesn't call db.save_job() until this
    whole function returns (or raises), so alignment/manus fields below are
    mutated onto `job` progressively as each chunk completes - if a later
    chunk fails, worker.py's exception handler still persists whatever
    chunks already succeeded (and their Manus credit cost isn't invisible),
    even though a retry can't yet skip redoing them."""
    full_transcript = job["sources"]["srt"]["fullText"]
    total_chars = len(full_transcript)
    slides = job["slides"]
    known_ids = {s["slideId"] for s in slides}

    def resolve_slide_id(raw_id):
        """Manus is asked to echo back our slide_id exactly, but with a real
        (larger, messier) deck it has sometimes returned the attached
        filename instead (e.g. 'pdf-1-p001.png' instead of 'pdf-1-p001') -
        confirmed live, not a guess. Strip a trailing image extension before
        giving up, since that's the one variation actually observed."""
        if raw_id in known_ids:
            return raw_id
        stripped = os.path.splitext(raw_id)[0]
        if stripped in known_ids:
            return stripped
        raise manus_client.ManusTaskError(
            f"Manus returned slide_id {raw_id!r}, which doesn't match any known slide ID {sorted(known_ids)}"
        )

    chunks = [slides[i:i + SLIDES_PER_MANUS_CHUNK] for i in range(0, len(slides), SLIDES_PER_MANUS_CHUNK)]
    num_chunks = len(chunks)

    def chunk_content(i, chunk_slides):
        nominal_start = round(total_chars * i / num_chunks)
        nominal_end = round(total_chars * (i + 1) / num_chunks)
        window = full_transcript[max(0, nominal_start - MANUS_CHUNK_OVERLAP_CHARS):
                                  min(total_chars, nominal_end + MANUS_CHUNK_OVERLAP_CHARS)]
        truncated = False
        if len(window) > MAX_TRANSCRIPT_CHARS:
            window = window[:MAX_TRANSCRIPT_CHARS]
            truncated = True

        slide_meta = [
            {"slide_id": s["slideId"], "pdf_role": next(p["role"] for p in job["sources"]["pdfs"] if p["id"] == s["pdfId"])}
            for s in chunk_slides
        ]
        prompt_text = (
            "You are aligning a video transcript to presentation slide images.\n\n"
            f"This is part {i + 1} of {num_chunks} of a single continuous presentation, split up only "
            "because of a message-size limit - only the slides attached below (a contiguous slice of the "
            "full deck) need aligning in this call.\n\n"
            "Slide images are attached below as file parts, in the order listed here, each tagged 'primary' "
            "or 'supplementary' by source deck. Build the main narrative sequence from the primary deck; pull "
            "a slide from a supplementary deck only when it covers transcript content the primary deck does "
            "not show. Skip slides (title/agenda/blank) that have no corresponding narrated content.\n\n"
            f"Transcript excerpt covering approximately this slice, padded with overlap on each side so "
            f"content right at the boundary isn't missed{' (truncated to fit the message size limit)' if truncated else ''}:\n"
            f"{window}\n\n"
            f"Slide order and role metadata (matches the order of the attached file parts):\n{json.dumps(slide_meta)}\n\n"
            "Return, in narrative order, which of these attached slides correspond to which excerpt of the transcript."
        )
        return [{"type": "text", "text": prompt_text}] + [
            {"type": "file", "file_id": s["manusFileId"]} for s in chunk_slides
        ]

    job["alignment"] = []
    job["manus"]["taskIds"] = []
    seq = 0
    # Chunks are independent, so create up to MANUS_MAX_CONCURRENT_TASKS of
    # them before polling any: Manus works on all of them at once and the
    # window takes about as long as its slowest chunk, not the sum. Polling
    # in chunk order keeps sequenceIndex in deck order, and each chunk's
    # alignment still lands on `job` as soon as it finishes, so a later
    # failure still leaves the earlier chunks' work persisted.
    window_size = max(1, config.MANUS_MAX_CONCURRENT_TASKS)
    indexed_chunks = list(enumerate(chunks))
    for start in range(0, num_chunks, window_size):
        window_task_ids = []
        for i, chunk_slides in indexed_chunks[start:start + window_size]:
            task_id = manus_client.create_task(chunk_content(i, chunk_slides), build_alignment_schema())
            job["manus"]["taskIds"].append(task_id)
            window_task_ids.append(task_id)
        job["manus"]["status"] = "running"

        for task_id in window_task_ids:
            value = manus_client.poll_task(task_id)
            for a in value.get("alignment", []):
                job["alignment"].append({
                    "sequenceIndex": seq,
                    "slideId": resolve_slide_id(a["slide_id"]),
                    "transcriptExcerpt": a["transcript_excerpt"],
                    "confidence": a.get("confidence"),
                    "notes": a.get("notes", ""),
                })
                seq += 1
    job["manus"]["status"] = "stopped"


def _claude_json(prompt: str, what: str) -> dict:
    """One Messages API call whose reply is expected to be a JSON object,
    tolerating a reply that wraps it in prose. Plain requests, matching the
    rest of this module. A reply cut off at max_tokens is a hard error, not
    something to parse - it would silently drop the tail of the output."""
    resp = requests.post(
        f"{config.ANTHROPIC_BASE_URL}/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={"model": "claude-sonnet-5", "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError(f"{what}: Claude's output was cut off at max_tokens - the input needs splitting further")
    text_block = next((block for block in data["content"] if block.get("type") == "text"), None)
    if text_block is None:
        raise RuntimeError(f"{what}: Claude response had no text block (stop_reason={data.get('stop_reason')})")
    text = text_block["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"{what}: Claude response contained no JSON object")
        return json.loads(match.group(0))


def clean_narration(job: dict) -> None:
    """Turns each slide's full aligned transcript excerpt into cleaned
    narration text - no duration budget applied here (see build_short, which
    reads this output). This pass produces job["narration"] once and it's
    permanent from here on - cleaning removes filler and personal references
    only; it must not shape content for any narration-style modality
    (CBT/MI/ACT/etc) - build_short does that, with the short's chosen prompt.
    job["alignment"] is left untouched as the original/raw excerpts, so
    nothing this step does is irreversible.

    Every excerpt is cleaned independently of the others, so the segments
    are split into groups of CLEAN_SEGMENTS_PER_CALL and cleaned by
    CLEAN_CONCURRENCY calls at a time; the result is reassembled in
    alignment order, keyed by sequence_index."""
    alignment = job["alignment"]
    style = job["params"].get("narrationStyle") or "clear, neutral documentary narration"
    segments = [
        {"sequence_index": a["sequenceIndex"], "slide_id": a["slideId"], "excerpt": a["transcriptExcerpt"]}
        for a in alignment
    ]
    groups = [segments[i:i + CLEAN_SEGMENTS_PER_CALL] for i in range(0, len(segments), CLEAN_SEGMENTS_PER_CALL)]

    def clean_group(group):
        prompt = (
            f'Clean each transcript excerpt below into narration matching this style: "{style}".\n'
            "For each excerpt:\n"
            "1. Remove filler - text that doesn't help a listener understand what the narration is trying to "
            "teach, establish, or clarify.\n"
            "2. Remove personal references - names, addresses, designations, and similar identifying details.\n"
            "3. Do NOT shape tone or content for any later narration modality (e.g. CBT, MI, ACT) - keep this "
            "pass style-neutral; that adaptation happens in a separate step.\n"
            "Keep everything else - this is cleaning, not summarizing or shortening. Preserve full teaching "
            "content and keep each segment's sentences natural and self-contained (each plays over one static "
            "image).\n"
            'Respond with ONLY JSON of the shape {"narration":[{"sequence_index":0,"slide_id":"...","script":"..."}]}, '
            "one entry per segment below, keeping each sequence_index exactly as given.\n\n"
            f"Segments:\n{json.dumps(group, indent=2)}"
        )
        return _claude_json(prompt, "clean_narration").get("narration", [])

    with ThreadPoolExecutor(max_workers=CLEAN_CONCURRENCY) as pool:
        results = list(pool.map(clean_group, groups))

    cleaned_by_seq = {n["sequence_index"]: n.get("script", "") for group_result in results for n in group_result}
    missing = [a["sequenceIndex"] for a in alignment if a["sequenceIndex"] not in cleaned_by_seq]
    if missing:
        print(f"warning: clean_narration got no cleaned text back for sequence indexes {missing} - left empty")
    job["narration"] = [
        {"sequenceIndex": a["sequenceIndex"], "slideId": a["slideId"], "script": cleaned_by_seq.get(a["sequenceIndex"], "")}
        for a in alignment
    ]


def build_short(job: dict, short: dict) -> None:
    """Writes one coherent, duration-targeted narrative script from the
    permanent job["narration"] pool (never mutated here - unlike the old
    condense_narration, this never touches the 1:1 alignment) and grounds
    each part of it in an original slideId. No segment gets an independent
    word quota: Claude writes flowing prose covering short["topic"] and
    decides for itself which of the original slides the result belongs
    next to, including dropping most of them - that's the intended
    behavior for a short focused on one topic within a longer deck, or
    condensed to a duration too short to touch every slide.

    Style comes from the prompt-library entry the short was created with
    (short["prompt"], chosen in the Create-short form), falling back to the
    SHORT_NARRATION_PROMPT_NAME entry for shorts that predate that picker.
    job["params"]["narrationStyle"] is clean_narration's field, not this
    one."""
    prompt_ref = short.get("prompt") or {}
    if prompt_ref.get("id"):
        style_prompt_row = db.get_prompt(prompt_ref["id"])
        label = prompt_ref.get("name") or prompt_ref["id"]
    else:
        style_prompt_row = db.get_prompt_by_name(SHORT_NARRATION_PROMPT_NAME)
        label = SHORT_NARRATION_PROMPT_NAME
    if style_prompt_row is None:
        raise RuntimeError(f"build_short: narration prompt {label!r} no longer exists in the prompt library")
    style_prompt = style_prompt_row["text"]

    full_narration = [
        {"sequence_index": n["sequenceIndex"], "slide_id": n["slideId"], "script": n["script"]}
        for n in job["narration"]
    ]
    known_ids = {n["slideId"] for n in job["narration"]}
    target_seconds = short["targetDurationSeconds"]
    total_word_budget = round(target_seconds / 60 * WORDS_PER_MINUTE)

    prompt = (
        f"{style_prompt}\n\n"
        f'Write ONE coherent, flowing narration script on this topic: "{short["topic"]}".\n'
        f"Target length: about {total_word_budget} words total (~{target_seconds}s spoken at "
        f"{WORDS_PER_MINUTE}wpm) - a hard constraint on the final video's runtime, not a suggestion.\n\n"
        "The full presentation's slide-by-slide narration is below, in order, each tagged with its "
        "slide_id. Use it as your only source material. Break your output into ordered segments, each "
        "tagged with the slide_id (from the list below) it plays over - merge several source segments "
        "under one slide_id where useful, skip slides that aren't relevant to the topic or don't fit the "
        "duration, and default to the original order unless the topic genuinely requires reordering. "
        "Never invent a slide_id that isn't in the source list below.\n\n"
        'Respond with ONLY JSON of the shape {"narration":[{"sequence_index":0,"slide_id":"...","script":"..."}]}, '
        "sequence_index being your own output order (0, 1, 2, ...), not the source's.\n\n"
        f"Source narration:\n{json.dumps(full_narration, indent=2)}"
    )

    parsed = _claude_json(prompt, "build_short")

    script = []
    for n in parsed.get("narration", []):
        slide_id = n["slide_id"]
        if slide_id not in known_ids:
            raise RuntimeError(f"build_short: Claude returned slide_id {slide_id!r}, not in job.narration")
        words = len((n.get("script") or "").split())
        script.append({
            "sequenceIndex": n["sequence_index"], "slideId": slide_id, "script": n.get("script", ""),
            "estimatedWords": words, "estimatedSeconds": round((words / WORDS_PER_MINUTE) * 60),
        })
    short["script"] = sorted(script, key=lambda n: n["sequenceIndex"])


def reindex_short(short: dict, ordered_script: list) -> None:
    """Applies a segment delete or insert: ordered_script must already be in
    the final desired order (a deleted segment simply isn't in it; an
    inserted one already sits at its intended position) - some items'
    sequenceIndex may now be stale or absent. Reassigns contiguous
    sequenceIndex 0..N-1 and remaps short["audio"] entries to match by each
    item's OLD sequenceIndex, dropping audio for anything removed. A newly
    inserted segment has no old sequenceIndex to match, so it simply gets no
    audio entry yet - the caller is expected to queue it in
    pendingSegments for the worker to synthesize."""
    seq_map = {}
    for new_seq, item in enumerate(ordered_script):
        old_seq = item.get("sequenceIndex")
        if old_seq is not None:
            seq_map[old_seq] = new_seq
        item["sequenceIndex"] = new_seq
    short["script"] = ordered_script

    remapped_audio = [a for a in short.get("audio", []) if a["sequenceIndex"] in seq_map]
    for a in remapped_audio:
        a["sequenceIndex"] = seq_map[a["sequenceIndex"]]
    short["audio"] = sorted(remapped_audio, key=lambda a: a["sequenceIndex"])


def _voice_cache_path() -> str:
    return os.path.join(config.DATA_DIR, "voice-cache.json")


def resolve_voice(short: dict) -> None:
    voice = short["voice"]
    if voice["mode"] == "preset":
        voice["resolvedVoiceId"] = voice["presetVoiceId"]
        return

    description = voice["customDescription"] or ""
    key = hashlib.sha256(description.encode()).hexdigest()
    cache = {}
    if os.path.exists(_voice_cache_path()):
        cache = json.load(open(_voice_cache_path()))
    if key in cache:
        voice["resolvedVoiceId"] = cache[key]
        return

    resp = requests.post(
        f"{config.ELEVENLABS_BASE_URL}/v1/text-to-voice/design",
        headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"voice_description": description, "text": "This is a preview of the requested narration voice."},
        timeout=60,
    )
    resp.raise_for_status()
    voice_id = resp.json()["voice_id"]
    cache[key] = voice_id
    os.makedirs(config.DATA_DIR, exist_ok=True)
    json.dump(cache, open(_voice_cache_path(), "w"))
    voice["resolvedVoiceId"] = voice_id


def synthesize_audio(short: dict, audio_dir: str, sequence_indexes: set | None = None) -> None:
    """Synthesizes short["script"] into short["audio"]. With sequence_indexes
    given, only (re)synthesizes those segments - one ElevenLabs call each -
    and merges the result into whatever's already in short["audio"], leaving
    every other segment's clip untouched (a post-review text edit or a
    single newly added segment). Without it, synthesizes everything from
    scratch, same as the original one-shot condensing behavior."""
    os.makedirs(audio_dir, exist_ok=True)
    voice_id = short["voice"]["resolvedVoiceId"]
    from mutagen.mp3 import MP3

    segments = [n for n in short["script"] if sequence_indexes is None or n["sequenceIndex"] in sequence_indexes]

    new_audio = {}
    for n in segments:
        resp = requests.post(
            f"{config.ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": n["script"], "model_id": "eleven_multilingual_v2"},
            timeout=60,
        )
        resp.raise_for_status()
        seq = n["sequenceIndex"]
        # A uuid suffix, not just seq, because sequenceIndex is reassigned
        # by reindex_short() on every segment insert/delete after review -
        # reusing "seg-{seq}.mp3" as a bare filename let an unrelated
        # segment's later resynthesis silently overwrite an earlier
        # segment's audio file once their positions happened to coincide.
        path = os.path.join(audio_dir, f"seg-{seq:03d}-{uuid.uuid4().hex[:8]}.mp3")
        with open(path, "wb") as f:
            f.write(resp.content)

        duration = MP3(path).info.length
        new_audio[seq] = {"sequenceIndex": seq, "path": path, "durationSeconds": duration}

    if sequence_indexes is None:
        short["audio"] = list(new_audio.values())
    else:
        merged = {a["sequenceIndex"]: a for a in short.get("audio", [])}
        merged.update(new_audio)
        short["audio"] = sorted(merged.values(), key=lambda a: a["sequenceIndex"])
