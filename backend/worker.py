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

import config
import db
import fargate_client
import pipeline
from manus_client import ManusWaiting, ManusTaskError


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
    """Dispatches a Fargate render task on the first tick a job enters phase
    'rendering', then polls that task's status on every later tick until it
    stops (or times out) - never blocks the cron worker on the render
    itself, unlike the old in-process version."""
    render_state = job.setdefault("render", {})
    task_arn = render_state.get("fargateTaskArn")

    if not task_arn:
        try:
            overrides = render_state.get("pendingOverrides") or {}
            render_count = render_state.get("renderCount", 0) + 1
            task_arn = fargate_client.dispatch_render(job, render_count)
            render_state["fargateTaskArn"] = task_arn
            render_state["fargatePendingRenderCount"] = render_count
            render_state["fargateAppliedOverrides"] = overrides
            render_state["fargateDispatchedAt"] = now_iso()
            db.save_job(job)
        except Exception as e:
            fail_job(job, "rendering", f"{type(e).__name__}: {e}", traceback.format_exc())
        return

    try:
        status = fargate_client.check_task(task_arn)
    except Exception as e:
        fail_job(job, "rendering", f"{type(e).__name__}: {e}", traceback.format_exc())
        return

    if status["state"] == "running":
        dispatched_at = datetime.fromisoformat(render_state["fargateDispatchedAt"])
        elapsed = (datetime.now(timezone.utc) - dispatched_at).total_seconds()
        if elapsed > config.RENDER_TIMEOUT_MINUTES * 60:
            fail_job(
                job, "rendering", "Fargate render task timed out",
                f"task {task_arn} still running after {config.RENDER_TIMEOUT_MINUTES} minutes",
            )
        return

    if status["state"] == "succeeded":
        render_count = render_state.pop("fargatePendingRenderCount")
        overrides = render_state.pop("fargateAppliedOverrides", {})
        output_url = fargate_client.presigned_output_url(job["jobId"], render_count)
        now = now_iso()
        history_entry = {
            "renderCount": render_count,
            "renderedAt": now,
            "outputUrl": output_url,
            "transition": {**job["params"]["transition"], **overrides},
            "resolution": overrides.get("resolution") or job["params"].get("resolution", "640x360"),
        }
        render_state.setdefault("history", []).append(history_entry)
        render_state["renderedAt"] = now
        render_state["outputUrl"] = output_url
        render_state["renderCount"] = render_count
        render_state.pop("pendingOverrides", None)
        render_state.pop("fargateTaskArn", None)
        render_state.pop("fargateDispatchedAt", None)
        job["phase"] = "done"
        job["step"] = "done"
        db.save_job(job)
        return

    fail_job(job, "rendering", "Fargate render task failed", status.get("detail", ""))


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
