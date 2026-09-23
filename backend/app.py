"""Flask app for the job pipeline and its shorts: POST /jobs, GET
/jobs/<id>/status, POST /jobs/<id>/shorts, POST /jobs/<id>/shorts/<shortId>/render,
GET /files, plus the narration-prompts library."""
import json
import os
import uuid
from datetime import datetime, timezone

import pymysql
from flask import Flask, jsonify, request, send_file, abort

import config
import db

app = Flask(__name__)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def job_dir(job_id: str) -> str:
    return os.path.join(config.DATA_DIR, "jobs", job_id)


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

    short_id = str(uuid.uuid4())
    short = {
        "shortId": short_id, "createdAt": now_iso(),
        "topic": topic, "targetDurationSeconds": target_duration,
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

    short.setdefault("render", {})
    short["render"]["pendingOverrides"] = overrides
    short["phase"] = "rendering"
    short["step"] = "rendering"
    job["activeShortId"] = short_id
    job["phase"] = "rendering"
    job["step"] = "rendering"
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
