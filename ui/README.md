# UI

Plain HTML/CSS/JS, no build step. Talks to four n8n webhooks:

- `POST {apiBase}/jobs` — Phase 1 intake (`phase1-prepare.workflow.json`)
- `GET {apiBase}/jobs/:jobId/status` — polled every 3s during Phase 1 (`status.workflow.json`)
- `POST {apiBase}/jobs/:jobId/render` — Phase 2 trigger (`render-trigger.workflow.json`)
- `GET {apiBase}/files?path=...` — slide images, narration audio, and the final video (`files.workflow.json`)

## Running it

Serve `ui/` as static files any way you like (it's just three files) and open `index.html`. Click
**Settings** to set the n8n webhook base URL — leave it blank if the UI is served from the same origin as
the webhooks.

## Flow

1. **Setup** — drag/drop the `.srt` and PDF(s), mark one PDF primary, set duration/voice/transition, submit.
   Posts multipart form data: one `params` field (JSON-stringified config) plus `srt` and `pdf_0`/`pdf_1`/…
   binary fields — matching what `Build Job Record` in Phase 1 expects.
2. **Progress** — polls status until `phase` is `ready_for_render` or `failed`.
3. **Review** — one card per narration segment: slide image, script text, audio player. Nothing has been
   rendered yet. Change the transition controls and click **Render video** as many times as you want — each
   click only re-runs Phase 2 (fast, local, free), never Phase 1.
4. **Result** — inline video preview + download link. "Back to review" returns to step 3 for another
   transition attempt without re-uploading anything.

## Verified in a real browser, not just read against the code

Built a throwaway mock backend implementing the same four-endpoint contract (real multipart parsing, and
actually shelling out to the real `render/render.py` for the render step — not faked) and drove the full
UI through Playwright: file upload → submit → poll → review with real slide images and audio players →
render → result with a working video. Two real bugs were caught and fixed this way, not by review:

- **`validateSetup()` required a non-empty `apiBase`**, which meant the submit button stayed disabled
  forever in the (fully valid) same-origin configuration. Fixed — `apiBase` is optional.
- **Multipart params contract mismatch**: Phase 1's `Build Job Record` node originally assumed
  `$json.body` would already be a parsed object; multipart text fields actually arrive as raw strings.
  Fixed by having the UI send one `params` field as a JSON string and having the workflow `JSON.parse` it.

## Two things worth knowing before you deploy this for real

**1080p rendering is slow — ~100 seconds for a trivial 3-segment, 7.5s test clip via MoviePy.** A full
3-5 minute video with many more segments could take several minutes. Two changes address this rather than
just working around it: (1) `render-trigger.workflow.json` is now async — it backgrounds the render and
acks in milliseconds regardless of resolution or video length, so it can't hit n8n's webhook timeout no
matter how slow a render gets (see `docs/phase2-render-trigger.md`); (2) resolution is chosen at render
time with a Preview/Standard/Full tier (`ui/app.js`'s `RESOLUTION_MAP`), defaulting to Preview (640x360)
so the normal iterate-on-transitions loop stays fast — Full/1080p is opt-in for the render you're keeping.

**Browsers probe video files with `HEAD` before the ranged `GET`.** This was invisible until actually
tested in a browser: a `<video>` element failed to load at all — not slowly, not with a visible error, just
stuck at `readyState 0` — because the file server only handled `GET` and returned a bare method-not-allowed
for `HEAD`, which Chrome's media pipeline treats as fatal rather than falling back. `files.workflow.json`
now registers a second webhook trigger for `HEAD` on the same path. `render.py` also now writes with
`-movflags +faststart` so metadata is readable from the front of the file regardless. If you swap in a
different file-serving approach (e.g. nginx in front of `data/`), both of these still apply — a static file
server handles `HEAD` and range requests correctly out of the box, which is one more reason to consider
that over routing video bytes through an n8n webhook response node long-term.
