#!/usr/bin/env python3
"""Cron-invoked worker: claims at most one job needing work, processes it to
completion (or failure) in this single invocation, then exits. A file lock
caps this to one worker process at a time regardless of cron's own overlap
behavior - simpler and safer than per-job-only locking on shared hosting.

Intended crontab entry (every minute):
  * * * * * /path/to/python3 /path/to/backend/worker.py >> /path/to/worker.log 2>&1
"""
import fcntl
import os
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for render/

import config
import db
import pipeline
from manus_client import ManusWaiting, ManusTaskError
from render.render import render as render_video


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fail_job(job: dict, step: str, message: str, detail: str = "") -> None:
    job["phase"] = "failed"
    job["error"] = {"step": step, "message": message, "detail": detail}
    db.save_job(job)


def run_prepare_pipeline(job: dict) -> None:
    job_dir = os.path.join(config.DATA_DIR, "jobs", job["jobId"])

    try:
        job["step"] = "parsing_srt"
        srt_info = pipeline.parse_srt(job["sources"]["srt"]["path"])
        job["sources"]["srt"].update(srt_info)
        db.save_job(job)

        job["step"] = "extracting_pdfs"
        slides_dir = os.path.join(job_dir, "slides")
        slides = []
        for pdf in job["sources"]["pdfs"]:
            slides.extend(pipeline.extract_pdf_slides(pdf["path"], pdf["id"], slides_dir))
        job["slides"] = slides
        db.save_job(job)

        job["step"] = "uploading_slides_to_manus"
        pipeline.upload_slides_to_manus(job)
        db.save_job(job)

        job["step"] = "aligning"
        db.save_job(job)
        pipeline.run_alignment(job)
        db.save_job(job)

        job["step"] = "condensing_narration"
        db.save_job(job)
        pipeline.condense_narration(job)
        db.save_job(job)

        job["step"] = "resolving_voice"
        db.save_job(job)
        pipeline.resolve_voice(job)
        db.save_job(job)

        job["step"] = "synthesizing_audio"
        db.save_job(job)
        audio_dir = os.path.join(job_dir, "audio")
        pipeline.synthesize_audio(job, audio_dir)

        job["phase"] = "ready_for_render"
        job["step"] = "ready_for_render"
        db.save_job(job)

    except ManusWaiting as e:
        fail_job(job, "aligning", str(e))
    except ManusTaskError as e:
        fail_job(job, "aligning", "Manus task failed", str(e))
    except TimeoutError as e:
        fail_job(job, "aligning", "Manus task timed out", str(e))
    except Exception as e:
        fail_job(job, job.get("step", "unknown"), f"{type(e).__name__}: {e}", traceback.format_exc())


def run_render(job: dict) -> None:
    overrides = job.get("render", {}).get("pendingOverrides") or {}
    try:
        final_video, ttype, resolution = render_video(
            job,
            transition_override={k: v for k, v in overrides.items() if k in ("type", "transitionSeconds", "minSlideSeconds")} or None,
            resolution_override=overrides.get("resolution"),
        )
        job_dir = os.path.join(config.DATA_DIR, "jobs", job["jobId"])
        out_dir = os.path.join(job_dir, "output")
        os.makedirs(out_dir, exist_ok=True)
        render_count = job.get("render", {}).get("renderCount", 0) + 1
        out_path = os.path.join(out_dir, f"video-{render_count:02d}.mp4")

        final_video.write_videofile(
            out_path, fps=30, codec="libx264", audio_codec="aac", logger=None,
            ffmpeg_params=["-movflags", "+faststart"],
        )

        history_entry = {
            "renderCount": render_count, "renderedAt": now_iso(), "outputPath": out_path,
            "transition": {**job["params"]["transition"], **overrides, "type": ttype}, "resolution": resolution,
        }
        job.setdefault("render", {})
        job["render"].setdefault("history", []).append(history_entry)
        job["render"]["renderedAt"] = history_entry["renderedAt"]
        job["render"]["outputPath"] = out_path
        job["render"]["renderCount"] = render_count
        job["render"].pop("pendingOverrides", None)
        job["phase"] = "done"
        job["step"] = "done"
        db.save_job(job)
    except Exception as e:
        fail_job(job, "rendering", f"{type(e).__name__}: {e}", traceback.format_exc())


def main():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    lock_fd = open(config.WORKER_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("worker already running, exiting")
        return

    job = db.claim_next_job()
    if not job:
        print("no job needs work")
        return

    print(f"claimed job {job['jobId']} (phase={job['phase']})")
    try:
        if job["phase"] == "prepare":
            run_prepare_pipeline(job)
        elif job["phase"] == "rendering":
            run_render(job)
    finally:
        db.release_job(job["jobId"])
    print(f"finished job {job['jobId']} (phase={job['phase']})")


if __name__ == "__main__":
    main()
