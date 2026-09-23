# Chatbot Shorts

Turns a video transcript (`.srt`) plus its presentation slides (`.pdf`) into a short narrated video:
Manus aligns transcript to slides, Claude condenses the narration to a target length, ElevenLabs voices it,
and the slides are assembled into a video with a chosen transition.

## Layout

- `ui/` — the drag-and-drop web interface (plain HTML/CSS/JS, no build step)
- `backend/` — Flask app + cron-invoked worker doing the actual pipeline (see `docs/backend-design.md`)
- `render/render.py` — standalone, independently testable video-assembly script the worker calls
- `schema/job.schema.json` — the job record shape, shared by the backend and documentation
- `docs/` — `backend-design.md` (architecture) and `deployment.md` (cPanel/CloudLinux setup)
- `archive/` — an earlier n8n-based version of the backend, kept for reference; see `archive/README.md`
  for why it was replaced

## Quick start

See `docs/deployment.md` for the real deployment steps (cPanel Python App + MySQL + cron). In short:
`backend/app.py` is a normal Flask WSGI app, `backend/worker.py` is meant to run from cron once a minute,
and `ui/` just needs to know the Flask app's base URL (set via its Settings panel).

## Status

Manus's API (`file.upload`, `task.create`, `task.listMessages`) is implemented against confirmed specs
from Manus's own docs, not guesses. ElevenLabs and Anthropic calls use standard documented conventions but
haven't been verified against real accounts the way Manus has. The full pipeline has been tested end to end
against a real MySQL database, real PDF extraction, and a mock matching Manus's confirmed API shape — not
yet against live Manus/ElevenLabs/Anthropic accounts.

## Planned work

- **Custom ElevenLabs voices.** Voice selection is currently limited to a small set of ElevenLabs' preset
  library voices (see the voice dropdown in `ui/`). Need to support voices we've cloned/created ourselves
  in ElevenLabs, not just the defaults — likely means accepting an arbitrary voice ID rather than only the
  presets baked into the UI.

## Recently added

- **Faster prepare phase, and per-step timings.** Slide uploads to Manus run in parallel; alignment
  creates up to `MANUS_MAX_CONCURRENT_TASKS` (default 4) chunk tasks before polling any, so a multi-chunk
  deck takes about as long as its slowest chunk instead of the sum; narration cleaning is split across
  concurrent Claude calls (lossless - each excerpt is independent), which also removes a silent-truncation
  risk on long transcripts. `worker.log` now records each step's wall time.
- **Per-short prompt choice.** The Create-short form has a "Narration prompt" dropdown listing the saved
  library (defaulting to the second-pass entry `build_short` used to apply silently by name). The choice is
  stored on the short. The Step 1 dropdown now says when the library is empty, and both lists reload after
  saving the API base in Settings instead of only at page load.
- **Post-render slide editing.** Once a short has a script+audio (`ready_for_render` or `done`), each
  segment can be edited from the review UI: rewrite its narration text (queues just that segment for
  ElevenLabs resynthesis via a new short phase, `editing` — every other segment's audio is untouched),
  replace its image (either an upload, which need not come from the original deck, or a different slide
  picked from the full deck), delete it, or insert a brand-new segment (text + a required uploaded image)
  at any position. Deleting/inserting keeps `sequenceIndex` contiguous across `script[]` and `audio[]`. A
  previously rendered preview stays visible/downloadable after an edit (flagged as stale) rather than being
  hidden or auto-re-rendered — re-rendering is still an explicit action, and it replaces (deletes) the
  previous render rather than keeping both.
- **Custom intro/outro clips.** Each short's render controls now have "Include intro"/"Include outro"
  toggles. The clips themselves (`intro.mp4`, `outro.mp4`) are fixed, non-job-specific files that must be
  placed at `data/assets/intro.mp4` and `data/assets/outro.mp4` on the cPanel host (see
  `docs/deployment.md`) — not committed to the repo. When a render requests one, `backend/fargate_client.py`
  uploads it to S3 for the Fargate task to fetch; `render/render.py` letterboxes it to the render's
  resolution and concatenates it (keeping its own audio) before/after the slideshow.
