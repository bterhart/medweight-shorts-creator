#!/usr/bin/env python3
"""
Zoom-recording ingest: turns a recorded presentation (shared screen with a
speaker thumbnail in a corner) into the pieces the prepare pipeline needs -
one full-resolution frame per distinct slide with the thumbnail filled in, a
timeline saying when each slide (or camera-only stretch, or "video off"
placeholder) was on screen, and the audio track in parts ready for
transcription.

Runs inside the Fargate render image (ffmpeg, numpy, Pillow) - never on the
CPU-throttled cPanel host. ecs_task.py calls run() when the task is
dispatched with MODE=ingest; the CLI below is for developing against a
local file (e.g. in CloudShell with the image: see docs/deployment.md).

How detection works. The video is decoded once at ANALYSIS_FPS frames per
second, each downscaled to ANALYSIS_W x ANALYSIS_H greyscale and kept in
memory (a 2.5 h recording is ~500 MB), then:
  - the speaker THUMBNAIL is found from the pixels that keep changing while
    slides are up (the speaker moves; a slide does not). Only the speaker
    moves, not the tile's background, so that region is a lower bound: the
    box is anchored to its corner and widened to at least Zoom's standard
    tile (DEFAULT_THUMBNAIL_BOX, 1/6 of each dimension, 16:9), growing
    beyond it only when the motion does. It is masked out of every
    comparison below and filled in every extracted frame, so a speaker
    switch or a head turn never looks like a slide change. The default box
    is used as is when no compact moving region exists (camera off).
    PowerPoint's slideshow toolbar (TOOLBAR_BOX, bottom-left, shown while
    the mouse moves) is masked for the same reason;
  - a second is DARK (camera shot / placeholder) when its mean luminance is
    low OR most of its pixels are pure black (Zoom's video-off tile on
    black is bimodal: mean 74, but 70% black), otherwise BRIGHT (a slide);
  - within bright stretches, a HOLD is a run of near-identical frames of at
    least MIN_HOLD_SECONDS; a new slide begins at a hold that REMOVES ink
    the current slide had (its title and bullets gone: REMOVED_THRESHOLD of
    the pixels went from dark to light) or that changes most of the picture
    (CHANGED_THRESHOLD, for slides that are mostly images). A PowerPoint
    build only adds ink, so it merges into its slide and the fuller picture
    becomes the slide's frame; cursor twitches and transitions between
    holds attach to the current slide;
  - dark stretches become PLACEHOLDER when perfectly still (Zoom's
    video-off card) and CAMERA otherwise (someone talking on camera);
  - a consolidation pass then drops segments under MIN_SEGMENT_SECONDS,
    merges adjacent slide segments whose frames are near-identical (a
    split caused by motion, not by a slide change), and folds slide
    segments under SHORT_SLIDE_SECONDS into the neighbouring slide.
Each slide's representative frame is the moment in its last hold (its final
state, after any builds) when the toolbar strip is emptiest, pulled at full
resolution; the thumbnail rectangle is filled with the median colour of a
ring just outside it (these decks have flat backgrounds).

Known limit: a dark-themed deck (mostly-black slides) reads as camera or
placeholder. The trim step and the review screen are the backstop.
"""
import argparse
import io
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

