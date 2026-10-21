#!/usr/bin/env python3
"""Cron-invoked worker: claims at most one job needing work, processes it to
completion (or failure) in this single invocation, then exits. A file lock
caps this to one worker process at a time regardless of cron's own overlap
behavior - simpler and safer than per-job-only locking on shared hosting.

Intended crontab entry (every minute):
  * * * * * /path/to/python3 /path/to/backend/worker.py >> /path/to/worker.log 2>&1
"""
from __future__ import annotations

import fcntl
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alignment
import config
import db
import fargate_client
import pipeline
import recording


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def timed(label: str):
    """Prints a step's wall time to worker.log, so a slow run shows where the
    minutes actually went instead of that being guessed at afterward."""
    start = time.monotonic()
    try:
        yield
    finally:
        print(f"  {label}: {time.monotonic() - start:.1f}s")


def fail_job(job: dict, step: str, message: str, detail: str = "") -> None:
    job["phase"] = "failed"
    job["error"] = {"step": step, "message": message, "detail": detail}
    db.save_job(job)


def fail_short(job: dict, short: dict, step: str, message: str, detail: str = "") -> None:
    """A short failing doesn't fail the job - the permanent 1:1 narration
    and every other short are untouched, so the job always returns to its
    resting ready_for_review state, free to try another short."""
    short["phase"] = "failed"
    short["step"] = step
    short["error"] = {"step": step, "message": message, "detail": detail}
    job["activeShortId"] = None
    job["phase"] = "ready_for_review"
    job["step"] = "ready_for_review"
    db.save_job(job)


def _outline(job: dict) -> None:
    """The sub-topic outline (pipeline.outline_subtopics) is the last prepare
    step in both intake modes. It is a convenience for the Create-short
    form, so its failure is a warning on the job, never a failed prepare -
    shorts still work from a typed topic."""
    job["step"] = "outlining"
    db.save_job(job)
    try:
        with timed("outline_subtopics"):
            pipeline.outline_subtopics(job)
        job.pop("subtopicsError", None)
    except Exception as e:
        job["subtopics"] = []
        job["subtopicsError"] = f"{type(e).__name__}: {e}"
        print(f"warning: outline_subtopics failed - the job is still usable: {job['subtopicsError']}")


