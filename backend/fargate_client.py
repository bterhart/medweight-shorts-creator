"""Fargate-based rendering: offloads video encoding off the CPU-throttled
cPanel host onto a short-lived ECS Fargate task. worker.py dispatches a task
(uploading the job's assets to S3 first) then polls for completion on later
cron ticks - see run_render() in worker.py.

AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are read directly by boto3 from the
environment (set via .env like everything else in config.py) - not
referenced by name here."""
from __future__ import annotations

import json
import os

import boto3

import config

# Fixed, non-job-specific keys - render/ecs_task.py downloads from these same
# keys regardless of which job/short is rendering.
INTRO_ASSET_KEY = "assets/intro.mp4"
OUTRO_ASSET_KEY = "assets/outro.mp4"


_s3_client = None


def _s3():
    """One client per process: Flask calls this on every status poll (to
    re-sign preview URLs), and building a boto3 client is the expensive
    part of that, not the signing itself."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=config.AWS_REGION)
    return _s3_client


def _upload_shared_asset(s3, filename: str, key: str) -> None:
    """intro.mp4/outro.mp4 are fixed clips uploaded once by hand to
    DATA_DIR/assets/ on the cPanel host - re-uploaded to S3 on every render
    that requests them, since the Fargate task has no other access to the
    cPanel host's disk."""
    path = os.path.join(config.DATA_DIR, "assets", filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{filename} was requested but is not present at {path}")
    s3.upload_file(path, config.RENDER_S3_BUCKET, key)


def _ecs():
    return boto3.client("ecs", region_name=config.AWS_REGION)


def _prefix(job_id: str, short_id: str) -> str:
    return f"jobs/{job_id}/shorts/{short_id}"


def dispatch_render(job: dict, short: dict, render_count: int) -> str:
    """Uploads a render view of this short (job.json shaped like the old
    job-level render input - slides/narration/audio/params/pendingOverrides -
    so render/render.py and render/ecs_task.py read it unchanged) plus the
    files it references to S3, launches the Fargate render task, and returns
    its task ARN. Each short renders under its own S3 prefix so multiple
    shorts on the same job never collide.

    Only the slides this short's script actually references are included
    and uploaded - a 90s short uses a handful of a 70-slide deck, and the
    render never reads a slide's extracted text, so neither the unused
    images nor any .txt files are sent."""
    job_id, short_id = job["jobId"], short["shortId"]
    prefix = _prefix(job_id, short_id)
    s3 = _s3()

    used_slide_ids = {n["slideId"] for n in short["script"] if n.get("slideId")}
    used_slides = [
        {k: v for k, v in slide.items() if k != "textPath"}
        for slide in job.get("slides", []) if slide["slideId"] in used_slide_ids
    ]

    render_view = {
        "jobId": job_id,
        "slides": used_slides,
        "narration": short["script"],
        "audio": short["audio"],
        "params": job["params"],
        "render": {"pendingOverrides": short.get("render", {}).get("pendingOverrides", {})},
    }
    s3.put_object(Bucket=config.RENDER_S3_BUCKET, Key=f"{prefix}/job.json", Body=json.dumps(render_view))

    for slide in used_slides:
        path = slide.get("imagePath")
        if path and os.path.isfile(path):
            s3.upload_file(path, config.RENDER_S3_BUCKET, f"{prefix}/slides/{os.path.basename(path)}")

    for a in short.get("audio", []):
        path = a.get("path")
        if path and os.path.isfile(path):
            s3.upload_file(path, config.RENDER_S3_BUCKET, f"{prefix}/audio/{os.path.basename(path)}")

    # A segment whose image was replaced or added post-review points at a
    # file under this short's own custom-slides/ dir, never one of
    # job["slides"]'s own images - upload those too, under their own prefix.
    for n in short.get("script", []):
        path = n.get("customImagePath")
        if path and os.path.isfile(path):
            s3.upload_file(path, config.RENDER_S3_BUCKET, f"{prefix}/custom-slides/{os.path.basename(path)}")

    overrides = short.get("render", {}).get("pendingOverrides") or {}
    if overrides.get("includeIntro"):
        _upload_shared_asset(s3, "intro.mp4", INTRO_ASSET_KEY)
    if overrides.get("includeOutro"):
        _upload_shared_asset(s3, "outro.mp4", OUTRO_ASSET_KEY)

    ecs = _ecs()
    resp = ecs.run_task(
        cluster=config.RENDER_ECS_CLUSTER,
        taskDefinition=config.RENDER_ECS_TASK_DEFINITION,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": config.RENDER_ECS_SUBNETS,
                "securityGroups": config.RENDER_ECS_SECURITY_GROUPS,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [{
                "name": "render",
                "environment": [
                    {"name": "JOB_ID", "value": job_id},
                    {"name": "S3_BUCKET", "value": config.RENDER_S3_BUCKET},
                    {"name": "S3_PREFIX", "value": prefix},
                    {"name": "RENDER_COUNT", "value": str(render_count)},
                ],
            }]
        },
    )
    if resp.get("failures"):
        f = resp["failures"][0]
        raise RuntimeError(f"ECS run_task failed: {f.get('reason')}: {f.get('detail', '')}")
    return resp["tasks"][0]["taskArn"]


