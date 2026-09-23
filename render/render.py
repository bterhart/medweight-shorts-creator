#!/usr/bin/env python3
"""
Phase 2: render a prepared job (slides + narration audio, from Phase 1)
into a single video with the chosen transition style.

Reads data/jobs/<jobId>/job.json (must be phase=ready_for_render, or
phase=done for a re-render) and writes data/jobs/<jobId>/output/video-NN.mp4.
Never calls Manus or ElevenLabs — everything it needs was already produced
and cached to disk by Phase 1, which is what makes this step cheap and
repeatable (try a different --transition-type without redoing Phase 1).

Usage:
    python render.py <jobId> [--data-dir data]
                      [--transition-type cut|fade|crossfade|wipe|slide]
                      [--transition-seconds 0.75] [--min-slide-seconds 3.0]
                      [--resolution 1920x1080] [--fps 30]

CLI overrides apply to this render only; they're recorded in
job.json's render.history but never overwrite the original params.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from moviepy import (
    ImageClip, ColorClip, VideoClip, VideoFileClip, CompositeVideoClip,
    AudioFileClip, CompositeAudioClip, concatenate_videoclips, vfx, afx,
)


def parse_resolution(res_str):
    w, h = res_str.lower().split("x")
    return int(w), int(h)


def fit_image_clip(image_path, duration, target_w, target_h):
    """Scale the slide to fit within the frame (letterboxed on black), centered."""
    img = ImageClip(image_path)
    scale = min(target_w / img.w, target_h / img.h)
    img = img.resized(scale).with_duration(duration).with_position("center")
    bg = ColorClip(size=(target_w, target_h), color=(0, 0, 0)).with_duration(duration)
    return CompositeVideoClip([bg, img], size=(target_w, target_h)).with_duration(duration)


def fit_video_clip(video_path, target_w, target_h):
    """Scale a fixed intro/outro clip to fit within the frame (letterboxed on
    black), centered - same treatment as fit_image_clip, but keeps the
    clip's own audio and runs for its own duration instead of one we pick."""
    clip = VideoFileClip(video_path)
    scale = min(target_w / clip.w, target_h / clip.h)
    resized = clip.resized(scale).with_position("center")
    bg = ColorClip(size=(target_w, target_h), color=(0, 0, 0)).with_duration(clip.duration)
    composed = CompositeVideoClip([bg, resized], size=(target_w, target_h)).with_duration(clip.duration)
    return composed.with_audio(clip.audio) if clip.audio else composed


def wipe_mask_clip(w, h, transition_duration, total_duration, direction="left-to-right"):
    """A mask that reveals the host clip over `transition_duration`, then stays fully
    revealed for the rest of `total_duration`. No built-in moviepy effect does this,
    so it's the one hand-rolled piece of the whole render pipeline."""
    def frame_function(t):
        frac = min(t / transition_duration, 1.0) if transition_duration > 0 else 1.0
        mask = np.zeros((h, w))
        if direction == "left-to-right":
            mask[:, : int(w * frac)] = 1.0
        else:
            mask[: int(h * frac), :] = 1.0
        return mask
    return VideoClip(frame_function=frame_function, is_mask=True).with_duration(total_duration)


def apply_incoming_transition(clip, ttype, tsec):
    """How this clip appears, relative to whatever is still visible underneath it
    during the overlap window (see `overlap` in compute_starts)."""
    if ttype == "fade":
        return clip.with_effects([vfx.FadeIn(tsec)])
    if ttype == "crossfade":
        return clip.with_effects([vfx.CrossFadeIn(tsec)])
    if ttype == "slide":
        return clip.with_effects([vfx.SlideIn(tsec, "right")])
    if ttype == "wipe":
        return clip.with_mask(wipe_mask_clip(clip.w, clip.h, tsec, clip.duration))
    return clip  # cut: no effect, hard boundary


def apply_outgoing_transition(clip, ttype, tsec):
    if ttype == "fade":
        return clip.with_effects([vfx.FadeOut(tsec)])
    return clip


def build_segments(job):
    slides_by_id = {s["slideId"]: s for s in job["slides"]}
    audio_by_seq = {a["sequenceIndex"]: a for a in job["audio"]}
    min_slide = job["params"]["transition"].get("minSlideSeconds", 3.0)

    segments = []
    for n in sorted(job["narration"], key=lambda x: x["sequenceIndex"]):
        seq = n["sequenceIndex"]
        # A post-review image swap or an added segment carries its own
        # customImagePath, which is never one of job.slides' own images -
        # only fall back to resolving slideId when there isn't one.
        image_path = n.get("customImagePath") or slides_by_id[n["slideId"]]["imagePath"]
        audio = audio_by_seq.get(seq)
        audio_dur = audio["durationSeconds"] if audio else min_slide
        segments.append({
            "sequenceIndex": seq,
            "imagePath": image_path,
            "audioPath": audio["path"] if audio else None,
            "duration": max(audio_dur, min_slide),
        })
    return segments


def compute_starts(segments, overlap):
    """Overlapping transitions (crossfade/slide/wipe) reclaim `overlap` seconds
    at each cut, same as ffmpeg xfade's offset math — replicated here by hand
    so video and audio share identical start times instead of drifting apart."""
    starts, t = [], 0.0
    for i, seg in enumerate(segments):
        starts.append(t)
        is_last = i == len(segments) - 1
        t += seg["duration"] - (0.0 if is_last else overlap)
    return starts, t


