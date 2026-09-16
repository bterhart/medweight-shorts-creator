"""Flask app exposing the exact same four endpoints the UI already talks to
(POST /jobs, GET /jobs/<id>/status, POST /jobs/<id>/render, GET /files) - so
ui/ needs zero changes after this replaces the n8n workflows."""
import json
import os
from datetime import datetime, timezone

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

    voice = params["voice"]
    job = {
        "jobId": job_id, "createdAt": now, "updatedAt": now,
        "phase": "prepare", "step": "saving_inputs", "error": None,
        "params": {
            "targetDurationSeconds": params["targetDurationSeconds"],
            "narrationStyle": params.get("narrationStyle", ""),
            "voice": {
                "mode": voice["mode"],
                "presetVoiceId": voice.get("presetVoiceId"),
                "customDescription": voice.get("customDescription"),
                "resolvedVoiceId": None,
            },
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
        "slides": [], "alignment": [], "narration": [], "audio": [],
        "render": {"renderedAt": None, "outputPath": None, "outputUrl": None, "renderCount": 0},
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


@app.get("/jobs/<job_id>/status")
def job_status(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.post("/jobs/<job_id>/render")
def trigger_render(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["phase"] not in ("ready_for_render", "done"):
        return jsonify({"error": f"job is not ready for render (phase={job['phase']})"}), 400

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

    job.setdefault("render", {})
    job["render"]["pendingOverrides"] = overrides
    job["phase"] = "rendering"
    job["step"] = "rendering"
    db.save_job(job)

    return jsonify({"jobId": job_id, "phase": job["phase"], "step": job["step"]})


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
