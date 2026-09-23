# UI

Plain HTML/CSS/JS, no build step. Talks to `backend/app.py` (a Flask app — originally these were n8n
webhooks, see `archive/README.md` for why that changed):

- `POST {apiBase}/jobs` — job intake
- `GET {apiBase}/jobs/:jobId/status` — polled every 3s
- `POST {apiBase}/jobs/:jobId/shorts` — create a short (condenses the job's permanent narration to a
  target duration/topic, resolves voice, synthesizes audio)
- `POST {apiBase}/jobs/:jobId/shorts/:shortId/render` — render trigger, scoped to one short
- `PATCH {apiBase}/jobs/:jobId/shorts/:shortId/segments/:seq` — rewrite one segment's narration text
  (queues just that segment for resynthesis via the short's `editing` phase)
- `POST {apiBase}/jobs/:jobId/shorts/:shortId/segments/:seq/image` — replace one segment's image: an
  `image` upload (need not come from the deck), or a `sourceSlideId` from the job's full deck
- `DELETE {apiBase}/jobs/:jobId/shorts/:shortId/segments/:seq` — remove one segment
- `POST {apiBase}/jobs/:jobId/shorts/:shortId/segments` — insert a new segment (`script`, `image`,
  optional `position`)
- `GET {apiBase}/files?path=...` — slide images and narration audio

## Running it

Serve `ui/` as static files any way you like (it's just three files) and open `index.html`. Click
**Settings** to set the backend's base URL — leave it blank if the UI is served from the same origin.

## Flow

1. **Setup** — drag/drop the `.srt` and PDF(s), mark one PDF primary, submit (duration/voice aren't chosen
   here - see step 3). Posts multipart form data: one `params` field (JSON-stringified config) plus `srt`
   and `pdf_0`/`pdf_1`/… binary fields.
2. **Progress** — polls status until `phase` is `ready_for_review` or `failed`. Alignment runs against the
   *full* transcript and *full* slide deck (no target duration), then each slide's excerpt is cleaned
   (filler and personal references removed, nothing shortened). This narration is permanent - nothing later
   ever rewrites it in place.
3. **Review & shorts** — the top of this screen is a fixed, read-only card per slide (image, cleaned
   narration, a collapsed "show original excerpt" comparison) - the full 1:1 alignment, unchanged for the
   life of the job. Below it, a **Shorts** panel lets you create any number of independent shorts from that
   narration: give each one a required **topic** (what it's about - the only way to tell shorts on the same
   job apart), a target duration, and a voice, then **Create short**. This calls `POST /jobs/:id/shorts`,
   which writes one coherent condensed script from the full narration (not a per-slide shrink) and grounds
   it back onto whichever original slides it actually covers - often a subset, sometimes just a few slides
   out of a large deck. Once a short reaches `ready_for_render`, its card gets its own transition/output
   controls and **Render video** button; the rendered video and download link appear inline on that short's
   card once done. From that point each segment on the card is editable: rewrite its narration text (**Save
   text** — resynthesizes just that segment's audio, nothing else), swap its image (a deck-slide picker or
   **Upload image…**), **Delete slide**, or use the **Add a slide** form to insert a new segment (text + image)
   at any position. A previously rendered preview stays visible after an edit, flagged as stale, until you
   render again - at which point it's deleted and replaced by the new render. Narration typed into a
   segment's box only counts once you click **Save text**; any action that would refresh the screen
   (render, save another segment, delete, swap an image, add a slide, create a short, switch job) first
   warns if unsaved edits would be lost, and so does closing the tab. Only one short per job can be mid-pipeline (condensing, editing, or rendering) at a time -
   creating, editing, or rendering another while one is in flight gets a 409 until it finishes.

## Verified in a real browser, not just read against the code

Before the backend rewrite, a throwaway mock backend (implementing this same endpoint contract, and
actually shelling out to the real `render/render.py` rather than faking the render step) was driven through
Playwright end to end: file upload → submit → poll → review with real slide images and audio players →
render → result with a working video. Two real bugs were caught and fixed this way, not by review — a
`validateSetup()` that required a non-empty `apiBase` (broke the same-origin case) and a multipart params
contract mismatch. Both fixes are in the current `app.js`.

## Two things worth knowing about how the backend handles this

**1080p rendering is slow — ~100 seconds for a trivial 3-segment, 7.5s test clip via MoviePy.** A full
3-5 minute video with many more segments could take several minutes. `backend/worker.py` never runs a
render inline with the HTTP request that triggers it — `POST /jobs/:id/shorts/:shortId/render` just flips
that short to `rendering` and returns immediately; a cron-invoked worker picks it up separately, so there's
no HTTP timeout to hit no matter how slow a render gets (see `docs/backend-design.md`). Resolution is chosen at
render time with a Preview/Standard/Full tier (`ui/app.js`'s `RESOLUTION_MAP`), defaulting to Preview
(640x360) so the normal iterate-on-transitions loop stays fast — Full/1080p is opt-in for the render you're
keeping.

**Browsers probe video files with `HEAD` before the ranged `GET`.** Learned the hard way during earlier
testing: a `<video>` element failed to load at all — not slowly, no visible error, just stuck at
`readyState 0` — because the file server only handled plain `GET` and returned a method-not-allowed for
`HEAD`, which Chrome's media pipeline treats as fatal. `backend/app.py`'s `/files` route uses Flask's
`send_file(..., conditional=True)`, which handles `HEAD` and byte-range requests correctly out of the box —
this is no longer something to hand-roll or get wrong.
