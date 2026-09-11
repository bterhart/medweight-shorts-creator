# Phase 1 (Prepare) — design notes

Phase 1 covers everything up to the "ready for review" checkpoint: intake, PDF→image/text extraction,
Manus vision alignment, narration condensation, voice resolution, and TTS synthesis. It stops **before**
any video is rendered — that's Phase 2 (a separate, repeatable, local-only workflow, not yet built).

## Job model = files on disk, not a database

`job.json` (validated by `schema/job.schema.json`) is the single source of truth for one job, and it lives
at `data/jobs/<jobId>/job.json` alongside everything else the job produces:

```
data/jobs/<jobId>/
  job.json
  input/
    transcript.srt
    pdf-1.pdf            # role: primary or supplementary, per job.json
    pdf-2.pdf
  slides/
    pdf-1-p001.png  pdf-1-p001.txt
    pdf-1-p002.png  pdf-1-p002.txt
    ...
  audio/
    seg-000.mp3
    seg-001.mp3
    ...
  output/
    video.mp4           # written by Phase 2, not Phase 1
```

No Postgres/Airtable/etc. needed for v1 — the status-polling endpoint (`n8n/status.workflow.json`) just
reads `job.json` back off disk. This also means the whole job folder is the unit of "resume/redo": Phase 2
re-renders by reading the same folder, and re-running just the render step never touches Manus or
ElevenLabs again.

## Why every node re-reads `job` via `$('Node Name')` instead of passthrough

Several n8n node types (Execute Command, HTTP Request, Read/Write Files) either replace `json` entirely
with their own output or have passthrough behavior that varies by version. Rather than depend on that,
every Code node in `phase1-prepare.workflow.json` pulls the current `job` object explicitly from the last
node that actually mutated it (e.g. `$('Parse Alignment Result').item.json.job`), and HTTP/Execute Command
node parameters do the same in their expressions. This makes the graph robust regardless of passthrough
quirks, at the cost of being more verbose than a naive linear chain. Checkpoint writes (`Prepare CPx` /
`Save CPx` pairs) are pure side branches — nothing downstream depends on them, they exist only so the
status endpoint has fresh data mid-run.

## Pipeline order (why alignment happens before condensing)

Manus aligns the **full, uncut transcript** against the slide images first (`alignment[]`): which slide(s),
in what order, map to which excerpt. Only after that does an LLM call condense each aligned excerpt down to
fit the target duration (`narration[]`), proportioned by each excerpt's share of the original transcript.
Condensing first would throw away the content Manus needs to align accurately, and would give no way to
decide which slides to drop (title/agenda slides with nothing narrated over them).

## Vision alignment via Manus

Each slide PNG is uploaded to Manus's Files API individually (`Upload Slide Image`) to get a `file_id`,
then all `file_id`s are attached to a single alignment task (`Create Manus Alignment Task`) along with the
full transcript text and per-slide `primary`/`supplementary` role tags, requesting a
`structured_output_schema` JSON result rather than prose. This means Manus is reasoning over the actual
slide images, not just OCR'd text — it can align narration to a chart or photo that carries little text.
Per-page `pdftotext` output is extracted too (`slides/*.txt`) but isn't currently sent to Manus in this
draft; wiring it in as extra context per attachment is a reasonable next step if alignment quality on
text-heavy slides needs improvement.

The task is async: `Wait Before Poll` → `Get Manus Task` → `Is Manus Task Done` loops until
`agent_status: "stopped"`, per Manus's documented pattern. Manus also supports webhook callbacks as an
alternative to polling — worth switching to once this is running for real, since it removes the polling
loop entirely.

## Primary vs. supplementary PDFs

`job.sources.pdfs[].role` is set from what the UI submits (exactly one `primary`, enforced client-side).
The alignment prompt explicitly instructs Manus to build the main sequence from the primary deck and only
pull from supplementary decks to cover content the primary deck doesn't show — this also resolves
duplicate-slide ties (e.g. a repeated title slide across two decks) in the primary deck's favor.

## What's solid vs. what needs verification against live accounts

`open.manus.ai` was unreachable from this sandbox (network egress block), so the Manus HTTP nodes are
drafted from third-party doc summaries and search results, not a first-hand spec read. Before running this
for real:

| Area | Status | What to check |
|---|---|---|
| Webhook intake, job record, file saving | Solid | Assumes the UI posts multipart fields `srt`, `pdf_0`/`pdf_1`/…, and a `body` JSON with `pdfs: [{filename, role, order, binaryKey}]` — adjust field names to match whatever the UI actually sends |
| SRT parsing, PDF→PNG/text extraction | Solid, but host-dependent | Requires `poppler-utils` (`pdftoppm`, `pdftotext`, `pdfinfo`) and `ffmpeg`/`ffprobe` installed on the n8n host, and `NODE_FUNCTION_ALLOW_BUILTIN=crypto` set so Code nodes can `require('crypto')` |
| Manus file upload / create task / poll / structured output | **Verify** | Exact endpoint paths, `attachments` shape, `structured_output_schema` field name, and the poll-completion field (`agent_status: "stopped"` per search results) should be confirmed against your Manus account's live docs/playground |
| Anthropic condensation call | Mostly solid | Response parsing assumes `response.content[0].text`, standard for the Messages API, but double check against current API version |
| ElevenLabs Voice Design / TTS | **Verify** | Endpoint paths and response field names (`voice_id`, binary response handling) should be confirmed against the ElevenLabs API reference you're on |
| `Read/Write Files from Disk` parameter names (`fileSelector` vs `fileName`, `dataPropertyName`) | **Verify** | These have shifted across n8n versions — re-check each node's Read/Disk tab after import |
| HTTP Request node body/response parameter shapes (`jsonBody`, multipart, binary response options) | **Verify** | Also version-dependent; the URL/method/payload intent is correct even if you need to re-click a field into place |
| `n8n/status.workflow.json` | Stub | The "job not found" path isn't wired to the read failure yet — add `continueOnFail`/error output on `Read job.json` before relying on it |

## Not yet built

- Phase 2 render workflow (the MoviePy script + Execute Command invocation)
- The UI itself
- Voice-description → `voice_id` cache (`data/voice-cache.json`) to avoid re-running Voice Design for a
  repeated custom description — mentioned in earlier design discussion, not yet implemented in this workflow