ANALYSIS_FPS = 1
ANALYSIS_W, ANALYSIS_H = 320, 180
DARK_LUMINANCE = 70          # 0-255; below this a second is a camera shot or placeholder, not a slide...
BLACK_LEVEL = 16             # ...or when at least BLACK_FRACTION of its pixels are at or below this level
BLACK_FRACTION = 0.5
MOTION_THRESHOLD = 4.0       # mean abs diff (0-255) between consecutive analysis frames: below = static...
HOLD_BREAK_CHANGED = 0.005   # ...unless this fraction of pixels changed by > 25 levels (a text slide swap moves the mean very little)
STILL_THRESHOLD = 0.5        # a dark run whose median per-second motion is under this is the video-off card, not a camera
INK_LUMINANCE = 150          # on a bright slide, pixels darker than this are "ink" (text, shapes, pictures)
REMOVED_THRESHOLD = 0.003    # fraction of pixels that went ink -> background between holds: above = new slide
CHANGED_THRESHOLD = 0.08     # fraction of pixels that changed by > 25 levels: above = new slide regardless
NEAR_IDENTICAL_CHANGED = 0.003  # adjacent slide segments whose frames differ in fewer pixels than this are one slide (a cursor is ~0.0005, one added bullet ~0.006)
MIN_HOLD_SECONDS = 3         # a picture must stay put this long to count as a slide (builds/transitions don't)
MIN_SEGMENT_SECONDS = 2      # anything shorter is noise (a flash to camera between shares, the end card)
SHORT_SLIDE_SECONDS = 12     # a slide segment shorter than this joins its neighbouring slide (flips, transitions)
LONG_STATIC_SECONDS = 600    # a slide held this long before the talk starts is the waiting-room card, not content
SETUP_SCRAP_SECONDS = 60     # a slide shorter than this right before the waiting-room card is the host setting up the share
# Zoom "shared screen with thumbnail": top-right, 1/6 of each dimension (320x180 of 1920x1080, measured on a
# recording). Detection confirms the corner and grows the box if the speaker's motion exceeds it.
DEFAULT_THUMBNAIL_BOX = (0.8333, 0.0, 0.1667, 0.1667)
THUMBNAIL_MOTION_FREQ = 0.1  # a pixel that changes in this fraction of bright seconds belongs to the speaker thumbnail
THUMBNAIL_MAX_FRACTION = 0.4  # a moving region wider or taller than this is not a thumbnail: use the default box
TOOLBAR_BOX = (0.0, 0.95, 0.14, 0.05)  # PowerPoint slideshow toolbar, as a fraction of the frame
EDGE_CROP_RIGHT_PX = 4       # Zoom leaves a dark border column at the right edge of the shared screen
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


def _changed_fraction(a, b, mask):
    return float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > 25)[mask].mean())


def detect_thumbnail_box(frames, log=print):
    """Where the speaker thumbnail is, from the analysis frames: the pixels
    that change in at least THUMBNAIL_MOTION_FREQ of the bright seconds.
    Returns (box as frame fractions, "detected" | "default")."""
    counts = np.zeros((ANALYSIS_H, ANALYSIS_W), dtype=np.int32)
    pairs = 0
    prev = None
    for _, frame in frames:
        bright = float(frame.mean()) >= DARK_LUMINANCE
        if bright and prev is not None:
            counts += np.abs(frame.astype(np.int16) - prev.astype(np.int16)) > 25
            pairs += 1
        prev = frame if bright else None
    if pairs < 30:
        log("  thumbnail: too few slide seconds to detect, using the default box")
        return DEFAULT_THUMBNAIL_BOX, "default"
    ys, xs = np.nonzero(counts >= THUMBNAIL_MOTION_FREQ * pairs)
    if len(xs) < 20:
        log("  thumbnail: no moving region found, using the default box")
        return DEFAULT_THUMBNAIL_BOX, "default"
    x_lo, x_hi = np.percentile(xs, [1, 99])
    y_lo, y_hi = np.percentile(ys, [1, 99])
    if (x_hi - x_lo) > THUMBNAIL_MAX_FRACTION * ANALYSIS_W or (y_hi - y_lo) > THUMBNAIL_MAX_FRACTION * ANALYSIS_H:
        log(f"  thumbnail: moving region too large ({x_lo:.0f}-{x_hi:.0f} x {y_lo:.0f}-{y_hi:.0f}), using the default box")
        return DEFAULT_THUMBNAIL_BOX, "default"
    right = (x_lo + x_hi) / 2 > ANALYSIS_W / 2
    top = (y_lo + y_hi) / 2 < ANALYSIS_H / 2
    pad = 2
    w = (ANALYSIS_W - x_lo if right else x_hi + 1) + pad
    h = (y_hi + 1 if top else ANALYSIS_H - y_lo) + pad
    w, h = max(w, h * 16 / 9), max(h, w * 9 / 16)  # Zoom's tile is 16:9; the speaker rarely moves across all of it
    w = max(w, DEFAULT_THUMBNAIL_BOX[2] * ANALYSIS_W)  # ...and never less than the standard tile
    h = max(h, DEFAULT_THUMBNAIL_BOX[3] * ANALYSIS_H)
    w = min(w, THUMBNAIL_MAX_FRACTION * ANALYSIS_W)
    h = min(h, THUMBNAIL_MAX_FRACTION * ANALYSIS_H)
    x = ANALYSIS_W - w if right else 0
    y = 0 if top else ANALYSIS_H - h
    box = tuple(round(float(v), 4) for v in (x / ANALYSIS_W, y / ANALYSIS_H, w / ANALYSIS_W, h / ANALYSIS_H))
    log(f"  thumbnail: {'top' if top else 'bottom'}-{'right' if right else 'left'} box {box}")
    return box, "detected"


