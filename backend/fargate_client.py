"""Fargate-based rendering and recording ingest: offloads video encoding
(and, for recording-mode jobs, decoding a Zoom recording into slides -
render/ingest.py) off the CPU-throttled cPanel host onto a short-lived ECS
Fargate task. worker.py dispatches a task (uploading the job's assets to S3
first) then polls for completion on later cron ticks - see run_render() and
run_recording_prepare() in worker.py.

AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are read directly by boto3 from the
environment (set via .env like everything else in config.py) - not
referenced by name here."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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


_s3_upload_client = None


def _s3_for_browser_uploads():
    """The client that signs the browser's part uploads. With
    RENDER_S3_ACCELERATE on, its URLs point at the bucket's Transfer
    Acceleration endpoint (the nearest AWS edge, then Amazon's network to
    the bucket's region) - what makes a far-away, high-latency upload fast.
    The bucket must have acceleration enabled (docs/deployment.md)."""
    global _s3_upload_client
    if _s3_upload_client is None:
        _s3_upload_client = boto3.client(
            "s3", region_name=config.AWS_REGION,
            config=Config(s3={"use_accelerate_endpoint": bool(config.RENDER_S3_ACCELERATE)}),
        )
    return _s3_upload_client


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


def _ingest_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/ingest"


def _run_task(environment: dict) -> str:
    """Launches the render image once with these container environment
    variables (MODE picks what render/ecs_task.py does) and returns the
    task ARN."""
    resp = _ecs().run_task(
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
        overrides={"containerOverrides": [{
            "name": "render",
            "environment": [{"name": k, "value": str(v)} for k, v in environment.items()],
        }]},
    )
    if resp.get("failures"):
        f = resp["failures"][0]
        raise RuntimeError(f"ECS run_task failed: {f.get('reason')}: {f.get('detail', '')}")
    return resp["tasks"][0]["taskArn"]


# ------------------------------------------------------ recording ingest ----

UPLOAD_URL_EXPIRES_SECONDS = 6 * 3600  # a slow link may need hours for one recording
UPLOAD_PART_BYTES = 16 * 1024 * 1024   # S3's minimum is 5 MB (except the last part); 16 MB keeps a retry cheap
UPLOAD_MAX_BYTES = 5 * 1024 ** 3        # S3's single-object limit; a 2.5 h 1080p Zoom recording is ~0.5 GB
RECORDING_CONTENT_TYPE = "video/mp4"


def start_recording_upload(filename: str, size_bytes: int) -> dict:
    """Opens an S3 multipart upload for the browser to send the recording
    with - directly to S3, never through the cPanel host - and signs one
    URL per UPLOAD_PART_BYTES part. Parts go up in parallel and a failed
    part is retried alone, which is what makes a hundreds-of-MB upload
    over a long, lossy link both fast and survivable (one PUT of the whole
    file crawls on one TCP stream and starts over on any drop). The key
    sits under uploads/<id>/ because the job that will own it does not
    exist yet; the ingest task deletes the object once it has what it
    needs. Needs the bucket's CORS to allow PUT from the UI's origin and to
    expose ETag (docs/deployment.md)."""
    if not 0 < size_bytes <= UPLOAD_MAX_BYTES:
        raise ValueError(f"recording size must be between 1 byte and {UPLOAD_MAX_BYTES} bytes")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(filename or "recording.mp4")).strip("-") or "recording.mp4"
    key = f"uploads/{uuid.uuid4()}/{safe}"
    s3 = _s3_for_browser_uploads()
    upload_id = s3.create_multipart_upload(Bucket=config.RENDER_S3_BUCKET, Key=key, ContentType=RECORDING_CONTENT_TYPE)["UploadId"]
    part_count = -(-size_bytes // UPLOAD_PART_BYTES)
    urls = [
        s3.generate_presigned_url(
            "upload_part",
            Params={"Bucket": config.RENDER_S3_BUCKET, "Key": key, "UploadId": upload_id, "PartNumber": n},
            ExpiresIn=UPLOAD_URL_EXPIRES_SECONDS,
        )
        for n in range(1, part_count + 1)
    ]
    return {"key": key, "uploadId": upload_id, "partSize": UPLOAD_PART_BYTES, "urls": urls,
            "accelerated": bool(config.RENDER_S3_ACCELERATE), "expiresIn": UPLOAD_URL_EXPIRES_SECONDS}


def complete_recording_upload(key: str, upload_id: str, parts: list) -> int:
    """Assembles the parts ([{partNumber, eTag}] from the browser) into the
    object and returns its size in bytes."""
    s3 = _s3_for_browser_uploads()
    s3.complete_multipart_upload(
        Bucket=config.RENDER_S3_BUCKET, Key=key, UploadId=upload_id,
        MultipartUpload={"Parts": [{"ETag": p["eTag"], "PartNumber": int(p["partNumber"])}
                                   for p in sorted(parts, key=lambda p: int(p["partNumber"]))]},
    )
    return int(s3.head_object(Bucket=config.RENDER_S3_BUCKET, Key=key)["ContentLength"])


def abort_recording_upload(key: str, upload_id: str) -> None:
    """Drops an upload the browser gave up on, so its parts stop costing
    storage. Best effort: the worker user's policy may lack
    s3:AbortMultipartUpload, in which case the bucket's lifecycle rule (or
    a hand clean-up) is what removes the parts."""
    try:
        _s3_for_browser_uploads().abort_multipart_upload(Bucket=config.RENDER_S3_BUCKET, Key=key, UploadId=upload_id)
    except ClientError as e:
        print(f"warning: could not abort multipart upload {upload_id} for {key}: {e}")


def uploaded_object_size(key: str) -> int | None:
    """Bytes at this key, or None if there is no such object - checked at
    job creation so a job is never queued for a recording that never
    arrived."""
    try:
        return int(_s3().head_object(Bucket=config.RENDER_S3_BUCKET, Key=key)["ContentLength"])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def dispatch_ingest(job: dict) -> str:
    """Starts the ingest task for a recording-mode job: it reads the
    uploaded recording, writes slides/audio/timeline under the job's ingest
    prefix and deletes the upload. Returns the task ARN."""
    job_id = job["jobId"]
    return _run_task({
        "MODE": "ingest",
        "JOB_ID": job_id,
        "S3_BUCKET": config.RENDER_S3_BUCKET,
        "SOURCE_KEY": job["sources"]["recording"]["s3Key"],
        "S3_PREFIX": _ingest_prefix(job_id),
    })


def fetch_ingest_outputs(job: dict, local_dir: str) -> dict:
    """Downloads everything the ingest task wrote (ingest.json, slide
    frames + previews, audio parts) into local_dir and returns ingest.json
    with its relative paths made absolute under local_dir."""
    s3 = _s3()
    prefix = _ingest_prefix(job["jobId"])
    local = Path(local_dir)
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=config.RENDER_S3_BUCKET, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):].lstrip("/")
            if not rel:
                continue
            dest = local / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(config.RENDER_S3_BUCKET, obj["Key"], str(dest))
            count += 1
    manifest = local / "ingest.json"
    if not manifest.is_file():
        raise RuntimeError(f"the ingest task left no ingest.json under s3://{config.RENDER_S3_BUCKET}/{prefix}/ ({count} objects)")
    result = json.loads(manifest.read_text())
    for seg in result.get("segments", []):
        for k in ("framePath", "previewPath"):
            if seg.get(k):
                seg[k] = str(local / seg[k])
    for part in result.get("audio", []):
        part["path"] = str(local / part["path"])
    return result


def dispatch_render(job: dict, short: dict, render_count: int) -> str:
    """Uploads a render view of this short (job.json shaped like the old
    job-level render input - slides/narration/audio/params/pendingOverrides -
    so render/render.py and render/ecs_task.py read it unchanged) plus the
    files it references to S3, launches the Fargate render task, and returns
    its task ARN. Each short renders under its own S3 prefix so multiple
    shorts on the same job never collide.

    Only the slides this short's script actually references are included
    and uploaded - a 90s short uses a handful of a 70-slide deck - and only
    the full-resolution image of each: the render never reads a slide's
    extracted text, preview rendition, or image audit."""
    job_id, short_id = job["jobId"], short["shortId"]
    prefix = _prefix(job_id, short_id)
    s3 = _s3()

    used_slide_ids = {n["slideId"] for n in short["script"] if n.get("slideId")}
    used_slides = [
        {k: v for k, v in slide.items() if k in ("slideId", "pdfId", "pageNumber", "imagePath", "video")}
        for slide in job.get("slides", []) if slide["slideId"] in used_slide_ids
    ]

    render_view = {
        "jobId": job_id,
        "name": short.get("name") or short.get("topic"),
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
        video_path = (slide.get("video") or {}).get("path")
        if video_path and os.path.isfile(video_path):
            s3.upload_file(video_path, config.RENDER_S3_BUCKET, f"{prefix}/media/{os.path.basename(video_path)}")

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
        video_path = n.get("customVideoPath")
        if video_path and os.path.isfile(video_path):
            s3.upload_file(video_path, config.RENDER_S3_BUCKET, f"{prefix}/custom-media/{os.path.basename(video_path)}")

    overrides = short.get("render", {}).get("pendingOverrides") or {}
    if overrides.get("includeIntro"):
        _upload_shared_asset(s3, "intro.mp4", INTRO_ASSET_KEY)
    if overrides.get("includeOutro"):
        _upload_shared_asset(s3, "outro.mp4", OUTRO_ASSET_KEY)

    return _run_task({
        "MODE": "render",
        "JOB_ID": job_id,
        "S3_BUCKET": config.RENDER_S3_BUCKET,
        "S3_PREFIX": prefix,
        "RENDER_COUNT": render_count,
    })


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
