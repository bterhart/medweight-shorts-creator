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
import manus_client

WORDS_PER_MINUTE = 130
# Manus caps message.content's combined text at ~5000 estimated tokens with
# no way around it by splitting - a real constraint hit during development,
# not a guess. ~4 chars/token is a conservative estimate that leaves margin.
MAX_TRANSCRIPT_CHARS = 16000


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


def run_alignment(job: dict) -> None:
    """Creates the Manus task and polls it to completion in-process - a
    simple blocking loop, since we're no longer constrained to n8n's
    cyclic-node polling pattern."""
    transcript = job["sources"]["srt"]["fullText"]
    truncated = False
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]
        truncated = True

    slide_meta = [
        {"slide_id": s["slideId"], "pdf_role": next(p["role"] for p in job["sources"]["pdfs"] if p["id"] == s["pdfId"])}
        for s in job["slides"]
    ]
    prompt_text = (
        "You are aligning a video transcript to presentation slide images.\n\n"
        "Slide images are attached below as file parts, in the order listed here, each tagged 'primary' "
        "or 'supplementary' by source deck. Build the main narrative sequence from the primary deck; pull "
        "a slide from a supplementary deck only when it covers transcript content the primary deck does "
        "not show. Skip slides (title/agenda/blank) that have no corresponding narrated content.\n\n"
        f"Full transcript{' (truncated to fit the message size limit)' if truncated else ''}:\n{transcript}\n\n"
        f"Slide order and role metadata (matches the order of the attached file parts):\n{json.dumps(slide_meta)}\n\n"
        "Return, in narrative order, which slides correspond to which excerpt of the transcript."
    )
    content = [{"type": "text", "text": prompt_text}] + [
        {"type": "file", "file_id": s["manusFileId"]} for s in job["slides"]
    ]

    task_id = manus_client.create_task(content, build_alignment_schema())
    job["manus"]["taskId"] = task_id
    job["manus"]["status"] = "running"

    value = manus_client.poll_task(task_id)
    job["manus"]["status"] = "stopped"
    job["alignment"] = [
        {
            "sequenceIndex": i,
            "slideId": a["slide_id"],
            "transcriptExcerpt": a["transcript_excerpt"],
            "confidence": a.get("confidence"),
            "notes": a.get("notes", ""),
        }
        for i, a in enumerate(value.get("alignment", []))
    ]


def condense_narration(job: dict) -> None:
    alignment = job["alignment"]
    total_chars = sum(len(a["transcriptExcerpt"]) for a in alignment) or 1
    target_seconds = job["params"]["targetDurationSeconds"]
    total_word_budget = round(target_seconds / 60 * WORDS_PER_MINUTE)

    segments = [
        {
            "sequence_index": a["sequenceIndex"],
            "slide_id": a["slideId"],
            "excerpt": a["transcriptExcerpt"],
            "word_budget": max(5, round((len(a["transcriptExcerpt"]) / total_chars) * total_word_budget)),
        }
        for a in alignment
    ]
    style = job["params"].get("narrationStyle") or "clear, neutral documentary narration"
    prompt = (
        f'Condense each transcript excerpt below into narration matching this style: "{style}".\n'
        f"Total narration across all segments must sum to about {total_word_budget} words "
        f"(~{target_seconds}s spoken at {WORDS_PER_MINUTE}wpm).\n"
        "Each segment lists its own word budget; stay close to it while keeping sentences natural and "
        "self-contained (each plays over one static image).\n"
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
        json={"model": "claude-sonnet-5", "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {"narration": []}

    narration = []
    for n in parsed.get("narration", []):
        words = len((n.get("script") or "").split())
        narration.append({
            "sequenceIndex": n["sequence_index"], "slideId": n["slide_id"], "script": n.get("script", ""),
            "estimatedWords": words, "estimatedSeconds": round((words / WORDS_PER_MINUTE) * 60),
        })
    job["narration"] = sorted(narration, key=lambda n: n["sequenceIndex"])


def _voice_cache_path() -> str:
    return os.path.join(config.DATA_DIR, "voice-cache.json")


def resolve_voice(job: dict) -> None:
    voice = job["params"]["voice"]
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


def synthesize_audio(job: dict, audio_dir: str) -> None:
    os.makedirs(audio_dir, exist_ok=True)
    voice_id = job["params"]["voice"]["resolvedVoiceId"]
    audio = []
    for n in job["narration"]:
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

        from moviepy import AudioFileClip
        duration = AudioFileClip(path).duration

        audio.append({"sequenceIndex": seq, "path": path, "durationSeconds": duration})
    job["audio"] = audio