def _duration(s):
    return s["endSeconds"] - s["startSeconds"]


def _ink_moved(old, new, mask):
    """(removed, added, old_ink): fractions of pixels that went ink -> background,
    background -> ink, and the old frame's ink fraction."""
    old_ink, new_ink = old < INK_LUMINANCE, new < INK_LUMINANCE
    return (float((old_ink & ~new_ink)[mask].mean()), float((~old_ink & new_ink)[mask].mean()), float(old_ink[mask].mean()))


def consolidate(segments, mask):
    """Post-pass over detected segments (each slide carrying its analysis
    frame in "_frame"): drop sub-MIN_SEGMENT_SECONDS segments, merge adjacent
    near-identical slides, fold short slides into their neighbouring slide.
    Repeats until nothing changes; keeps the timeline continuous."""
    def absorb(k, into, frame_from=None):
        a, b = sorted((k, into))
        keep = segments[into]
        keep["startSeconds"] = segments[a]["startSeconds"]
        keep["endSeconds"] = segments[b]["endSeconds"]
        if frame_from is not None:
            keep["frameAt"], keep["_frame"] = segments[frame_from]["frameAt"], segments[frame_from]["_frame"]
        del segments[k]

    def cleared_overlay(prev, short):
        """True when the short segment is the previous slide with a region
        cleared and nothing added - a popup or annotation went away - so its
        frame is the clean one to keep."""
        removed, added, old_ink = _ink_moved(prev["_frame"], short["_frame"], mask)
        return added < REMOVED_THRESHOLD and old_ink > 0 and removed / old_ink < 0.5

    changed = True
    while changed and len(segments) > 1:
        changed = False
        for k, s in enumerate(segments):
            if _duration(s) < MIN_SEGMENT_SECONDS:
                absorb(k, k - 1 if k > 0 else k + 1)
                changed = True
                break
        if changed:
            continue
        for k in range(len(segments) - 1):
            a, b = segments[k], segments[k + 1]
            if a["kind"] == b["kind"] == "slide" and _changed_fraction(a["_frame"], b["_frame"], mask) < NEAR_IDENTICAL_CHANGED:
                absorb(k, k + 1)  # the later frame is the slide's final state
                changed = True
                break
        if changed:
            continue
        for k, s in enumerate(segments):
            if s["kind"] != "slide" or _duration(s) >= SHORT_SLIDE_SECONDS:
                continue
            if k > 0 and segments[k - 1]["kind"] == "slide":
                absorb(k, k - 1, frame_from=k if cleared_overlay(segments[k - 1], s) else None)
            elif k + 1 < len(segments) and segments[k + 1]["kind"] == "slide":
                absorb(k, k + 1)
            else:
                continue
            changed = True
            break
    return segments