def run_recording_prepare(job: dict) -> None:
    """Recording mode's prepare, multi-tick like rendering because the heavy
    step runs on Fargate: tick 1 dispatches the ingest task; later ticks
    poll it (step 'ingesting'); the tick that finds it finished fetches its
    outputs and runs the rest here - transcript (Zoom VTT or speech-to-
    text), trim, time-based alignment, then the shared cleaning and
    outlining - to the same ready_for_review shape as the other modes.
    Step 'realigning' re-enters after the review screen changes the trim:
    alignment and cleaning again from the stored cues, no new ingest or
    transcription. A job put back to phase 'prepare' at step
    'fetching_ingest' (by hand, after a failure past the ingest) resumes
    from the fetch: the outputs are still in S3 and nothing is re-uploaded."""
    job_dir = os.path.join(config.DATA_DIR, "jobs", job["jobId"])
    rec = job["sources"]["recording"]
    step = job.get("step")

    try:
        if step in ("saving_inputs", "queued", None):
            with timed("dispatch_ingest (ECS run_task)"):
                task_arn = fargate_client.dispatch_ingest(job)
            rec["ingest"] = {"taskArn": task_arn, "dispatchedAt": now_iso()}
            job["step"] = "ingesting"
            db.save_job(job)
            return

        if step in ("ingesting", "fetching_ingest"):
            if step == "ingesting":
                status = fargate_client.check_task(rec["ingest"]["taskArn"])
                if status["state"] == "running":
                    dispatched_at = datetime.fromisoformat(rec["ingest"]["dispatchedAt"])
                    elapsed = (datetime.now(timezone.utc) - dispatched_at).total_seconds()
                    rec["ingest"]["elapsedSeconds"] = round(elapsed)
                    db.save_job(job)
                    if elapsed > config.INGEST_TIMEOUT_MINUTES * 60:
                        fail_job(job, "ingesting", "Fargate ingest task timed out",
                                 f"task {rec['ingest']['taskArn']} still running after {config.INGEST_TIMEOUT_MINUTES} minutes")
                    return
                if status["state"] != "succeeded":
                    # ECS forgets a stopped task after about an hour; the
                    # manifest in S3 is the durable sign the ingest finished.
                    if not fargate_client.ingest_manifest_exists(job):
                        fail_job(job, "ingesting", "Fargate ingest task failed", status.get("detail", ""))
                        return
                    print(f"  ingest task reported {status['state']} ({status.get('detail', '')}) but its manifest exists - continuing")
                rec["ingest"]["finishedAt"] = now_iso()
                job["step"] = "fetching_ingest"
                db.save_job(job)
            with timed("fetch_ingest_outputs"):
                result = fargate_client.fetch_ingest_outputs(job, os.path.join(job_dir, "ingest"))
            rec.update({
                "video": result["video"],
                "thumbnailBox": result["thumbnailBox"],
                "thumbnailBoxSource": result.get("thumbnailBoxSource"),
                "timeline": result["segments"],
                "audio": result["audio"],
                "audioStartSeconds": result["audioStartSeconds"],
                "sourceDeleted": bool(result.get("sourceDeleted")),
                "detectionThresholds": result.get("thresholds"),
            })
            job["slides"] = recording.slides_from_timeline(job)
            db.save_job(job)

            job["step"] = "transcribing"
            db.save_job(job)
            with timed("recording_cues"):
                cues = recording.recording_cues(job)
            db.save_job(job)

            job["step"] = "trimming"
            db.save_job(job)
            with timed("trim_recording"):
                recording.trim_recording(job, cues)
            db.save_job(job)
        elif step == "realigning":
            cues = pipeline.parse_srt_cues(job["sources"]["srt"]["path"])
        else:
            raise RuntimeError(f"recording prepare does not know step {step!r}")

        job["step"] = "aligning"
        db.save_job(job)
        with timed("align_recording"):
            recording.align_recording(job, cues)
        db.save_job(job)

        job["step"] = "cleaning_narration"
        db.save_job(job)
        with timed(f"clean_narration ({len(job['alignment'])} segments)"):
            pipeline.clean_narration(job)
        db.save_job(job)
        _outline(job)
        job["phase"] = "ready_for_review"
        job["step"] = "ready_for_review"
        db.save_job(job)
    except Exception as e:
        fail_job(job, job.get("step", "unknown"), f"{type(e).__name__}: {e}", traceback.format_exc())


def run_prepare_pipeline(job: dict) -> None:
    """Three intake modes. 'transcript' (the original): parse the SRT,
    extract the PDF's slides, align with Claude, clean the excerpts.
    'notes': the PDF is the export of a PPTX whose speaker notes are the
    narration - no transcript, so nothing to align or clean; the notes are
    paired to pages by position and written verbatim. 'recording': a Zoom
    recording, handled by run_recording_prepare. All end at the same
    ready_for_review shape."""
    if job.get("inputMode") == "recording":
        run_recording_prepare(job)
        return
    job_dir = os.path.join(config.DATA_DIR, "jobs", job["jobId"])
    notes_mode = job.get("inputMode") == "notes"

    try:
        if not notes_mode:
            job["step"] = "parsing_srt"
            with timed("parse_srt"):
                srt_info = pipeline.parse_srt(job["sources"]["srt"]["path"])
            job["sources"]["srt"].update(srt_info)
            db.save_job(job)

        job["step"] = "extracting_pdfs"
        db.save_job(job)
        slides_dir = os.path.join(job_dir, "slides")
        slides = []
        with timed("extract_pdf_slides"):
            for pdf in job["sources"]["pdfs"]:
                slides.extend(pipeline.extract_pdf_slides(pdf["path"], pdf["id"], slides_dir))
        job["slides"] = slides
        db.save_job(job)

        if notes_mode:
            job["step"] = "extracting_notes"
            db.save_job(job)
            with timed("extract_pptx_notes"):
                notes = pipeline.extract_pptx_notes(job["sources"]["pptx"]["path"], os.path.join(job_dir, "media"))
                pipeline.narration_from_notes(job, notes)
            db.save_job(job)
            _outline(job)
            job["phase"] = "ready_for_review"
            job["step"] = "ready_for_review"
            db.save_job(job)
            return

        job["step"] = "aligning"
        db.save_job(job)
        with timed(f"align_job ({len(slides)} slides)"):
            alignment.align_job(job)
        db.save_job(job)

        job["step"] = "cleaning_narration"
        db.save_job(job)
        with timed(f"clean_narration ({len(job['alignment'])} segments)"):
            pipeline.clean_narration(job)
        db.save_job(job)
        _outline(job)

        # Duration-based condensing, voice resolution, and audio synthesis
        # are deferred to a later step that doesn't exist yet (per explicit
        # direction) - this phase now stops at the human-reviewable
        # slide+cleaned-text output, same shape as before, just not
        # duration-boxed and with no audio yet.
        job["phase"] = "ready_for_review"
        job["step"] = "ready_for_review"
        db.save_job(job)

    except Exception as e:
        fail_job(job, job.get("step", "unknown"), f"{type(e).__name__}: {e}", traceback.format_exc())


