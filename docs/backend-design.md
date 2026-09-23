# Backend design

Replaces the earlier n8n-based approach (see `archive/`) with plain, testable Python: a small Flask app
for the HTTP endpoints `ui/` talks to, and a cron-invoked worker script that does the actual pipeline work.

## Why this shape

The intended host turned out to be n8n Cloud, which can't run `Execute Command` or touch a local
filesystem at all (see `archive/README.md`) — a hard incompatibility, not a tuning problem. Separately,
building the n8n version surfaced real friction (multipart parsing quirks, ambiguous node-to-node data
passthrough, an env var needed just to re-enable a core node) that plain Python code doesn't have. This
version is built for the actual target: a cPanel/CloudLinux Python App (Passenger/WSGI, confirmed from the
existing Twilio webhook app's entry script) with MySQL already available.

## Job state = a MySQL row, not a file

`backend/schema.sql` defines one `jobs` table: `id`, `phase`, `step` (duplicated as real columns for the
worker to query), and `data` (the full job record as JSON — same shape as the old `job.json`, validated by
`schema/job.schema.json`, unchanged). Uploaded/generated files (images, audio, video) still live on disk
under `DATA_DIR/jobs/<id>/`; only metadata moved into the database.

## Request/response split

Flask (`backend/app.py`) only ever does fast, synchronous work: validate the request, save uploaded files,
write a `queued` row, return the job ID — or read a row back for status. It never calls Manus/ElevenLabs/
Anthropic or runs ffmpeg itself, because Passenger-managed WSGI processes aren't a good place for
multi-minute work to run inline.

The actual pipeline runs in `backend/worker.py`, meant to be invoked by cron every minute:

```
* * * * * /path/to/python3 /path/to/backend/worker.py >> /path/to/worker.log 2>&1
```

Each invocation: acquire an exclusive file lock (so overlapping cron ticks can't run two workers at once —
simpler and safer on shared hosting than trying to bound per-job concurrency), atomically claim one job
that still needs work (`phase IN ('prepare', 'condensing', 'editing', 'rendering')`, skipping anything
another worker already holds a lease on), process it fully — which can take minutes, that's fine, cron
doesn't need it to finish before the next tick, the lock just makes the next tick a no-op until this one's
done — then exit. Neither `POST /jobs/<id>/shorts` nor `POST /jobs/<id>/shorts/<shortId>/render` does the
actual work inline; each just writes the request onto the named short (in `job.shorts`, with
`job.activeShortId` pointing at it) and flips `job.phase` to `condensing`/`rendering`, and the same worker
loop picks it up next. Only one short per job may be mid-pipeline at a time - `job.activeShortId` enforces
that at the API layer. The same pattern covers post-review segment edits that need ElevenLabs: `PATCH
.../segments/<seq>` (rewrite narration text) and `POST .../segments` (insert a new segment) both write
directly to `short.script`/`short.pendingSegments` and flip `job.phase` to `editing`, which
`worker.run_edit_pipeline` picks up and resolves back to `ready_for_review` - resynthesizing only the
segment(s) named in `pendingSegments`, never the whole short. Segment edits with no audio impact - `POST
.../segments/<seq>/image` (replace, from an upload or another deck slide) and `DELETE .../segments/<seq>` -
apply synchronously instead, the same fast/sync category as job creation and status polling.

## Pipeline steps (`backend/pipeline.py`)

Same steps as the old n8n workflow, now as plain functions the worker calls in order, saving the job back
to the DB after each one so status polls see live progress:

1. `parse_srt` — cue/duration/full-text extraction, unchanged logic.
2. `extract_pdf_slides` — **PyMuPDF (`pymupdf`/`fitz`) instead of poppler-utils.** Renders pages to PNG and
   pulls text directly, no system binary (`pdftoppm`/`pdftotext`) or root access required — deliberate,
   since root/system-package access on the target host isn't guaranteed.
3. `upload_slides_to_manus` / `run_alignment` — `backend/manus_client.py` implements the confirmed real
   Manus v2 API: `file.upload`'s two-step create-record-then-PUT flow, `task.create` with slide images as
   `file`-type parts mixed into `message.content` (not a separate `attachments` field), and polling via
   `task.listMessages` scanning for the newest `status_update` event's `agent_status`. All of this was
   verified against Manus's own OpenAPI specs during development, not third-party summaries.
4. `clean_narration` — one Anthropic Messages API call producing `job.narration`: the full 1:1
   slide-to-narration alignment, cleaned of filler/personal references, sized to nothing. Permanent once
   written - nothing later ever mutates it in place.
5. `build_short` — a separate, explicitly-triggered step (`POST /jobs/<id>/shorts`, not part of the
   automatic prepare flow): one Anthropic Messages API call that writes a single coherent, duration-targeted
   narrative from the full `job.narration` pool (not a per-slide shrink) and grounds it back onto whichever
   original slides it actually covers - always via the hardcoded "Second pass (CBT/MI/ACT/DBT narration)"
   prompt, looked up from `narration_prompts` by name. A job can hold any number of these.
6. `resolve_voice` / `synthesize_audio` — ElevenLabs preset or Voice Design (cached by description hash in
   `DATA_DIR/voice-cache.json`, same as before) and per-segment TTS, with duration read via
   `moviepy.AudioFileClip` instead of shelling out to `ffprobe` separately — one less external dependency,
   since `moviepy` (needed for rendering anyway) already resolves its own `ffmpeg` via the pip-installable
   `imageio-ffmpeg`, which also means **no system `ffmpeg` install is needed either.**

Between PyMuPDF and `imageio-ffmpeg`, the entire pipeline needs zero system packages beyond Python itself
and `pip install -r backend/requirements.txt` — a direct fix for not knowing whether the target host allows
system-level installs.

## The one hard constraint carried over unaddressed

Manus caps `message.content`'s combined text at ~5,000 estimated tokens, with no way around it by
splitting across parts (confirmed from their spec). `run_alignment` now truncates the transcript to
`MAX_TRANSCRIPT_CHARS` (16,000 chars, a conservative buffer under the token cap) rather than sending it
unbounded and getting an opaque `InvalidArgument` failure — an actual mitigation, not just a documented
gap, though truncation itself means alignment quality degrades for long source videos rather than the job
failing outright. Worth revisiting (real summarization instead of a hard cut) if long transcripts turn out
to be common.

## What's confirmed vs. still assumed

Everything Manus-related is now built against confirmed specs (pasted directly from Manus's own docs
during development): `file.upload`, `task.create`, `task.listMessages`, including the exact
`{ok, request_id, ...}` response envelope and the `waiting`/`error`/`stopped`/`running` state machine.
ElevenLabs (`text-to-speech`, `text-to-voice/design`) and Anthropic (`messages`) calls use standard,
well-documented conventions but haven't been verified against your specific accounts the way Manus has —
worth a real test run to confirm, same as the rest of this pipeline.