def render(job, transition_override=None, resolution_override=None, intro_path=None, outro_path=None):
    transition = dict(job["params"]["transition"])
    if transition_override:
        transition.update(transition_override)
    ttype = transition["type"]
    tsec = transition.get("transitionSeconds", 0.75)
    resolution = resolution_override or job["params"].get("resolution", "640x360")
    target_w, target_h = parse_resolution(resolution)

    # minSlideSeconds may also have been overridden; build_segments reads it
    # off job["params"]["transition"], so apply the override there too.
    job = json.loads(json.dumps(job))  # cheap deep copy, avoid mutating caller's job
    job["params"]["transition"] = transition

    segments = build_segments(job)
    if not segments:
        raise SystemExit("Job has no narration/audio segments to render.")

    overlap = tsec if ttype in ("crossfade", "slide", "wipe") else 0.0
    starts, total_duration = compute_starts(segments, overlap)

    video_layers, audio_layers = [], []
    for i, seg in enumerate(segments):
        clip = fit_image_clip(seg["imagePath"], seg["duration"], target_w, target_h)
        if i > 0:
            clip = apply_incoming_transition(clip, ttype, tsec)
        if i < len(segments) - 1:
            clip = apply_outgoing_transition(clip, ttype, tsec)
        video_layers.append(clip.with_start(starts[i]))

        if seg["audioPath"]:
            a = AudioFileClip(seg["audioPath"])
            # A small audio fade regardless of transition type avoids audible
            # clicks at hard cuts; overlapping transitions reuse the same
            # window so the narration crossfades along with the picture.
            fade = overlap if overlap > 0 else min(0.08, seg["duration"] * 0.1)
            effects = []
            if i > 0 and fade > 0:
                effects.append(afx.AudioFadeIn(fade))
            if i < len(segments) - 1 and fade > 0:
                effects.append(afx.AudioFadeOut(fade))
            if effects:
                a = a.with_effects(effects)
            audio_layers.append(a.with_start(starts[i]))

    final_video = CompositeVideoClip(video_layers, size=(target_w, target_h)).with_duration(total_duration)
    if audio_layers:
        final_audio = CompositeAudioClip(audio_layers).with_duration(total_duration)
        final_video = final_video.with_audio(final_audio)

    clips = []
    if intro_path:
        clips.append(fit_video_clip(intro_path, target_w, target_h))
    clips.append(final_video)
    if outro_path:
        clips.append(fit_video_clip(outro_path, target_w, target_h))
    if len(clips) > 1:
        final_video = concatenate_videoclips(clips)

    return final_video, ttype, resolution


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--transition-type", choices=["cut", "fade", "crossfade", "wipe", "slide"])
    parser.add_argument("--transition-seconds", type=float)
    parser.add_argument("--min-slide-seconds", type=float)
    parser.add_argument("--resolution")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--intro-path", help="Local path to a fixed intro clip to prepend")
    parser.add_argument("--outro-path", help="Local path to a fixed outro clip to append")
    args = parser.parse_args()

    job_dir = Path(args.data_dir) / "jobs" / args.job_id
    job_path = job_dir / "job.json"
    if not job_path.exists():
        sys.exit(f"No job.json at {job_path}")
    job = json.loads(job_path.read_text())

    # "rendering" is included because the trigger that launches this script
    # in the background writes that phase to job.json first (so status polls
    # reflect progress immediately), then hands off to this process.
    if job["phase"] not in ("ready_for_render", "rendering", "done"):
        sys.exit(f"Job {args.job_id} is not ready for render (phase={job['phase']}).")

    transition_override = {}
    if args.transition_type:
        transition_override["type"] = args.transition_type
    if args.transition_seconds is not None:
        transition_override["transitionSeconds"] = args.transition_seconds
    if args.min_slide_seconds is not None:
        transition_override["minSlideSeconds"] = args.min_slide_seconds

    final_video, ttype, resolution = render(
        job, transition_override or None, args.resolution,
        intro_path=args.intro_path, outro_path=args.outro_path,
    )

    out_dir = job_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    render_count = job.get("render", {}).get("renderCount", 0) + 1
    out_path = out_dir / f"video-{render_count:02d}.mp4"

    # +faststart moves the moov atom to the front of the file so browsers can
    # read metadata (duration, seek points) from the first bytes instead of
    # needing a range request to the tail - matters because the file server
    # in front of data/jobs/ may not support HTTP Range at all (see files
    # workflow's caveats).
    final_video.write_videofile(
        str(out_path), fps=args.fps, codec="libx264", audio_codec="aac", logger=None,
        ffmpeg_params=["-movflags", "+faststart"],
    )

    now = datetime.now(timezone.utc).isoformat()
    job.setdefault("render", {})
    history = job["render"].setdefault("history", [])
    history.append({
        "renderCount": render_count,
        "renderedAt": now,
        "outputPath": str(out_path),
        "transition": {**job["params"]["transition"], **transition_override, "type": ttype},
        "resolution": resolution,
    })
    job["render"]["renderedAt"] = now
    job["render"]["outputPath"] = str(out_path)
    job["render"]["renderCount"] = render_count
    job["phase"] = "done"
    job["step"] = "done"
    job["updatedAt"] = now
    job_path.write_text(json.dumps(job, indent=2))

    print(f"Rendered {out_path} ({final_video.duration:.2f}s)")


if __name__ == "__main__":
    main()
