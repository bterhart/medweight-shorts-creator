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
3. `alignment.align_job` (`backend/alignment.py`) — one Claude vision request per job: the whole
   transcript as numbered cues plus every slide as an image (re-rendered at 96 DPI for the request) with
   its extracted text. The model returns, in narrative order, the inclusive cue range each slide was on
   screen for; `transcriptExcerpt` is then sliced from the SRT, so it's exact rather than model-written,
   and the reply is a few hundred tokens instead of a re-emitted transcript. Official `anthropic` SDK:
   streaming (the image payload is large), structured output (`output_config.format` with a JSON schema,
   so the reply is valid by construction), adaptive thinking at `ALIGNMENT_EFFORT`, and server-side
   refusal fallbacks (`fallbacks: "default"`) so a benign deck tripping a safety classifier is re-run on
   Anthropic's recommended substitute instead of failing the job. A deck over `MAX_SLIDES_PER_REQUEST`
   (60) is split across requests that each still get the full transcript, and the results are merged by
   cue order - there is never a transcript window.
4. `clean_narration` — concurrent Anthropic Messages API calls (groups of `CLEAN_SEGMENTS_PER_CALL`
   segments, `CLEAN_CONCURRENCY` at a time - every excerpt is cleaned independently, so the split is
   lossless) producing `job.narration`: the full 1:1 slide-to-narration alignment, cleaned of
   filler/personal references, sized to nothing. Splitting also keeps each call well under `max_tokens`;
   a reply that does hit it is now a hard error rather than a silently truncated narration. Permanent
   once written - nothing later ever mutates it in place.
5. `build_short` — a separate, explicitly-triggered step (`POST /jobs/<id>/shorts`, not part of the
   automatic prepare flow): one Anthropic Messages API call that writes a single coherent, duration-targeted
   narrative from the full `job.narration` pool (not a per-slide shrink) and grounds it back onto whichever
   original slides it actually covers - via whichever `narration_prompts` entry the Create-short form chose
   (stored on the short as `prompt.id`/`name`). Shorts created before that picker existed fall back to the
   entry named "Second pass (CBT/MI/ACT/DBT narration)". A job can hold any number of these.

`worker.py` prints each step's wall time to `worker.log` (`parse_srt`, `upload_slides_to_manus`,
`run_alignment`, `clean_narration`, `build_short`, `synthesize_audio`, dispatch, and render duration), so a
slow run says where the minutes went instead of that being reconstructed after the fact.
6. `resolve_voice` / `synthesize_audio` — ElevenLabs preset or Voice Design (cached by description hash in
   `DATA_DIR/voice-cache.json`, same as before) and per-segment TTS, with duration read via
   `moviepy.AudioFileClip` instead of shelling out to `ffprobe` separately — one less external dependency,
   since `moviepy` (needed for rendering anyway) already resolves its own `ffmpeg` via the pip-installable
   `imageio-ffmpeg`, which also means **no system `ffmpeg` install is needed either.**

Between PyMuPDF and `imageio-ffmpeg`, the entire pipeline needs zero system packages beyond Python itself
and `pip install -r backend/requirements.txt` — a direct fix for not knowing whether the target host allows
system-level installs.

## Why alignment moved off Manus

Manus is an autonomous-agent platform; alignment is a single act of judgment over the deck and the talk.
Every cost in the old design came from that mismatch: minutes of agent runtime per chunk, a confirmed
~5k-token cap on message text that forced transcript *windows*, a confirmed file-attachment ceiling that
forced 12-slide *chunks*, a window heuristic that could strand a slide's narration across a chunk
boundary, and `waiting`/404 states the pipeline couldn't answer. Claude's 1M-token context takes the whole
deck and the whole transcript in one request, so all of that machinery - and the quality loss it implied -
is simply gone. The old `manus` job field is left in the schema as legacy for jobs aligned before this.
Manus's real strengths (browsing, multi-step tool use, producing artifacts) belong in optional enrichment
steps off the critical path - sourcing images, fact-checking claims against sources, publishing - not in
the prepare pipeline.

## What's confirmed vs. still assumed

The Manus-based pipeline ran end to end live. The Claude alignment step replaces it and is unverified
against a real deck as of this change - the `worker.log` line for `align_job` (wall time and the request's
input/output token counts) is the first thing to read on the next run, and `ALIGNMENT_EFFORT` is the
lever if it is slow (`medium`) or a deck aligns poorly (`xhigh`). ElevenLabs (`text-to-speech`,
`text-to-voice/design`) and the two remaining plain-HTTP Anthropic calls (`clean_narration`,
`build_short`) have been exercised live.