def _active_short(job: dict) -> dict | None:
    active_id = job.get("activeShortId")
    return next((s for s in job.get("shorts", []) if s["shortId"] == active_id), None)


def run_condense_pipeline(job: dict) -> None:
    """Builds the short named by job["activeShortId"] (see
    pipeline.build_short), resolves its voice, and synthesizes its audio -
    triggered explicitly via POST /jobs/<id>/shorts. Never touches
    job["narration"], the permanent 1:1 pool every short is built from."""
    short = _active_short(job)
    if short is None:
        fail_job(job, "condensing_narration", "activeShortId does not match any short in job.shorts")
        return

    job_dir = os.path.join(config.DATA_DIR, "jobs", job["jobId"])
    try:
        short["step"] = "condensing_narration"
        db.save_job(job)
        with timed("build_short"):
            pipeline.build_short(job, short)
        db.save_job(job)

        short["step"] = "resolving_voice"
        db.save_job(job)
        with timed("resolve_voice"):
            pipeline.resolve_voice(short)
        db.save_job(job)

        short["step"] = "synthesizing_audio"
        db.save_job(job)
        audio_dir = os.path.join(job_dir, "audio", short["shortId"])
        with timed(f"synthesize_audio ({len(short['script'])} segments)"):
            pipeline.synthesize_audio(short, audio_dir)

        short["phase"] = "ready_for_render"
        short["step"] = "ready_for_render"
        job["activeShortId"] = None
        job["phase"] = "ready_for_review"
        job["step"] = "ready_for_review"
        db.save_job(job)

    except Exception as e:
        fail_short(job, short, short.get("step", "unknown"), f"{type(e).__name__}: {e}", traceback.format_exc())


def run_edit_pipeline(job: dict) -> None:
    """(Re)synthesizes audio for whichever segments short["pendingSegments"]
    names - a post-review text edit or a newly added segment - triggered
    explicitly via PATCH/POST on a segment. Never touches any other
    segment's audio, script text, or image. The job always returns to
    ready_for_review once this finishes or fails, same as condensing."""
    short = _active_short(job)
    if short is None:
        fail_job(job, "resynthesizing_audio", "activeShortId does not match any short in job.shorts")
        return

    job_dir = os.path.join(config.DATA_DIR, "jobs", job["jobId"])
    try:
        short["step"] = "resynthesizing_audio"
        db.save_job(job)
        audio_dir = os.path.join(job_dir, "audio", short["shortId"])
        pending = set(short.pop("pendingSegments", []) or [])
        with timed(f"resynthesize_audio ({len(pending)} segment(s))"):
            pipeline.synthesize_audio(short, audio_dir, sequence_indexes=pending)

        short["phase"] = "ready_for_render"
        short["step"] = "ready_for_render"
        job["activeShortId"] = None
        job["phase"] = "ready_for_review"
        job["step"] = "ready_for_review"
        db.save_job(job)

    except Exception as e:
        fail_short(job, short, short.get("step", "unknown"), f"{type(e).__name__}: {e}", traceback.format_exc())


