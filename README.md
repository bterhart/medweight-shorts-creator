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
- **Custom intro/outro clips.** Rendering only assembles the deck's own slides. We already have intro and
  outro clips produced separately; need a UI toggle (per short, at render time) to prepend/append them to
  the render, and the corresponding support in `render/render.py` and the Fargate render path.
