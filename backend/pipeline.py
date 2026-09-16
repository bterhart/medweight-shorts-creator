"""Phase 1 pipeline steps - PDF/SRT extraction, Manus alignment, narration
condensation, voice/TTS. Each function takes and mutates a job dict; the
caller (worker.py) saves to the DB between steps so status polls see
progress as it happens, same as the checkpoint pattern in the earlier n8n
version."""
import hashlib
import json
import os
import re

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
# build_short() always uses this prompt for style, regardless of any
# per-job narrationStyle text (that's clean_narration's field, not this
# stage's) - the full CBT/MI/ACT/DBT framing belongs here, not in the
# style-neutral cleaning pass. Referenced by name (not pasted as a literal
# string) so editing it in the prompt library takes effect immediately.
SHORT_NARRATION_PROMPT_NAME = "Second pass (CBT/MI/ACT/DBT narration)"


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
    for slide in job["slides"]:
        slide["manusFileId"] = manus_client.upload_file(slide["imagePath"], os.path.basename(slide["imagePath"]))


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

    job["alignment"] = []
    job["manus"]["taskIds"] = []
    seq = 0
    for i, chunk_slides in enumerate(chunks):
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
        content = [{"type": "text", "text": prompt_text}] + [
            {"type": "file", "file_id": s["manusFileId"]} for s in chunk_slides
        ]

        task_id = manus_client.create_task(content, build_alignment_schema())
        job["manus"]["taskIds"].append(task_id)
        job["manus"]["status"] = "running"

        value = manus_client.poll_task(task_id)
        job["manus"]["status"] = "stopped"

        for a in value.get("alignment", []):
            job["alignment"].append({
                "sequenceIndex": seq,
                "slideId": resolve_slide_id(a["slide_id"]),
                "transcriptExcerpt": a["transcript_excerpt"],
                "confidence": a.get("confidence"),
                "notes": a.get("notes", ""),
            })
            seq += 1


def clean_narration(job: dict) -> None:
    """Turns each slide's full aligned transcript excerpt into cleaned
    narration text - no duration budget applied here (see build_short, which
    reads this output). This pass produces job["narration"] once and it's
    permanent from here on - cleaning removes filler and personal references
    only; it must not shape content for any narration-style modality
    (CBT/MI/ACT/etc) - build_short does that, with its own hardcoded prompt.
    job["alignment"] is left untouched as the
    original/raw excerpts, so nothing this step does is irreversible."""
    alignment = job["alignment"]
    style = job["params"].get("narrationStyle") or "clear, neutral documentary narration"
    segments = [
        {"sequence_index": a["sequenceIndex"], "slide_id": a["slideId"], "excerpt": a["transcriptExcerpt"]}
        for a in alignment
    ]
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
        'Respond with ONLY JSON of the shape {"narration":[{"sequence_index":0,"slide_id":"...","script":"..."}]}.\n\n'
        f"Segments:\n{json.dumps(segments, indent=2)}"
    )

    resp = requests.post(
        f"{config.ANTHROPIC_BASE_URL}/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        # Unlike condense_narration, output length scales with the source
        # material (minus filler), not a fixed word budget - a long real
        # transcript may need this raised further, or split across multiple
        # calls (deferred - see the chunking follow-up discussed with the
        # user).
        json={"model": "claude-sonnet-5", "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["content"]
    text_block = next((block for block in content if block.get("type") == "text"), None)
    if text_block is None:
        raise RuntimeError(
            f"clean_narration: Claude response had no text block (stop_reason={data.get('stop_reason')})"
        )
    text = text_block["text"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {"narration": []}

    job["narration"] = [
        {"sequenceIndex": n["sequence_index"], "slideId": n["slide_id"], "script": n.get("script", "")}
        for n in parsed.get("narration", [])
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

    Always uses the hardcoded SHORT_NARRATION_PROMPT_NAME prompt (full
    CBT/MI/ACT/DBT framing) for style. job["params"]["narrationStyle"] is
    clean_narration's field, not this one."""
    style_prompt_row = db.get_prompt_by_name(SHORT_NARRATION_PROMPT_NAME)
    if style_prompt_row is None:
        raise RuntimeError(f"build_short: no narration_prompts row named {SHORT_NARRATION_PROMPT_NAME!r}")
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
    content = data["content"]
    text_block = next((block for block in content if block.get("type") == "text"), None)
    if text_block is None:
        raise RuntimeError(
            f"build_short: Claude response had no text block (stop_reason={data.get('stop_reason')})"
        )
    text = text_block["text"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {"narration": []}

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


def synthesize_audio(short: dict, audio_dir: str) -> None:
    os.makedirs(audio_dir, exist_ok=True)
    voice_id = short["voice"]["resolvedVoiceId"]
    audio = []
    for n in short["script"]:
        resp = requests.post(
            f"{config.ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": n["script"], "model_id": "eleven_multilingual_v2"},
            timeout=60,
        )
        resp.raise_for_status()
        seq = n["sequenceIndex"]
        path = os.path.join(audio_dir, f"seg-{seq:03d}.mp3")
        with open(path, "wb") as f:
            f.write(resp.content)

        from mutagen.mp3 import MP3
        duration = MP3(path).info.length

        audio.append({"sequenceIndex": seq, "path": path, "durationSeconds": duration})
    short["audio"] = audio
