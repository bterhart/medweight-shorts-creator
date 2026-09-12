# Phase 2 (Render Trigger) — design notes

`render-trigger.workflow.json` fires a render for an already-prepared job. It's async by design: the
webhook acks in milliseconds regardless of how long the actual render takes, because it backgrounds the
render process rather than waiting on it.

## Why it has to be async

`render.py` at 1920x1080 took ~100s to render a trivial 3-segment, 7.5s test clip (real measurement, not
an estimate — see `ui/README.md`). A full 3-5 minute video with many more segments could take several
minutes. A synchronous webhook response blocking on that risks n8n's webhook timeout outright, so instead:

1. **Build Render Command** — builds the `render/render.py` CLI invocation from the request body
   (transition overrides, `--resolution` if given).
2. **Read Job For Marking → Mark Rendering → Save Rendering State** — reads `job.json`, sets
   `phase: "rendering"`, writes it back. This is what makes the status endpoint immediately reflect
   "rendering" rather than showing stale `ready_for_render` state while the UI polls.
3. **Launch Render Background** — runs the command via `nohup … & disown`, so the Execute Command node
   returns as soon as the shell backgrounds the process, not when the process exits.
4. **Respond Render Started** — acks with `{jobId, phase: "rendering", step: "rendering"}` immediately.

The UI then polls the *existing* `status.workflow.json` endpoint (same one Phase 1 uses) until
`phase: "done"`, exactly mirroring the Phase 1 prepare-and-poll pattern. No new status endpoint was needed.

## Bug this surfaced when actually tested (not caught by review)

`render.py`'s own guard originally only allowed running against a job at `phase in
("ready_for_render", "done")`. Once step 2 above started writing `phase: "rendering"` to `job.json`
*before* launching the script, every render call immediately self-rejected: *"Job X is not ready for
render (phase=rendering)."* Fixed by adding `"rendering"` to the allowed phases in `render.py` — it's a
legitimate transient state the trigger itself writes, not an invalid one. Caught by actually running the
new async flow end-to-end against the real script (via the same Playwright+mock-backend harness described
in `ui/README.md`), not by reading the diff.

A second, unrelated race was found the same way: the UI could poll `GET /jobs/:jobId/status` immediately
after receiving Phase 1's ack and get a 404, because `Ack Job` and the first `job.json` checkpoint write
(`Prepare CP0` → `Save CP0`) were on parallel branches with no ordering guarantee — and `Save CP0` itself
raced against `Ensure Job Dirs`' `mkdir`. Fixed by re-sequencing: `Build Job Record` → `Ensure Job Dirs` →
(`Fan Out Input Files` for the main pipeline, and `Prepare CP0` → `Save CP0` → `Ack Job` for the response).
The client now never receives a `jobId` before `job.json` exists on disk for it.

## Resolution is a render-time choice, not an intake-time one

Resolution/aspect ratio don't affect alignment, narration, or voice — only the final render — so the UI
asks for them in the **Review** step (Phase 2), not **Setup** (Phase 1), via an aspect-ratio dropdown and
a Preview/Standard/Full quality tier that map to actual dimensions (`ui/app.js`'s `RESOLUTION_MAP`).
Default is `preview` (640x360 for 16:9), specifically so the fast iterate-on-transitions loop stays fast;
`full` (1080p) is opt-in for the render you're actually keeping. Phase 1's `job.params.resolution` is now
just a fallback (defaults to `640x360`) for the rare case a render is triggered without an explicit
override.
