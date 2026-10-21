#!/usr/bin/env python3
"""
Zoom-recording ingest: turns a recorded presentation (shared screen with a
speaker thumbnail in a fixed corner) into the pieces the prepare pipeline
needs - one full-resolution frame per distinct slide with the thumbnail
filled in, a timeline saying when each slide (or camera-only stretch, or
"video off" placeholder) was on screen, and the audio track in parts ready
for transcription.

Runs inside the Fargate render image (ffmpeg, numpy, Pillow) - never on the
CPU-throttled cPanel host. ecs_task.py calls run() when the task is
dispatched with MODE=ingest; the CLI below is for developing against a
local file (e.g. in CloudShell with the image: see docs/deployment.md).

How detection works, in one pass over the video at ANALYSIS_FPS frames per
second, each downscaled to ANALYSIS_W x ANALYSIS_H greyscale with the
thumbnail rectangle blanked so a speaker switch never looks like a slide
change:
  - a second is DARK (camera shot / placeholder) or BRIGHT (a slide) by its
    mean luminance;
  - within bright stretches, a HOLD is a run of near-identical frames of at
    least MIN_HOLD_SECONDS; a new slide begins at a hold that REMOVES ink
    the current slide had (its title and bullets gone: REMOVED_THRESHOLD of
    the pixels went from dark to light) or that changes most of the picture
    (CHANGED_THRESHOLD, for slides that are mostly images). A PowerPoint
    build only adds ink, so it merges into its slide and the fuller picture
    becomes the slide's frame; cursor twitches and transitions between
    holds attach to the current slide;
  - dark stretches become PLACEHOLDER when perfectly still (Zoom's
    video-off card) and CAMERA otherwise (someone talking on camera).
Each slide's representative frame is taken from the middle of its last
hold (its final state, after any builds) at full resolution; the thumbnail rectangle is filled with the median
colour of a ring just outside it (these decks have flat backgrounds).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ANALYSIS_FPS = 1
ANALYSIS_W, ANALYSIS_H = 320, 180
DARK_LUMINANCE = 70          # 0-255; below this a second is a camera shot or placeholder, not a slide
MOTION_THRESHOLD = 4.0       # mean abs diff (0-255) between consecutive analysis frames: below = static...
HOLD_BREAK_CHANGED = 0.005   # ...unless this fraction of pixels changed by > 25 levels (a text slide swap moves the mean very little)
STILL_THRESHOLD = 0.5        # a dark run with mean motion under this is the video-off card, not a camera
INK_LUMINANCE = 150          # on a bright slide, pixels darker than this are "ink" (text, shapes, pictures)
REMOVED_THRESHOLD = 0.003    # fraction of pixels that went ink -> background between holds: above = new slide
CHANGED_THRESHOLD = 0.08     # fraction of pixels that changed by > 25 levels: above = new slide regardless
MIN_HOLD_SECONDS = 3         # a picture must stay put this long to count as a slide (builds/transitions don't)
MIN_SEGMENT_SECONDS = 2      # dark runs shorter than this are noise (a flash to camera between shares)
# Zoom "shared screen with thumbnail": top-right, 1/6 of each dimension (320x180 of 1920x1080).
DEFAULT_THUMBNAIL_BOX = (0.8333, 0.0, 0.1667, 0.1667)
AUDIO_PART_SECONDS = 20 * 60  # transcription is sent in parts no longer than this
AUDIO_LEAD_SECONDS = 60       # audio starts this long before the first non-opening segment


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def probe(video):
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(video)]).stdout
    info = json.loads(out)
    stream = info["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    return {"width": int(stream["width"]), "height": int(stream["height"]),
            "fps": float(num) / float(den or 1), "durationSeconds": float(info["format"]["duration"])}


def analysis_frames(video):
    """Yields (second, 2-D uint8 array) at ANALYSIS_FPS, downscaled greyscale."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
           f"fps={ANALYSIS_FPS},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 7)
    size = ANALYSIS_W * ANALYSIS_H
    t = 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield t / ANALYSIS_FPS, np.frombuffer(buf, dtype=np.uint8).reshape(ANALYSIS_H, ANALYSIS_W)
        t += 1
    proc.wait()


def _box_pixels(box, w, h):
    x, y, bw, bh = box
    return int(round(x * w)), int(round(y * h)), int(round((x + bw) * w)), int(round((y + bh) * h))


