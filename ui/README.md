# UI

Plain HTML/CSS/JS, no build step. Talks to four endpoints served by `backend/app.py` (a Flask app —
originally these were n8n webhooks, see `archive/README.md` for why that changed; the endpoint contract
below is unchanged, which is why `ui/` itself needed zero edits when the backend was rewritten):

- `POST {apiBase}/jobs` — job intake
- `GET {apiBase}/jobs/:jobId/status` — polled every 3s
- `POST {apiBase}/jobs/:jobId/render` — render trigger
- `GET {apiBase}/files?path=...` — slide images, narration audio, and the final video

## Running it

Serve `ui/` as static files any way you like (it's just three files) and open `index.html`. Click
**Settings** to set the backend's base URL — leave it blank if the UI is served from the same origin.

## Flow

1. **Setup** — drag/drop the `.srt` and PDF(s), mark one PDF primary, submit (duration/voice aren't chosen
   here - see step 3). Posts multipart form data: one `params` field (JSON-stringified config) plus `srt`
   and `pdf_0`/`pdf_1`/… binary fields.
2. **Progress** — polls status until `phase` is `ready_for_review` or `failed`. Alignment runs against the
   *full* transcript and *full* slide deck (no target duration), then each slide's excerpt is cleaned
   (filler and personal references removed, nothing shortened).
3. **Review** — one card per slide: slide image, cleaned narration text, a collapsed "show original excerpt"
   comparison. A **Finalize narration** panel here is where you pick target duration and voice and trigger
   `POST /jobs/:id/condense` (shortens the narration, resolves the voice, synthesizes audio) - once that
   reaches `ready_for_render`, the panel below it (transition, output shape/quality, **Render video**)
   appears. Re-running just the render (different transition/resolution) doesn't redo alignment or voice.
4. **Result** — inline video preview + download link. "Back to review" returns to step 3 for another
   transition attempt without re-doing narration or voice.

## Verified in a real browser, not just read against the code

Before the backend rewrite, a throwaway mock backend (implementing this same four-endpoint contract, and
actually shelling out to the real `render/render.py` rather than faking the render step) was driven through
Playwright end to end: file upload → submit → poll → review with real slide images and audio players →
render → result with a working video. Two real bugs were caught and fixed this way, not by review — a
`validateSetup()` that required a non-empty `apiBase` (broke the same-origin case) and a multipart params
contract mismatch. Both fixes are in the current `app.js`.

## Two things worth knowing about how the backend handles this

**1080p rendering is slow — ~100 seconds for a trivial 3-segment, 7.5s test clip via MoviePy.** A full
3-5 minute video with many more segments could take several minutes. `backend/worker.py` never runs a
render inline with the HTTP request that triggers it — `POST /jobs/:id/render` just flips the job to
`rendering` and returns immediately; a cron-invoked worker picks it up separately, so there's no HTTP
timeout to hit no matter how slow a render gets (see `docs/backend-design.md`). Resolution is chosen at
render time with a Preview/Standard/Full tier (`ui/app.js`'s `RESOLUTION_MAP`), defaulting to Preview
(640x360) so the normal iterate-on-transitions loop stays fast — Full/1080p is opt-in for the render you're
keeping.

**Browsers probe video files with `HEAD` before the ranged `GET`.** Learned the hard way during earlier
testing: a `<video>` element failed to load at all — not slowly, no visible error, just stuck at
`readyState 0` — because the file server only handled plain `GET` and returned a method-not-allowed for
`HEAD`, which Chrome's media pipeline treats as fatal. `backend/app.py`'s `/files` route uses Flask's
`send_file(..., conditional=True)`, which handles `HEAD` and byte-range requests correctly out of the box —
this is no longer something to hand-roll or get wrong.
