#!/usr/bin/env python3
"""ECS Fargate entrypoint, two modes picked by the MODE environment variable
(set via ECS task container overrides - this container is never invoked any
other way):
  render (default): downloads a prepared short's assets from S3, renders the
      video (same render() used for local CLI rendering), uploads the result.
  ingest: downloads an uploaded Zoom recording, runs ingest.run() on it
      (slide detection, frames, audio parts, timeline), uploads the outputs
      under the job's ingest prefix, then deletes the recording - the
      upload was only ever a hand-off, and the task role is the one with
      delete rights on the bucket.

On any failure, prints a traceback to stderr (captured by CloudWatch Logs)
and exits non-zero, so the polling side (worker.py on cPanel) can detect
failure from the task's exit code without needing a callback.
"""
import json
import os
import sys
import traceback
from pathlib import Path

import boto3

import ingest
from render import render as render_video

# Fixed, non-job-specific keys - uploaded once by backend/fargate_client.py
# from DATA_DIR/assets/ on the cPanel host, not scoped under a job's prefix.
INTRO_ASSET_KEY = "assets/intro.mp4"
OUTRO_ASSET_KEY = "assets/outro.mp4"


def s3_download_prefix(s3, bucket, prefix, local_dir):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix):].lstrip("/")
            if not rel:
                continue
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dest))


def rewrite_local_paths(job, local_dir):
    """job.json's paths are absolute paths from the cPanel host - rewrite
    them to wherever this container downloaded the same files to."""
    for slide in job.get("slides", []):
        slide["imagePath"] = str(local_dir / "slides" / Path(slide["imagePath"]).name)
        if slide.get("textPath"):
            slide["textPath"] = str(local_dir / "slides" / Path(slide["textPath"]).name)
        if slide.get("video"):
            slide["video"]["path"] = str(local_dir / "media" / Path(slide["video"]["path"]).name)
    for a in job.get("audio", []):
        if a.get("path"):
            a["path"] = str(local_dir / "audio" / Path(a["path"]).name)
    for n in job.get("narration", []):
        if n.get("customImagePath"):
            n["customImagePath"] = str(local_dir / "custom-slides" / Path(n["customImagePath"]).name)
        if n.get("customVideoPath"):
            n["customVideoPath"] = str(local_dir / "custom-media" / Path(n["customVideoPath"]).name)


def main_ingest():
    job_id = os.environ["JOB_ID"]
    bucket = os.environ["S3_BUCKET"]
    source_key = os.environ["SOURCE_KEY"]
    prefix = os.environ["S3_PREFIX"].rstrip("/")  # e.g. "jobs/<job_id>/ingest"

    work = Path("/tmp/ingest") / job_id
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    s3 = boto3.client("s3")
    try:
        source = work / "recording.mp4"
        print(f"downloading s3://{bucket}/{source_key}")
        s3.download_file(bucket, source_key, str(source))
        result = ingest.run(str(source), str(out_dir))

        # Paths in ingest.json become keys relative to the prefix; the
        # worker makes them absolute again wherever it downloads to.
        uploads = []
        for seg in result["segments"]:
            for k in ("framePath", "previewPath"):
                if seg.get(k):
                    rel = f"slides/{Path(seg[k]).name}"
                    uploads.append((seg[k], rel))
                    seg[k] = rel
        for part in result["audio"]:
            rel = Path(part["path"]).name
            uploads.append((part["path"], rel))
            part["path"] = rel
        for local_path, rel in uploads:
            s3.upload_file(local_path, bucket, f"{prefix}/{rel}")
        result["sourceKey"] = source_key
        result["sourceDeleted"] = True
        s3.put_object(Bucket=bucket, Key=f"{prefix}/ingest.json", Body=json.dumps(result))
        s3.delete_object(Bucket=bucket, Key=source_key)
        kinds = {}
        for seg in result["segments"]:
            kinds[seg["kind"]] = kinds.get(seg["kind"], 0) + 1
        print(f"Ingested {result['video']['durationSeconds'] / 60:.1f} min: {kinds}, {len(result['audio'])} audio part(s), "
              f"{len(uploads)} files uploaded to s3://{bucket}/{prefix}/; deleted the upload")
    except Exception:
        traceback.print_exc()
        sys.exit(1)


def main():
    if os.environ.get("MODE", "render") == "ingest":
        main_ingest()
        return
    job_id = os.environ["JOB_ID"]
    bucket = os.environ["S3_BUCKET"]
    prefix = os.environ["S3_PREFIX"].rstrip("/")  # e.g. "jobs/<job_id>"
    render_count = int(os.environ["RENDER_COUNT"])

    local_dir = Path("/tmp/render") / job_id
    local_dir.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3")

    try:
        job_path = local_dir / "job.json"
        s3.download_file(bucket, f"{prefix}/job.json", str(job_path))
        job = json.loads(job_path.read_text())

        s3_download_prefix(s3, bucket, f"{prefix}/slides", local_dir / "slides")
        s3_download_prefix(s3, bucket, f"{prefix}/audio", local_dir / "audio")
        s3_download_prefix(s3, bucket, f"{prefix}/custom-slides", local_dir / "custom-slides")
        s3_download_prefix(s3, bucket, f"{prefix}/media", local_dir / "media")
        s3_download_prefix(s3, bucket, f"{prefix}/custom-media", local_dir / "custom-media")
        rewrite_local_paths(job, local_dir)

        overrides = job.get("render", {}).get("pendingOverrides") or {}

        intro_path = None
        if overrides.get("includeIntro"):
            intro_path = local_dir / "assets" / "intro.mp4"
            intro_path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, INTRO_ASSET_KEY, str(intro_path))

        outro_path = None
        if overrides.get("includeOutro"):
            outro_path = local_dir / "assets" / "outro.mp4"
            outro_path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, OUTRO_ASSET_KEY, str(outro_path))

        final_video, ttype, resolution = render_video(
            job,
            transition_override={k: v for k, v in overrides.items() if k in ("type", "transitionSeconds", "minSlideSeconds")} or None,
            resolution_override=overrides.get("resolution"),
            intro_path=str(intro_path) if intro_path else None,
            outro_path=str(outro_path) if outro_path else None,
            title_cards=bool(overrides.get("titleCards")),
        )

        out_path = local_dir / "output" / f"video-{render_count:02d}.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        final_video.write_videofile(
            str(out_path), fps=30, codec="libx264", audio_codec="aac", logger=None,
            ffmpeg_params=["-movflags", "+faststart"],
        )

        s3.upload_file(str(out_path), bucket, f"{prefix}/output/video-{render_count:02d}.mp4")
        print(f"Rendered and uploaded video-{render_count:02d}.mp4 ({final_video.duration:.2f}s)")

    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