def detect(video, thumbnail_box=None, log=print):
    """Decode once, find the thumbnail, then segments. Returns (probe_info,
    segments, thumbnail_box, box_source) - each segment {kind:
    slide|camera|placeholder, startSeconds, endSeconds, frameAt, index}
    where frameAt is the best second to pull a slide's frame from."""
    info = probe(video)
    frames = []
    for t, frame in analysis_frames(video):
        frames.append((t, frame))
        if int(t) % 600 == 0 and t > 0:
            log(f"  decoded {int(t) // 60} min")
    if not frames:
        raise RuntimeError("no frames decoded")
    if thumbnail_box is None:
        box, source = detect_thumbnail_box(frames, log)
    else:
        box, source = tuple(thumbnail_box), "given"

    mask = np.ones((ANALYSIS_H, ANALYSIS_W), dtype=bool)
    x0, y0, x1, y1 = _box_pixels(box, ANALYSIS_W, ANALYSIS_H)
    mask[y0:y1, x0:x1] = False
    tx0, ty0, tx1, ty1 = _box_pixels(TOOLBAR_BOX, ANALYSIS_W, ANALYSIS_H)
    mask[ty0:ty1, tx0:tx1] = False

    n = len(frames)
    times = np.array([t for t, _ in frames])
    lum = np.zeros(n)
    black = np.zeros(n)
    motion = np.zeros(n)
    changed = np.zeros(n)
    toolbar_ink = np.zeros(n)
    prev = None
    for i, (_, frame) in enumerate(frames):
        vis = frame[mask]
        lum[i] = float(vis.mean())
        black[i] = float((vis <= BLACK_LEVEL).mean())
        toolbar_ink[i] = float((frame[ty0:ty1, tx0:tx1] < INK_LUMINANCE).mean())
        if prev is not None:
            d = np.abs(frame.astype(np.int16) - prev.astype(np.int16))[mask]
            motion[i], changed[i] = float(d.mean()), float((d > 25).mean())
        prev = frame

    # 1. dark vs bright runs
    runs = []  # [is_dark, start_idx, end_idx_exclusive]
    for i in range(n):
        dark = bool(lum[i] < DARK_LUMINANCE or black[i] >= BLACK_FRACTION)
        if runs and runs[-1][0] == dark:
            runs[-1][2] = i + 1
        else:
            runs.append([dark, i, i + 1])
    # absorb dark blips shorter than MIN_SEGMENT_SECONDS into the bright run before them
    merged = []
    for r in runs:
        if merged and (r[2] - r[1]) < MIN_SEGMENT_SECONDS * ANALYSIS_FPS and r[0] and merged[-1][0] is False:
            merged[-1][2] = r[2]
        elif merged and merged[-1][0] == r[0]:
            merged[-1][2] = r[2]
        else:
            merged.append(list(r))

    segments = []

    def add(kind, i0, i1, at_idx):
        seg = {"kind": kind, "startSeconds": float(times[i0]), "endSeconds": float(times[i1 - 1]) + 1.0 / ANALYSIS_FPS,
               "frameAt": float(times[at_idx])}
        if kind == "slide":
            seg["_frame"] = frames[at_idx][1]
        segments.append(seg)

    def cleanest(h):
        """Index within hold h where the toolbar strip has the least ink (latest on ties)."""
        idx = range(h[0], h[1])
        least = min(toolbar_ink[i] for i in idx)
        return max(i for i in idx if toolbar_ink[i] <= least + 1e-9)

    for dark, i0, i1 in merged:
        if dark:
            motions = motion[i0 + 1:i1]
            still = motions.size == 0 or float(np.median(motions)) < STILL_THRESHOLD  # median: one card-to-black jump must not make it a camera
            add("placeholder" if still else "camera", i0, i1, (i0 + i1) // 2)
            continue
        # 2. within a bright run: holds, then slides
        holds = []  # [start_idx, end_idx_exclusive]
        for i in range(i0, i1):
            static = i > i0 and motion[i] < MOTION_THRESHOLD and changed[i] < HOLD_BREAK_CHANGED
            if static and holds and holds[-1][1] == i:
                holds[-1][1] = i + 1
            elif static:
                holds.append([i - 1, i + 1])  # the previous frame started this still picture
        holds = [h for h in holds if (h[1] - h[0]) >= MIN_HOLD_SECONDS * ANALYSIS_FPS]
        if not holds:
            add("slide", i0, i1, (i0 + i1) // 2)  # never settles (animation?) - keep it as one slide
            continue
        cur = None  # {"start": idx, "rep": frame, "holds": [...]}
        for h in holds:
            rep = frames[h[0] + (h[1] - h[0]) // 2][1]
            if cur is None:
                cur = {"start": i0, "rep": rep, "holds": [h]}
                continue
            old, new = cur["rep"], rep
            removed = _ink_moved(old, new, mask)[0]
            if removed > REMOVED_THRESHOLD or _changed_fraction(old, new, mask) > CHANGED_THRESHOLD:
                add("slide", cur["start"], h[0], cleanest(cur["holds"][-1]))  # final state, after any builds
                cur = {"start": h[0], "rep": rep, "holds": [h]}
            else:
                cur["holds"].append(h)
                cur["rep"] = rep  # a build: the fuller picture is the better representative
        add("slide", cur["start"], i1, cleanest(cur["holds"][-1]))

    segments = consolidate(segments, mask)
    for k, s in enumerate(segments):
        s["index"] = k
        s.pop("_frame", None)
    return info, segments, box, source


def extract_frame(video, at_seconds, out_path, thumbnail_box=DEFAULT_THUMBNAIL_BOX, preview_path=None, preview_w=1280):
    """Full-resolution frame at at_seconds, right border cropped, thumbnail
    rectangle filled from the median colour of a ring around it; optional
    downscaled preview."""
    png = _run(["ffmpeg", "-v", "error", "-ss", f"{at_seconds:.3f}", "-i", str(video), "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "-"]).stdout
    img = Image.open(io.BytesIO(png)).convert("RGB")
    arr = np.array(img)
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = _box_pixels(thumbnail_box, w, h)
    if w > 4 * EDGE_CROP_RIGHT_PX:
        arr = arr[:, :w - EDGE_CROP_RIGHT_PX].copy()
        w = arr.shape[1]
        x1 = min(x1, w)
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
    """Where the audio worth transcribing begins: after the whole opening run
    - placeholders (camera off, black), slides held LONG_STATIC_SECONDS or
    more (the waiting-room card), the host's share being set up just before
    that card, and transitional scraps - up to the first camera or ordinary
    slide segment, less AUDIO_LEAD_SECONDS. Errs early, because a late
    start would lose speech from the transcript while an early one only
    sends the card's music to transcription: 0 unless the recording opens
    on a placeholder (camera off / black) and the card is held for
    LONG_STATIC_SECONDS."""
    if not segments or segments[0]["kind"] != "placeholder":
        return 0.0

    def opening(k):
        s = segments[k]
        if s["kind"] == "placeholder":
            return True
        if s["kind"] != "slide":
            return False
        if _duration(s) >= LONG_STATIC_SECONDS or _duration(s) < SHORT_SLIDE_SECONDS:
            return True
        nxt = segments[k + 1] if k + 1 < len(segments) else None
        return (_duration(s) < SETUP_SCRAP_SECONDS and nxt is not None and nxt["kind"] == "slide"
                and _duration(nxt) >= LONG_STATIC_SECONDS)

    i = 0
    while i < len(segments) and opening(i):
        i += 1
    if i == 0 or i >= len(segments):
        return 0.0
    return max(0.0, segments[i]["startSeconds"] - AUDIO_LEAD_SECONDS)


def run(video, out_dir, thumbnail_box=None, log=print, with_audio=True):
    out_dir = Path(out_dir)
    (out_dir / "slides").mkdir(parents=True, exist_ok=True)
    log(f"analysing {video}")
    info, segments, box, box_source = detect(video, thumbnail_box, log=log)
    log(f"  {len(segments)} segments over {info['durationSeconds'] / 60:.1f} min")
    for s in segments:
        if s["kind"] == "slide":
            stem = f"frame-{s['index']:03d}"
            s["framePath"] = str(out_dir / "slides" / f"{stem}.png")
            s["previewPath"] = str(out_dir / "slides" / f"{stem}-preview.png")
            extract_frame(video, s["frameAt"], s["framePath"], box, s["previewPath"])
    start = content_start_hint(segments)
    audio = extract_audio_parts(video, out_dir, start, info["durationSeconds"]) if with_audio else []
    result = {"video": info, "thumbnailBox": list(box), "thumbnailBoxSource": box_source, "segments": segments,
              "audioStartSeconds": start, "audio": audio,
              "thresholds": {"dark": DARK_LUMINANCE, "blackLevel": BLACK_LEVEL, "blackFraction": BLACK_FRACTION,
                             "motion": MOTION_THRESHOLD, "still": STILL_THRESHOLD, "ink": INK_LUMINANCE,
                             "removed": REMOVED_THRESHOLD, "changed": CHANGED_THRESHOLD, "holdBreak": HOLD_BREAK_CHANGED,
                             "nearIdentical": NEAR_IDENTICAL_CHANGED, "minHold": MIN_HOLD_SECONDS,
                             "minSegment": MIN_SEGMENT_SECONDS, "shortSlide": SHORT_SLIDE_SECONDS, "longStatic": LONG_STATIC_SECONDS,
                             "setupScrap": SETUP_SCRAP_SECONDS}}
    (out_dir / "ingest.json").write_text(json.dumps(result, indent=2))
    return result


def _clock(s):
    s = int(round(s))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("--out", required=True)
    p.add_argument("--thumbnail-box", default="auto",
                   help="x,y,w,h as fractions of the frame, or 'auto' (default) to detect it from the speaker's motion")
    p.add_argument("--no-audio", action="store_true")
    args = p.parse_args()
    box = None if args.thumbnail_box == "auto" else tuple(float(v) for v in args.thumbnail_box.split(","))
    result = run(args.video, args.out, box, with_audio=not args.no_audio)
    print(f"\nthumbnail box ({result['thumbnailBoxSource']}): {result['thumbnailBox']}")
    print("timeline:")
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
