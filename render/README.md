# Phase 2 — render

Turns a Phase-1-prepared job (`data/jobs/<jobId>/job.json` at `phase: ready_for_render`) into a video.
Reads only files Phase 1 already produced — never calls Claude or ElevenLabs — so it's cheap and safe
to re-run repeatedly while tuning the transition.

## Setup

```
pip install -r render/requirements.txt
```

Also requires `ffmpeg` on PATH (used internally by moviepy for encoding).

## Usage

```
python render/render.py <jobId> --data-dir data
```

Try a different transition without touching Phase 1's output (recorded under `render.history` in
`job.json`, the base `params.transition` is left untouched):

```
python render/render.py <jobId> --data-dir data --transition-type wipe --transition-seconds 1.0
```

Options: `--transition-type {cut,fade,crossfade,wipe,slide}`, `--transition-seconds`,
`--min-slide-seconds`, `--resolution WxH`, `--fps`, `--intro-path`/`--outro-path` (fixed clips to
prepend/append, letterboxed to the target resolution, keeping their own audio).

Output goes to `data/jobs/<jobId>/output/video-NN.mp4` (`NN` increments on every render — old renders
are kept, not overwritten).

In production the same `render()` function runs inside the Fargate container (`ecs_task.py`) - a Docker
image that must be rebuilt and pushed to ECR after any change to the files in this directory; see
"Rebuilding the Fargate render image" in `docs/deployment.md`. It renders against a
per-short render view that `backend/fargate_client.py` uploads to S3 — shaped like the job-level input
above (`slides`/`narration`/`audio`/`params`), where `narration` is that short's `script`. A segment may
carry a `customImagePath` (its image was replaced with an upload, or it was added post-review); when set,
`build_segments` uses it instead of resolving `slideId` against `slides`. There, unlike the CLI path
above, a re-render replaces the previous one: `POST .../render` clears the stale preview and the worker
deletes the old `video-NN.mp4` from S3 before dispatching the new render. The finished video is served to the
UI through a presigned S3 URL; the worker stores one at completion and `GET /jobs/<id>/status` re-signs it on
every read, so the preview never expires in the UI.

## How the transitions work

Every image is letterboxed (scaled to fit, centered on black) to the target resolution first, so mixed
source aspect ratios don't distort. Then, per `params.transition.type`:

- **cut** — hard boundary, no overlap.
- **fade** — each clip fades to/from black within its own duration (`vfx.FadeIn`/`FadeOut`); no overlap
  needed since the fade happens inside existing footage, not besides it.
- **crossfade** — alpha blend between the outgoing and incoming clip (`vfx.CrossFadeIn`), needs the two
  clips to genuinely overlap on screen for `transitionSeconds`.
- **slide** — incoming clip slides in from the right over the outgoing one (`vfx.SlideIn`), same overlap
  requirement as crossfade.
- **wipe** — not a built-in moviepy effect; implemented as a small hand-rolled mask (`wipe_mask_clip`)
  that reveals the incoming clip left-to-right over `transitionSeconds`, then stays fully revealed.

For the three overlapping styles (crossfade/slide/wipe), `compute_starts` reclaims `transitionSeconds` at
each cut — replicating the same offset math ffmpeg's `xfade` filter does internally — and audio segments
are given a matching fade over the identical window, so narration and picture never drift out of sync
across a multi-minute render.

## Verified

Hand-tested against real output (not just read against docs): rendered all five transition types against
a synthetic 3-slide job (mixed 16:9/4:3 source images, three different-length audio clips including one
shorter than `minSlideSeconds`), confirmed total video duration exactly matches the hand-computed overlap
math for each transition type, and inspected extracted mid-transition frames pixel-by-pixel against
predicted colors/positions for wipe, crossfade, slide, and fade. `job.json`'s `render.history` correctly
accumulates one entry per re-render with its own transition/resolution, `renderCount` increments, and
`phase` moves to `done`.

Built and tested against `moviepy==2.1.2`'s actual API (`with_duration`/`with_effects`/etc.) — note this is
a different, incompatible API from MoviePy 1.x (no `moviepy.editor`, no `.fx()` chaining with plain
functions); don't mix in v1-style snippets from older tutorials/StackOverflow answers.
