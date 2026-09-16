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


def _s3():
    return boto3.client("s3", region_name=config.AWS_REGION)


def _ecs():
    return boto3.client("ecs", region_name=config.AWS_REGION)


def _prefix(job_id: str, short_id: str) -> str:
    return f"jobs/{job_id}/shorts/{short_id}"


def dispatch_render(job: dict, short: dict, render_count: int) -> str:
    """Uploads a render view of this short (job.json shaped exactly like the
    old job-level render input - slides/narration/audio/params/pendingOverrides
    - so render/render.py and render/ecs_task.py need no changes at all) plus
    its slide/audio files to S3, launches the Fargate render task, and
    returns its task ARN. Each short renders under its own S3 prefix so
    multiple shorts on the same job never collide."""
    job_id, short_id = job["jobId"], short["shortId"]
    prefix = _prefix(job_id, short_id)
    s3 = _s3()

    render_view = {
        "jobId": job_id,
        "slides": job.get("slides", []),
        "narration": short["script"],
        "audio": short["audio"],
        "params": job["params"],
        "render": {"pendingOverrides": short.get("render", {}).get("pendingOverrides", {})},
    }
    s3.put_object(Bucket=config.RENDER_S3_BUCKET, Key=f"{prefix}/job.json", Body=json.dumps(render_view))

    for slide in job.get("slides", []):
        for field in ("imagePath", "textPath"):
            path = slide.get(field)
            if path and os.path.isfile(path):
                s3.upload_file(path, config.RENDER_S3_BUCKET, f"{prefix}/slides/{os.path.basename(path)}")

    for a in short.get("audio", []):
        path = a.get("path")
        if path and os.path.isfile(path):
            s3.upload_file(path, config.RENDER_S3_BUCKET, f"{prefix}/audio/{os.path.basename(path)}")

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


def presigned_output_url(job_id: str, short_id: str, render_count: int, expires_in: int = 3600) -> str:
    s3 = _s3()
    key = f"{_prefix(job_id, short_id)}/output/video-{render_count:02d}.mp4"
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": config.RENDER_S3_BUCKET, "Key": key}, ExpiresIn=expires_in
    )
