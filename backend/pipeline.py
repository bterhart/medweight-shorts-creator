"""Pipeline steps - PDF/SRT extraction, narration cleaning, short writing,
voice/TTS. (Slide-to-transcript alignment lives in alignment.py.) Each
function takes and mutates a job dict; the caller (worker.py) saves to the
DB between steps so status polls see progress as it happens, same as the
checkpoint pattern in the earlier n8n version."""
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

WORDS_PER_MINUTE = 130
# The prompt-library entry build_short() falls back to when a short doesn't
# name one (shorts created before the Create-short form had a prompt picker).
# Referenced by name, not pasted as a literal, so editing it in the library
# takes effect immediately. Never job["params"]["narrationStyle"] - that's
# clean_narration's style-neutral field, not this stage's.
SHORT_NARRATION_PROMPT_NAME = "Second pass (CBT/MI/ACT/DBT narration)"
# clean_narration is output-bound: the cleaned text is nearly as long as the
# transcript, so one call re-emits the whole talk. Splitting the segments
# across concurrent calls divides that wall time and keeps each call well
# under max_tokens - a single call could silently truncate a long transcript.
CLEAN_SEGMENTS_PER_CALL = 10
CLEAN_CONCURRENCY = 4


def _srt_time_to_seconds(t: str) -> float:
    h, m, rest = t.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt_cues(srt_path: str) -> list:
    """Every cue as {start, end, text} (seconds), in file order. The cue's
    position in this list is the index alignment.py asks Claude to cite, so
    the SRT's own (possibly gappy) numbering is deliberately not used."""
    text = open(srt_path, "r", encoding="utf-8", errors="replace").read()
    blocks = [b for b in text.replace("\r", "").split("\n\n") if b.strip()]
    cues = []
    for block in blocks:
        lines = block.split("\n")
        time_line = next((l for l in lines if "-->" in l), None)
        if not time_line:
            continue
        idx = lines.index(time_line)
        start, end = time_line.split("-->")
        cues.append({
            "start": _srt_time_to_seconds(start),
            "end": _srt_time_to_seconds(end),
            "text": " ".join(lines[idx + 1:]).strip(),
        })
    return cues


def parse_srt(srt_path: str) -> dict:
    cues = parse_srt_cues(srt_path)
    duration = cues[-1]["end"] if cues else 0
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
                "imagePath": image_path, "textPath": text_path,
            })
    finally:
        doc.close()
    return slides


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
