"""Flask app for the job pipeline and its shorts: POST /jobs, GET
/jobs/<id>/status, POST /jobs/<id>/shorts, POST /jobs/<id>/shorts/<shortId>/render,
GET /files, plus the narration-prompts library."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pymysql
from flask import Flask, jsonify, request, send_file, abort

import config
import db
import pipeline

app = Flask(__name__)

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def job_dir(job_id: str) -> str:
    return os.path.join(config.DATA_DIR, "jobs", job_id)


def _find_short(job: dict, short_id: str) -> dict | None:
    return next((s for s in job.get("shorts", []) if s["shortId"] == short_id), None)


def _check_short_editable(job: dict, short: dict):
    """Segment edits (text, image, delete, add) are allowed once a short has
    a script+audio to edit and aren't themselves mid-pipeline - same
    ready_for_render/done window the review UI already shows the segment
    list for. Returns a Flask error response, or None if editing may proceed."""
    if short["phase"] not in ("ready_for_render", "done"):
        return jsonify({"error": f"short is not editable (phase={short['phase']})"}), 400
    if job.get("activeShortId"):
        return jsonify({"error": "another short is currently processing for this job - try again once it finishes"}), 409
    return None


@app.post("/jobs")
def create_job():
    params = json.loads(request.form["params"])
    job_id = db.new_job_id()
    now = now_iso()

    pdf_meta = []
    for i, p in enumerate(params["pdfs"]):
        pdf_meta.append({
            "id": f"pdf-{i + 1}",
            "filename": p["filename"],
            "path": os.path.join(job_dir(job_id), "input", f"pdf-{i + 1}.pdf"),
            "role": p["role"],
            "order": p.get("order", i),
        })

    job = {
        "jobId": job_id, "createdAt": now, "updatedAt": now,
        "phase": "prepare", "step": "saving_inputs", "error": None,
        "params": {
            "narrationStyle": params.get("narrationStyle", ""),
            "transition": {
                "type": params["transition"]["type"],
                "transitionSeconds": params["transition"].get("transitionSeconds", 0.75),
                "minSlideSeconds": params["transition"].get("minSlideSeconds", 3.0),
            },
            "aspectRatio": params.get("aspectRatio", "16:9"),
            "resolution": params.get("resolution", "640x360"),
            "captions": bool(params.get("captions", False)),
        },
        "sources": {
            "srt": {
                "filename": params["srtFilename"],
                "path": os.path.join(job_dir(job_id), "input", "transcript.srt"),
                "cueCount": None, "durationSeconds": None, "fullText": None,
            },
            "pdfs": pdf_meta,
        },
        "slides": [], "alignment": [], "narration": [], "shorts": [], "activeShortId": None,
        "manus": {"taskIds": [], "status": None},
    }

    for d in ("input", "slides", "audio", "output"):
        os.makedirs(os.path.join(job_dir(job_id), d), exist_ok=True)

    request.files["srt"].save(job["sources"]["srt"]["path"])
    for i, pdf in enumerate(pdf_meta):
        request.files[f"pdf_{i}"].save(pdf["path"])

    # job.json exists (as a DB row, now) before the client ever receives the
    # jobId, same fix as the earlier n8n version's ack-vs-checkpoint race.
    db.create_job(job)

    return jsonify({"jobId": job_id, "phase": job["phase"], "step": job["step"]})


@app.get("/jobs")
def list_jobs():
    return jsonify(db.list_jobs())


@app.get("/jobs/<job_id>/status")
def job_status(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.post("/jobs/<job_id>/shorts")
def create_short(job_id):
    """Creates a new short: a duration-targeted, topic-focused condensed
    narrative built from the job's permanent 1:1 narration (see
    pipeline.build_short). A job can hold any number of these - e.g. a 90s
    teaser and a 5-minute full summary side by side, built and re-rendered
    independently."""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["phase"] != "ready_for_review":
        return jsonify({"error": f"job is not ready for a new short (phase={job['phase']})"}), 400
    if job.get("activeShortId"):
        return jsonify({"error": "another short is currently processing for this job - try again once it finishes"}), 409

    body = request.get_json(silent=True) or {}
    topic = (body.get("topic") or "").strip()
    target_duration = body.get("targetDurationSeconds")
    voice = body.get("voice") or {}
    if not topic:
        return jsonify({"error": "topic is required"}), 400
    if not target_duration or not voice.get("mode"):
        return jsonify({"error": "targetDurationSeconds and voice are required"}), 400

    # Which prompt-library entry writes this short. Resolved to id+name now so
    # the short records what it was built with even if the entry is later
    # renamed; an omitted promptId keeps the pre-picker default.
    prompt_ref = {"id": None, "name": pipeline.SHORT_NARRATION_PROMPT_NAME}
    if body.get("promptId"):
        row = db.get_prompt(body["promptId"])
        if row is None:
            return jsonify({"error": "promptId does not match any saved prompt"}), 400
        prompt_ref = {"id": row["id"], "name": row["name"]}

    short_id = str(uuid.uuid4())
    short = {
        "shortId": short_id, "createdAt": now_iso(),
        "topic": topic, "targetDurationSeconds": target_duration,
        "prompt": prompt_ref,
        "voice": {
            "mode": voice["mode"],
            "presetVoiceId": voice.get("presetVoiceId"),
            "customDescription": voice.get("customDescription"),
            "resolvedVoiceId": None,
        },
        "phase": "condensing", "step": "condensing_narration", "error": None,
        "script": [], "audio": [],
        "render": {"renderedAt": None, "outputUrl": None, "renderCount": 0},
    }
    job.setdefault("shorts", []).append(short)
    job["activeShortId"] = short_id
    job["phase"] = "condensing"
    job["step"] = "condensing_narration"
    db.save_job(job)

    return jsonify({"jobId": job_id, "shortId": short_id, "phase": job["phase"], "step": job["step"]})


@app.post("/jobs/<job_id>/shorts/<short_id>/render")
def trigger_short_render(job_id, short_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    short = next((s for s in job.get("shorts", []) if s["shortId"] == short_id), None)
    if not short:
        return jsonify({"error": "short not found"}), 404
    if short["phase"] not in ("ready_for_render", "done"):
        return jsonify({"error": f"short is not ready for render (phase={short['phase']})"}), 400
    if job.get("activeShortId"):
        return jsonify({"error": "another short is currently processing for this job - try again once it finishes"}), 409

    body = request.get_json(silent=True) or {}
    overrides = {}
    if body.get("transitionType"):
        overrides["type"] = body["transitionType"]
    if body.get("transitionSeconds") is not None:
        overrides["transitionSeconds"] = body["transitionSeconds"]
    if body.get("minSlideSeconds") is not None:
        overrides["minSlideSeconds"] = body["minSlideSeconds"]
    if body.get("resolution"):
        overrides["resolution"] = body["resolution"]
    if body.get("includeIntro"):
        overrides["includeIntro"] = True
    if body.get("includeOutro"):
        overrides["includeOutro"] = True

    render_state = short.setdefault("render", {})
    # A re-render replaces the previous render rather than sitting beside
    # it: drop the stale preview from the UI now, and leave its number for
    # the worker to delete from S3 on dispatch (Flask never talks to AWS).
    if render_state.get("outputUrl"):
        render_state["staleRenderCount"] = render_state.get("renderCount", 0)
        render_state["outputUrl"] = None
        render_state["renderedAt"] = None
    render_state["pendingOverrides"] = overrides
    short["phase"] = "rendering"
    short["step"] = "rendering"
    job["activeShortId"] = short_id
    job["phase"] = "rendering"
    job["step"] = "rendering"
    db.save_job(job)

    return jsonify({"jobId": job_id, "shortId": short_id, "phase": job["phase"], "step": job["step"]})


@app.patch("/jobs/<job_id>/shorts/<short_id>/segments/<int:seq>")
def edit_segment_text(job_id, short_id, seq):
    """Rewrites one segment's narration text - queues just that segment for
    resynthesis (phase='editing') rather than touching any other segment's
    audio. Only available once a short has a script+audio to edit."""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    short = _find_short(job, short_id)
    if not short:
        return jsonify({"error": "short not found"}), 404
    err = _check_short_editable(job, short)
    if err:
        return err
    segment = next((n for n in short["script"] if n["sequenceIndex"] == seq), None)
    if not segment:
        return jsonify({"error": "segment not found"}), 404

    body = request.get_json(silent=True) or {}
    text = (body.get("script") or "").strip()
    if not text:
        return jsonify({"error": "script is required"}), 400

    segment["script"] = text
    words = len(text.split())
    segment["estimatedWords"] = words
    segment["estimatedSeconds"] = round((words / pipeline.WORDS_PER_MINUTE) * 60)

    short["pendingSegments"] = [seq]
    short["phase"] = "editing"
    short["step"] = "resynthesizing_audio"
    job["activeShortId"] = short_id
    job["phase"] = "editing"
    job["step"] = "resynthesizing_audio"
    db.save_job(job)

    return jsonify({"jobId": job_id, "shortId": short_id, "phase": job["phase"], "step": job["step"]})


@app.post("/jobs/<job_id>/shorts/<short_id>/segments/<int:seq>/image")
def replace_segment_image(job_id, short_id, seq):
    """Replaces one segment's image, either with an uploaded file (an
    'image' form file - need not be one of the deck's own slides) or by
    pointing it at a different slide from the job's full original deck (a
    'sourceSlideId' form field). No audio impact, so this applies
    immediately rather than going through the editing phase."""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    short = _find_short(job, short_id)
    if not short:
        return jsonify({"error": "short not found"}), 404
    err = _check_short_editable(job, short)
    if err:
        return err
    segment = next((n for n in short["script"] if n["sequenceIndex"] == seq), None)
    if not segment:
        return jsonify({"error": "segment not found"}), 404

    upload = request.files.get("image")
    source_slide_id = request.form.get("sourceSlideId")

    if upload:
        ext = os.path.splitext(upload.filename or "")[1].lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            return jsonify({"error": f"unsupported image type {ext!r} - use png, jpg, or webp"}), 400
        custom_dir = os.path.join(job_dir(job_id), "shorts", short_id, "custom-slides")
        os.makedirs(custom_dir, exist_ok=True)
        path = os.path.join(custom_dir, f"seg-{seq:03d}-{uuid.uuid4().hex[:8]}{ext}")
        upload.save(path)
        segment["customImagePath"] = path
    elif source_slide_id:
        if not any(s["slideId"] == source_slide_id for s in job.get("slides", [])):
            return jsonify({"error": f"no slide {source_slide_id!r} in this job's deck"}), 400
        segment["slideId"] = source_slide_id
        segment["customImagePath"] = None
    else:
        return jsonify({"error": "either an 'image' file upload or a 'sourceSlideId' is required"}), 400

    if short["phase"] == "done":
        short["phase"] = "ready_for_render"
        short["step"] = "ready_for_render"
    db.save_job(job)

    return jsonify({"jobId": job_id, "shortId": short_id, "sequenceIndex": seq, "phase": short["phase"]})


@app.delete("/jobs/<job_id>/shorts/<short_id>/segments/<int:seq>")
def delete_segment(job_id, short_id, seq):
    """Removes one segment (script, audio, and any custom image reference)
    and renumbers the rest to stay contiguous. No audio impact on the
    segments that remain, so this applies immediately."""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    short = _find_short(job, short_id)
    if not short:
        return jsonify({"error": "short not found"}), 404
    err = _check_short_editable(job, short)
    if err:
        return err
    if not any(n["sequenceIndex"] == seq for n in short["script"]):
        return jsonify({"error": "segment not found"}), 404
    if len(short["script"]) <= 1:
        return jsonify({"error": "a short must keep at least one segment"}), 400

    remaining = sorted((n for n in short["script"] if n["sequenceIndex"] != seq), key=lambda n: n["sequenceIndex"])
    pipeline.reindex_short(short, remaining)

    if short["phase"] == "done":
        short["phase"] = "ready_for_render"
        short["step"] = "ready_for_render"
    db.save_job(job)

    return jsonify({"jobId": job_id, "shortId": short_id, "phase": short["phase"]})


@app.post("/jobs/<job_id>/shorts/<short_id>/segments")
def add_segment(job_id, short_id):
    """Inserts a new segment at 'position' (0-based, default: end) with the
    given narration text and an uploaded image (required - a new segment
    has no original slide to fall back on). Queues just the new segment for
    synthesis, same as a text edit."""
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    short = _find_short(job, short_id)
    if not short:
        return jsonify({"error": "short not found"}), 404
    err = _check_short_editable(job, short)
    if err:
        return err

    text = (request.form.get("script") or "").strip()
    upload = request.files.get("image")
    if not text:
        return jsonify({"error": "script is required"}), 400
    if not upload:
        return jsonify({"error": "an 'image' file is required"}), 400
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify({"error": f"unsupported image type {ext!r} - use png, jpg, or webp"}), 400

    try:
        position = int(request.form.get("position", len(short["script"])))
    except ValueError:
        return jsonify({"error": "position must be an integer"}), 400
    position = max(0, min(position, len(short["script"])))

    custom_dir = os.path.join(job_dir(job_id), "shorts", short_id, "custom-slides")
    os.makedirs(custom_dir, exist_ok=True)
    path = os.path.join(custom_dir, f"seg-new-{uuid.uuid4().hex[:8]}{ext}")
    upload.save(path)

    words = len(text.split())
    new_segment = {
        "sequenceIndex": None, "slideId": None, "script": text,
        "estimatedWords": words, "estimatedSeconds": round((words / pipeline.WORDS_PER_MINUTE) * 60),
        "customImagePath": path,
    }
    ordered = sorted(short["script"], key=lambda n: n["sequenceIndex"])
    ordered.insert(position, new_segment)
    pipeline.reindex_short(short, ordered)

    short["pendingSegments"] = [new_segment["sequenceIndex"]]
    short["phase"] = "editing"
    short["step"] = "resynthesizing_audio"
    job["activeShortId"] = short_id
    job["phase"] = "editing"
    job["step"] = "resynthesizing_audio"
    db.save_job(job)

    return jsonify({"jobId": job_id, "shortId": short_id, "phase": job["phase"], "step": job["step"]})


@app.get("/prompts")
def list_prompts():
    return jsonify(db.list_prompts())


@app.post("/prompts")
def create_prompt():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    text = body.get("text") or ""
    if not name or not text:
        return jsonify({"error": "name and text are required"}), 400
    try:
        prompt = db.create_prompt(name, text)
    except pymysql.err.IntegrityError:
        return jsonify({"error": f"a prompt named {name!r} already exists"}), 409
    return jsonify(prompt), 201


@app.put("/prompts/<prompt_id>")
def update_prompt(prompt_id):
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    text = body.get("text") or ""
    if not name or not text:
        return jsonify({"error": "name and text are required"}), 400
    try:
        db.update_prompt(prompt_id, name, text)
    except pymysql.err.IntegrityError:
        return jsonify({"error": f"a prompt named {name!r} already exists"}), 409
    return jsonify({"id": prompt_id, "name": name, "text": text})


@app.get("/files")
def serve_file():
    requested = request.args.get("path", "")
    jobs_root = os.path.realpath(os.path.join(config.DATA_DIR, "jobs"))
    resolved = os.path.realpath(requested)
    if not (resolved == jobs_root or resolved.startswith(jobs_root + os.sep)):
        abort(403)
    if not os.path.isfile(resolved):
        abort(404)
    # conditional=True gives Flask/Werkzeug's built-in Range + HEAD support
    # for free - hand-rolling this in the n8n version was a real, learned
    # requirement (browsers HEAD-probe video before a ranged GET).
    return send_file(resolved, conditional=True)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