def run_render(job: dict, short: dict) -> None:
    """Dispatches a Fargate render task on the first tick this short enters
    phase 'rendering', then polls that task's status on every later tick
    until it stops (or times out) - never blocks the cron worker on the
    render itself. Scoped entirely to one short; every other short on the
    job is untouched."""
    render_state = short.setdefault("render", {})
    task_arn = render_state.get("fargateTaskArn")

    if not task_arn:
        try:
            # A re-render replaces the previous one: the trigger endpoint
            # already cleared outputUrl (so the UI stopped showing it) and
            # left the old render's number here for us to delete from S3.
            stale_count = render_state.pop("staleRenderCount", None)
            if stale_count is not None:
                try:
                    fargate_client.delete_output(job["jobId"], short["shortId"], stale_count)
                    render_state["history"] = [
                        h for h in render_state.get("history", []) if h.get("renderCount") != stale_count
                    ]
                    render_state.pop("staleDeleteError", None)
                except Exception as e:
                    # Cleanup of a superseded render must never block the new
                    # one - the stale preview is already hidden (outputUrl was
                    # cleared at trigger time), and a failed short is terminal.
                    # Most likely cause: the IAM policy lacks s3:DeleteObject.
                    render_state["staleDeleteError"] = f"video-{stale_count:02d}: {type(e).__name__}: {e}"
                    print(f"warning: could not delete superseded render for short {short['shortId']}: "
                          f"{render_state['staleDeleteError']}")
            overrides = render_state.get("pendingOverrides") or {}
            render_count = render_state.get("renderCount", 0) + 1
            with timed("dispatch_render (asset upload + ECS run_task)"):
                task_arn = fargate_client.dispatch_render(job, short, render_count)
            render_state["fargateTaskArn"] = task_arn
            render_state["fargatePendingRenderCount"] = render_count
            render_state["fargateAppliedOverrides"] = overrides
            render_state["fargateDispatchedAt"] = now_iso()
            db.save_job(job)
        except Exception as e:
            fail_short(job, short, "rendering", f"{type(e).__name__}: {e}", traceback.format_exc())
        return

    try:
        status = fargate_client.check_task(task_arn)
    except Exception as e:
        fail_short(job, short, "rendering", f"{type(e).__name__}: {e}", traceback.format_exc())
        return

    if status["state"] == "running":
        dispatched_at = datetime.fromisoformat(render_state["fargateDispatchedAt"])
        elapsed = (datetime.now(timezone.utc) - dispatched_at).total_seconds()
        if elapsed > config.RENDER_TIMEOUT_MINUTES * 60:
            fail_short(
                job, short, "rendering", "Fargate render task timed out",
                f"task {task_arn} still running after {config.RENDER_TIMEOUT_MINUTES} minutes",
            )
        return

    if status["state"] == "succeeded":
        render_count = render_state.pop("fargatePendingRenderCount")
        overrides = render_state.pop("fargateAppliedOverrides", {})
        dispatched_at = datetime.fromisoformat(render_state["fargateDispatchedAt"])
        print(f"  render: {(datetime.now(timezone.utc) - dispatched_at).total_seconds():.1f}s from dispatch to completion")
        output_url = fargate_client.presigned_output_url(job["jobId"], short["shortId"], render_count)
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
        short["phase"] = "done"
        short["step"] = "done"
        job["activeShortId"] = None
        job["phase"] = "ready_for_review"
        job["step"] = "ready_for_review"
        db.save_job(job)
        return

    fail_short(job, short, "rendering", "Fargate render task failed", status.get("detail", ""))


def main():
    # stdout is a file under cron, so Python would otherwise buffer every
    # print until exit - and a prepare run lasts minutes. Line-buffer it so
    # the per-step timings above appear in worker.log as they happen.
    sys.stdout.reconfigure(line_buffering=True)
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
        elif job["phase"] == "condensing":
            run_condense_pipeline(job)
        elif job["phase"] == "editing":
            run_edit_pipeline(job)
        elif job["phase"] == "rendering":
            short = _active_short(job)
            if short is None:
                fail_job(job, "rendering", "activeShortId does not match any short in job.shorts")
            else:
                run_render(job, short)
    finally:
        db.release_job(job["jobId"])
    print(f"finished job {job['jobId']} (phase={job['phase']})")


if __name__ == "__main__":
    main()