def detect(video, thumbnail_box=DEFAULT_THUMBNAIL_BOX, log=print):
    """One pass: per-second luminance and motion with the thumbnail masked,
    then segments. Returns (probe_info, segments) - each segment
    {kind: slide|camera|placeholder, startSeconds, endSeconds, frameAt}
    where frameAt is the best second to pull a slide's frame from."""
    info = probe(video)
    x0, y0, x1, y1 = _box_pixels(thumbnail_box, ANALYSIS_W, ANALYSIS_H)
    mask = np.ones((ANALYSIS_H, ANALYSIS_W), dtype=bool)
    mask[y0:y1, x0:x1] = False
    n_mask = int(mask.sum())

    seconds = []  # (t, luminance, motion, frame, changed_fraction)
    prev = None
    for t, frame in analysis_frames(video):
        lum = float(frame[mask].mean())
        if prev is not None:
            d = np.abs(frame.astype(np.int16) - prev.astype(np.int16))[mask]
            motion, changed = float(d.mean()), float((d > 25).mean())
        else:
            motion, changed = 0.0, 0.0
        seconds.append((t, lum, motion, frame, changed))
        prev = frame
        if int(t) % 600 == 0 and t > 0:
            log(f"  analysed {int(t) // 60} min")
    if not seconds:
        raise RuntimeError("no frames decoded")

    # 1. dark vs bright runs
    runs = []  # [kind_or_None, start_idx, end_idx_exclusive]
    for i, (t, lum, motion, _, _) in enumerate(seconds):
        dark = lum < DARK_LUMINANCE
        if runs and runs[-1][0] == dark:
            runs[-1][2] = i + 1
        else:
            runs.append([dark, i, i + 1])
    # absorb dark blips shorter than MIN_SEGMENT_SECONDS into their neighbours
    merged = []
    for r in runs:
        if merged and (r[2] - r[1]) < MIN_SEGMENT_SECONDS * ANALYSIS_FPS and r[0] and merged[-1][0] is False:
            merged[-1][2] = r[2]
        elif merged and merged[-1][0] == r[0]:
            merged[-1][2] = r[2]
        else:
            merged.append(list(r))

    segments = []

    def add(kind, i0, i1, frame_at):
        segments.append({"kind": kind, "startSeconds": seconds[i0][0], "endSeconds": seconds[i1 - 1][0] + 1.0 / ANALYSIS_FPS,
                         "frameAt": frame_at})

    for dark, i0, i1 in merged:
        if dark:
            motions = [seconds[i][2] for i in range(i0 + 1, i1)]
            still = not motions or float(np.mean(motions)) < STILL_THRESHOLD
            add("placeholder" if still else "camera", i0, i1, seconds[(i0 + i1) // 2][0])
            continue
        # 2. within a bright run: holds, then slides
        holds = []  # [start_idx, end_idx_exclusive]
        for i in range(i0, i1):
            static = i > i0 and seconds[i][2] < MOTION_THRESHOLD and seconds[i][4] < HOLD_BREAK_CHANGED
            if static and holds and holds[-1][1] == i:
                holds[-1][1] = i + 1
            elif static:
                holds.append([i - 1, i + 1])  # the previous frame started this still picture
        holds = [h for h in holds if (h[1] - h[0]) >= MIN_HOLD_SECONDS * ANALYSIS_FPS]
        if not holds:
            add("slide", i0, i1, seconds[(i0 + i1) // 2][0])  # never settles (animation?) - keep it as one slide
            continue
        cur = None  # {"start": idx, "rep": frame, "holds": [...]}
        for h in holds:
            rep = seconds[h[0] + (h[1] - h[0]) // 2][3]
            if cur is None:
                cur = {"start": i0, "rep": rep, "holds": [h]}
                continue
            old, new = cur["rep"], rep
            removed = float(((old < INK_LUMINANCE) & (new >= INK_LUMINANCE))[mask].mean())
            changed = float((np.abs(new.astype(np.int16) - old.astype(np.int16)) > 25)[mask].mean())
            if removed > REMOVED_THRESHOLD or changed > CHANGED_THRESHOLD:
                last = cur["holds"][-1]  # the slide's final state - after any builds - is the picture to keep
                add("slide", cur["start"], h[0], seconds[last[0] + (last[1] - last[0]) // 2][0])
                cur = {"start": h[0], "rep": rep, "holds": [h]}
            else:
                cur["holds"].append(h)
                cur["rep"] = rep  # a build: the fuller picture is the better representative
        last = cur["holds"][-1]
        add("slide", cur["start"], i1, seconds[last[0] + (last[1] - last[0]) // 2][0])

    for k, s in enumerate(segments):
        s["index"] = k
    return info, segments


def extract_frame(video, at_seconds, out_path, thumbnail_box=DEFAULT_THUMBNAIL_BOX, preview_path=None, preview_w=1280):
    """Full-resolution frame at at_seconds with the thumbnail rectangle filled
    from the median colour of a ring around it; optional downscaled preview."""
    png = _run(["ffmpeg", "-v", "error", "-ss", f"{at_seconds:.3f}", "-i", str(video), "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "-"]).stdout
    import io
    img = Image.open(io.BytesIO(png)).convert("RGB")
    arr = np.array(img)
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = _box_pixels(thumbnail_box, w, h)
    ring = 12
    rx0, ry0, rx1, ry1 = max(0, x0 - ring), max(0, y0 - ring), min(w, x1 + ring), min(h, y1 + ring)
    region = arr[ry0:ry1, rx0:rx1].copy()
    inner = np.ones(region.shape[:2], dtype=bool)
    inner[(y0 - ry0):(y1 - ry0), (x0 - rx0):(x1 - rx0)] = False
    if inner.any():
        fill = np.median(region[inner].reshape(-1, 3), axis=0).astype(np.uint8)
        arr[y0:y1, x0:x1] = fill
    out = Image.fromarray(arr)
    out.save(out_path)
    if preview_path:
        out.resize((preview_w, round(h * preview_w / w)), Image.LANCZOS).save(preview_path)
    return w, h


def extract_audio_parts(video, out_dir, start_seconds, duration_seconds):
    """Mono 64 kbps MP3 from start_seconds to the end, in AUDIO_PART_SECONDS
    parts: [{path, offsetSeconds, durationSeconds}]."""
    parts = []
    t = max(0.0, start_seconds)
    k = 0
    while t < duration_seconds:
        length = min(AUDIO_PART_SECONDS, duration_seconds - t)
        path = Path(out_dir) / f"audio-{k:02d}.mp3"
        _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-t", f"{length:.3f}", "-i", str(video),
              "-vn", "-ac", "1", "-ar", "22050", "-b:a", "64k", str(path)])
        parts.append({"path": str(path), "offsetSeconds": t, "durationSeconds": length})
        t += length
        k += 1
    return parts


def content_start_hint(segments):
    """Where the audio worth transcribing begins: after the opening run of
    placeholder + the first long static slide (the welcome card). Falls back
    to 0 if the recording doesn't open that way."""
    i = 0
    while i < len(segments) and segments[i]["kind"] == "placeholder":
        i += 1
    if i < len(segments) and segments[i]["kind"] == "slide" and (segments[i]["endSeconds"] - segments[i]["startSeconds"]) >= 300:
        i += 1
    if i >= len(segments) or i == 0:
        return 0.0
    return max(0.0, segments[i]["startSeconds"] - AUDIO_LEAD_SECONDS)


def run(video, out_dir, thumbnail_box=DEFAULT_THUMBNAIL_BOX, log=print, with_audio=True):
    out_dir = Path(out_dir)
    (out_dir / "slides").mkdir(parents=True, exist_ok=True)
    log(f"analysing {video}")
    info, segments = detect(video, thumbnail_box, log=log)
    log(f"  {len(segments)} segments over {info['durationSeconds'] / 60:.1f} min")
    for s in segments:
        if s["kind"] == "slide":
            stem = f"frame-{s['index']:03d}"
            s["framePath"] = str(out_dir / "slides" / f"{stem}.png")
            s["previewPath"] = str(out_dir / "slides" / f"{stem}-preview.png")
            extract_frame(video, s["frameAt"], s["framePath"], thumbnail_box, s["previewPath"])
    start = content_start_hint(segments)
    audio = extract_audio_parts(video, out_dir, start, info["durationSeconds"]) if with_audio else []
    result = {"video": info, "thumbnailBox": list(thumbnail_box), "segments": segments,
              "audioStartSeconds": start, "audio": audio,
              "thresholds": {"dark": DARK_LUMINANCE, "motion": MOTION_THRESHOLD, "still": STILL_THRESHOLD, "ink": INK_LUMINANCE,
                             "removed": REMOVED_THRESHOLD, "changed": CHANGED_THRESHOLD, "holdBreak": HOLD_BREAK_CHANGED, "minHold": MIN_HOLD_SECONDS}}
    (out_dir / "ingest.json").write_text(json.dumps(result, indent=2))
    return result


def _clock(s):
    s = int(round(s))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("--out", required=True)
    p.add_argument("--thumbnail-box", default=",".join(str(v) for v in DEFAULT_THUMBNAIL_BOX),
                   help="x,y,w,h as fractions of the frame (default: Zoom top-right 1/6)")
    p.add_argument("--no-audio", action="store_true")
    args = p.parse_args()
    box = tuple(float(v) for v in args.thumbnail_box.split(","))
    result = run(args.video, args.out, box, with_audio=not args.no_audio)
    print("\ntimeline:")
    for s in result["segments"]:
        length = s["endSeconds"] - s["startSeconds"]
        print(f"  {s['index']:3d}  {_clock(s['startSeconds'])} - {_clock(s['endSeconds'])}  {length:6.0f}s  {s['kind']:<11}"
              + (f"  frame@{_clock(s['frameAt'])}" if s["kind"] == "slide" else ""))
    kinds = {}
    for s in result["segments"]:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    print(f"\n{kinds}  audio from {_clock(result['audioStartSeconds'])} in {len(result['audio'])} part(s)")


if __name__ == "__main__":
    main()