def check_task(task_arn: str) -> dict:
    """Returns {"state": "running"|"succeeded"|"failed", "detail": str}."""
    ecs = _ecs()
    resp = ecs.describe_tasks(cluster=config.RENDER_ECS_CLUSTER, tasks=[task_arn])
    if resp.get("failures"):
        f = resp["failures"][0]
        return {"state": "failed", "detail": f"ECS describe_tasks failure: {f.get('reason')}"}

    task = resp["tasks"][0]
    if task["lastStatus"] != "STOPPED":
        return {"state": "running", "detail": ""}

    container = task["containers"][0]
    exit_code = container.get("exitCode")
    if exit_code == 0:
        return {"state": "succeeded", "detail": ""}
    detail = (
        f"exitCode={exit_code} stopCode={task.get('stopCode')} "
        f"stoppedReason={task.get('stoppedReason')} containerReason={container.get('reason')}"
    )
    return {"state": "failed", "detail": detail}


def _output_key(job_id: str, short_id: str, render_count: int) -> str:
    return f"{_prefix(job_id, short_id)}/output/video-{render_count:02d}.mp4"


def presigned_output_url(job_id: str, short_id: str, render_count: int, expires_in: int = 3600) -> str:
    s3 = _s3()
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.RENDER_S3_BUCKET, "Key": _output_key(job_id, short_id, render_count)},
        ExpiresIn=expires_in,
    )


def refresh_output_urls(job: dict) -> dict:
    """Re-signs every finished render's outputUrl on the job in place and
    returns it. The URL the worker stores at render completion expires an
    hour later, after which the UI's preview and download link silently
    fail; the status endpoint calls this on every read so what the UI gets
    is always freshly signed. Signing is local (no AWS round trip). If it
    fails - most likely the API process lacks the AWS keys - the stored URLs
    are left as they are rather than breaking status polling."""
    job_id = job["jobId"]
    for short in job.get("shorts", []):
        render_state = short.get("render") or {}
        try:
            if render_state.get("outputUrl"):
                render_state["outputUrl"] = presigned_output_url(job_id, short["shortId"], render_state["renderCount"])
            for h in render_state.get("history", []):
                if h.get("outputUrl"):
                    h["outputUrl"] = presigned_output_url(job_id, short["shortId"], h["renderCount"])
        except Exception as e:
            print(f"warning: could not re-sign output URLs for short {short.get('shortId')}: {type(e).__name__}: {e}")
    return job


def delete_output(job_id: str, short_id: str, render_count: int) -> None:
    """Removes a superseded render's video from S3. delete_object is a no-op
    on a key that's already gone, so calling this twice is harmless."""
    _s3().delete_object(Bucket=config.RENDER_S3_BUCKET, Key=_output_key(job_id, short_id, render_count))
