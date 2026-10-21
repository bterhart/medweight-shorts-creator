"""Recording mode (job.inputMode == "recording"): a Zoom recording of a
presentation instead of a transcript + PDF. The heavy part - decoding the
video, finding the slides, pulling frames and audio - runs in the Fargate
image (render/ingest.py, dispatched by worker.py via fargate_client). This
module is the cPanel side of it, and its job is to turn the ingest output
into the same three things the transcript path already consumes, so that
cleaning, outlining, shorts, voices and rendering stay shared:

  1. cues  - from the Zoom WebVTT the user uploaded, or from ElevenLabs
             speech-to-text over the ingest's audio parts; written out as
             an SRT under the job's input dir, like a transcript-mode job;
  2. trim  - where the presentation actually starts and ends in the
             recording (one Claude call over the timeline and the words
             spoken in each stretch; the review screen can override it);
  3. alignment - by time, not by Claude: each kept slide segment takes the
             cues spoken while it was on screen. Same entry shape as
             alignment.align_job, so pipeline.clean_narration reads it as is.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import pipeline

STT_CONCURRENCY = 3        # audio parts transcribed at a time (each is up to 20 min of speech)
CUE_MAX_SECONDS = 7.0      # STT words are grouped into cues no longer than this...
CUE_MAX_WORDS = 20         # ...or this many words, breaking early at sentence ends
TRIM_PREVIEW_WORDS = 30    # words from each stretch shown to Claude when it decides the trim
SETUP_SCRAP_SECONDS = 60   # mirrors render/ingest.py: a slide this short before the waiting card is the host setting up
LONG_STATIC_SECONDS = 600  # mirrors render/ingest.py: a slide held this long before the talk is the waiting card
SLIDE_ID_PREFIX = "rec-s"
PDF_ID = "recording"       # what stands in for pdfId on a recording's slides (the UI labels slides "<pdfId> p.<n>")


# ---------------------------------------------------------------- cues ----

def _vtt_time_to_seconds(t: str) -> float:
    parts = t.strip().replace(",", ".").split(":")
    if len(parts) == 2:
        parts = ["0"] + parts
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


_VTT_VOICE_TAG = re.compile(r"</?v[^>]*>")
_VTT_SPEAKER_PREFIX = re.compile(r"^[^:\d][^:]{0,60}:\s+")


def parse_vtt_cues(path: str) -> list:
    """Zoom's WebVTT export as [{start, end, text}] in file order, the same
    shape pipeline.parse_srt_cues produces. Zoom prefixes each cue with the
    speaker ("Jane Doe: ..."); that and <v> voice tags are stripped."""
    text = open(path, "r", encoding="utf-8-sig", errors="replace").read().replace("\r", "")
    cues = []
    for block in text.split("\n\n"):
        lines = [l for l in block.split("\n") if l.strip()]
        time_line = next((l for l in lines if "-->" in l), None)
        if not time_line:
            continue
        start, end = time_line.split("-->")
        end = end.strip().split(" ")[0]  # cue settings may follow the end time
        body = " ".join(lines[lines.index(time_line) + 1:]).strip()
        body = _VTT_VOICE_TAG.sub("", body)
        body = _VTT_SPEAKER_PREFIX.sub("", body).strip()
        if not body:
            continue
        cues.append({"start": _vtt_time_to_seconds(start), "end": _vtt_time_to_seconds(end), "text": body})
    return cues


def transcribe_words(path: str) -> list:
    """ElevenLabs speech-to-text over one audio part: its words with
    timestamps, [{text, start, end}], spacing entries dropped. Raises on
    failure - a recording with no transcript has nothing to narrate, so
    this is not the warning-only path pipeline.transcribe_video takes for
    an embedded clip."""
    with open(path, "rb") as f:
        resp = requests.post(
            f"{config.ELEVENLABS_BASE_URL}/v1/speech-to-text",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY},
            data={"model_id": "scribe_v1", "timestamps_granularity": "word", "tag_audio_events": "false"},
            files={"file": (os.path.basename(path), f)},
            timeout=900,
        )
    resp.raise_for_status()
    words = []
    for w in resp.json().get("words", []):
        if w.get("type", "word") != "word" or not (w.get("text") or "").strip():
            continue
        words.append({"text": w["text"].strip(), "start": float(w["start"]), "end": float(w["end"])})
    return words


def words_to_cues(words: list, offset_seconds: float = 0.0) -> list:
    """Groups timestamped words into subtitle-sized cues: a cue ends at a
    sentence end once it has a few words, or when it would exceed
    CUE_MAX_SECONDS / CUE_MAX_WORDS. offset_seconds shifts a part's
    timestamps to recording time."""
    cues, cur = [], []

    def flush():
        if cur:
            cues.append({"start": cur[0]["start"] + offset_seconds, "end": cur[-1]["end"] + offset_seconds,
                         "text": " ".join(w["text"] for w in cur)})
            cur.clear()

    for w in words:
        if cur and (w["end"] - cur[0]["start"] > CUE_MAX_SECONDS or len(cur) >= CUE_MAX_WORDS):
            flush()
        cur.append(w)
        if len(cur) >= 4 and w["text"][-1:] in ".?!":
            flush()
    flush()
    return cues


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n{c['text']}\n\n")


def recording_cues(job: dict) -> list:
    """The recording's cues, from the uploaded Zoom VTT when there is one,
    else from speech-to-text over the ingest's audio parts (which start at
    the ingest's audio start, well before the talk - see
    ingest.content_start_hint). Writes them as the job's transcript.srt and
    fills job.sources.srt the way transcript mode's parse step does, so the
    rest of the app sees a transcript-mode job from here on."""
    rec = job["sources"]["recording"]
    vtt = rec.get("vtt")
    if vtt and vtt.get("path") and os.path.isfile(vtt["path"]):
        cues = parse_vtt_cues(vtt["path"])
        source = "zoom-vtt"
        if not cues:
            raise RuntimeError(f"the uploaded transcript {vtt.get('filename')} has no cues")
    else:
        parts = rec.get("audio") or []
        if not parts:
            raise RuntimeError("the ingest produced no audio parts, so there is nothing to transcribe")
        with ThreadPoolExecutor(max_workers=STT_CONCURRENCY) as pool:
            word_lists = list(pool.map(lambda p: transcribe_words(p["path"]), parts))
        cues = []
        for part, words in zip(parts, word_lists):
            cues.extend(words_to_cues(words, part["offsetSeconds"]))
        source = "elevenlabs-stt"
        if not cues:
            raise RuntimeError("speech-to-text returned no words for the recording")
    cues.sort(key=lambda c: (c["start"], c["end"]))
    srt_path = os.path.join(config.DATA_DIR, "jobs", job["jobId"], "input", "transcript.srt")
    write_srt(cues, srt_path)
    job["sources"]["srt"] = {
        "filename": (vtt or {}).get("filename") or "transcript.srt (speech-to-text)",
        "path": srt_path,
        "cueCount": len(cues),
        "durationSeconds": cues[-1]["end"],
        "fullText": " ".join(c["text"] for c in cues),
    }
    rec["transcriptSource"] = source
    return cues


# ---------------------------------------------------------------- trim ----

def _cues_in(cues: list, start: float, end: float) -> list:
    return [c for c in cues if start <= (c["start"] + c["end"]) / 2 < end]


def default_trim(segments: list) -> dict:
    """Without Claude: from the first slide after the opening run (the
    same run ingest.content_start_hint skips, plus any camera stretch
    right after the waiting card, which is the host's introduction) to the
    end of the last slide segment."""
    def dur(s):
        return s["endSeconds"] - s["startSeconds"]

    i = 0
    while i < len(segments):
        s = segments[i]
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        if s["kind"] == "placeholder" or (s["kind"] == "slide" and (
                dur(s) >= LONG_STATIC_SECONDS
                or (dur(s) < SETUP_SCRAP_SECONDS and nxt and nxt["kind"] == "slide" and dur(nxt) >= LONG_STATIC_SECONDS))):
            i += 1
            continue
        if s["kind"] == "camera" and i > 0:
            i += 1
            continue
        break
    first = next((s for s in segments[i:] if s["kind"] == "slide"), None)
    last = next((s for s in reversed(segments) if s["kind"] == "slide"), None)
    if not first or not last:
        return {"startSeconds": 0.0, "endSeconds": segments[-1]["endSeconds"] if segments else 0.0,
                "excludedSegments": [], "reason": "no slide segments found", "source": "default"}
    return {"startSeconds": first["startSeconds"], "endSeconds": last["endSeconds"], "excludedSegments": [],
            "reason": "first content slide after the opening run, through the last slide", "source": "default"}


def trim_recording(job: dict, cues: list) -> dict:
    """Asks Claude where the presentation proper starts and ends, given the
    timeline and the first words spoken in each stretch; falls back to
    default_trim if the call fails or returns nonsense. The result is kept
    on job.sources.recording.trim; the review screen can replace it."""
    rec = job["sources"]["recording"]
    segments = rec["timeline"]
    fallback = default_trim(segments)
    rows = []
    for s in segments:
        words = " ".join(c["text"] for c in _cues_in(cues, s["startSeconds"], s["endSeconds"])).split()
        rows.append({
            "index": s["index"], "kind": s["kind"],
            "start": pipeline_clock(s["startSeconds"]), "end": pipeline_clock(s["endSeconds"]),
            "seconds": round(s["endSeconds"] - s["startSeconds"]),
            "wordsSpoken": len(words),
            "firstWords": " ".join(words[:TRIM_PREVIEW_WORDS]),
        })
    prompt = (
        "A Zoom recording of a slide presentation has been split into stretches of time: 'slide' (a slide was on "
        "screen), 'camera' (someone on camera, no slide), 'placeholder' (camera off / nothing shown). Recordings "
        "start early (a waiting-room card with music, housekeeping, introductions) and run past the end (thanks, "
        "questions about logistics, goodbyes). Decide the span that is the presentation itself - the teaching "
        "content a viewer would want - and name any stretches inside that span that are not part of it (a break, "
        "a technical interruption, an aside about scheduling).\n"
        "Rules: start at the beginning of the first stretch that belongs to the talk, including an on-camera "
        "introduction of the topic if there is one; end at the end of the last stretch with teaching content. "
        "Q&A about the content counts as content; logistics do not. When unsure, include rather than exclude.\n"
        f"Suggested default: start {pipeline_clock(fallback['startSeconds'])}, end {pipeline_clock(fallback['endSeconds'])}.\n"
        'Respond with ONLY JSON: {"startIndex": <index of the first stretch>, "endIndex": <index of the last stretch>, '
        '"excludedIndexes": [<indexes inside the span to leave out>], "reason": "<one sentence>"}.\n\n'
        f"Stretches:\n{json.dumps(rows, indent=1)}"
    )
    try:
        out = pipeline._claude_json(prompt, "trim_recording")
        by_index = {s["index"]: s for s in segments}
        first, last = by_index[int(out["startIndex"])], by_index[int(out["endIndex"])]
        if last["endSeconds"] <= first["startSeconds"]:
            raise ValueError("end before start")
        excluded = sorted({int(i) for i in out.get("excludedIndexes", []) if int(i) in by_index
                           and first["index"] < int(i) < last["index"]})
        trim = {"startSeconds": first["startSeconds"], "endSeconds": last["endSeconds"], "excludedSegments": excluded,
                "reason": str(out.get("reason", "")).strip(), "source": "claude"}
    except Exception as e:
        print(f"warning: trim_recording fell back to the default span: {type(e).__name__}: {e}")
        trim = dict(fallback)
        trim["reason"] = f"{fallback['reason']} (Claude's suggestion was unusable: {type(e).__name__})"
    rec["trim"] = trim
    return trim


def pipeline_clock(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ----------------------------------------------------------- alignment ----

def slides_from_timeline(job: dict) -> list:
    """One job slide per detected slide segment, whatever the trim - the
    review screen's slide picker may use any of them. pdfId/pageNumber
    stand in for what the UI and render view expect of a PDF slide."""
    rec = job["sources"]["recording"]
    slides = []
    for s in rec["timeline"]:
        if s["kind"] != "slide":
            continue
        slides.append({
            "slideId": f"{SLIDE_ID_PREFIX}{s['index']:03d}", "pdfId": PDF_ID, "pageNumber": len(slides) + 1,
            "imagePath": s["framePath"], "previewImagePath": s.get("previewPath") or s["framePath"],
            "textPath": None, "imageAudit": None,
            "recordingSegment": {"index": s["index"], "startSeconds": s["startSeconds"], "endSeconds": s["endSeconds"]},
        })
    return slides


def align_recording(job: dict, cues: list) -> None:
    """job["alignment"] by time: every slide segment inside the trim (and
    not excluded) takes the cues spoken while it was on screen. Speech
    during a camera or placeholder stretch inside the trim belongs to the
    slide before it - a presenter who stops sharing is still on that topic
    - or to the first slide after it when nothing precedes. Slide segments
    with no words at all are left out: there is nothing to narrate."""
    rec = job["sources"]["recording"]
    trim = rec["trim"]
    excluded = set(trim.get("excludedSegments") or [])
    start, end = trim["startSeconds"], trim["endSeconds"]
    kept = [s for s in rec["timeline"]
            if s["index"] not in excluded and s["endSeconds"] > start and s["startSeconds"] < end]
    if not any(s["kind"] == "slide" for s in kept):
        raise RuntimeError("no slide segment falls inside the trimmed span - widen the trim on the review screen")

    cue_index = {id(c): i for i, c in enumerate(cues)}
    buckets = []  # [slide segment, [cue indexes]]
    orphans = []  # cue indexes spoken before the first kept slide
    for s in kept:
        s_cues = [cue_index[id(c)] for c in _cues_in(cues, max(s["startSeconds"], start), min(s["endSeconds"], end))]
        if s["kind"] == "slide":
            buckets.append([s, orphans + s_cues])
            orphans = []
        elif buckets:
            buckets[-1][1].extend(s_cues)
        else:
            orphans.extend(s_cues)

    alignment = []
    for s, idxs in buckets:
        if not idxs:
            continue
        idxs.sort()
        alignment.append({
            "sequenceIndex": len(alignment),
            "slideId": f"{SLIDE_ID_PREFIX}{s['index']:03d}",
            "startCue": idxs[0],
            "endCue": idxs[-1],
            "startSeconds": cues[idxs[0]]["start"],
            "endSeconds": cues[idxs[-1]]["end"],
            "transcriptExcerpt": " ".join(cues[i]["text"] for i in idxs),
            "confidence": 1.0,
            "notes": f"spoken {pipeline_clock(s['startSeconds'])}-{pipeline_clock(s['endSeconds'])} while this slide was on screen",
        })
    if not alignment:
        raise RuntimeError("no words were spoken over any slide inside the trimmed span")
    job["alignment"] = alignment
