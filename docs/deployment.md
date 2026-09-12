# Deployment — what goes where

This assumes a self-hosted n8n instance (npm/binary install or a systemd service — not the Docker image,
which needs extra volume/working-directory setup this guide doesn't cover). n8n's `Execute Command` and
`Read/Write Files from Disk` nodes operate on paths **relative to n8n's own process working directory**,
so the whole point of this guide is: n8n must be started with this repo as its working directory.

## 1. Prerequisites on the n8n host

```bash
# Node.js + n8n itself (skip if n8n is already installed/running)
npm install -g n8n

# Runtime deps the workflows shell out to
apt-get install -y ffmpeg poppler-utils   # poppler-utils = pdftoppm, pdftotext, pdfinfo

# Python deps for the render script
pip install -r render/requirements.txt    # moviepy, numpy, pillow
```

Verify each of `ffmpeg`, `pdftoppm`, `pdftotext`, `pdfinfo`, `python3` is on `PATH` for whatever user/service
will run n8n — `which ffmpeg` etc. If n8n runs as a different user (e.g. a `n8n` service account), install
the Python deps for *that* user or in a venv on *that* user's `PATH`, not just your own login shell.

## 2. Where the repo goes

Clone it to a fixed, permanent path on the n8n host — this guide uses `/opt/medweight-shorts-creator`, but
any path is fine as long as it matches step 3 exactly:

```bash
git clone https://github.com/bterhart/medweight-shorts-creator.git /opt/medweight-shorts-creator
mkdir -p /opt/medweight-shorts-creator/data/jobs
```

Nothing further needs to be copied anywhere else — `render/render.py` stays in the repo, and n8n's
`Execute Command` nodes invoke it via the relative path `render/render.py`, which only resolves correctly
if n8n's working directory *is* `/opt/medweight-shorts-creator`. That's the one fact this entire guide
hinges on:

| Path used in the workflow JSON | Resolves to |
|---|---|
| `render/render.py` | `/opt/medweight-shorts-creator/render/render.py` |
| `data/jobs/<jobId>/…` | `/opt/medweight-shorts-creator/data/jobs/<jobId>/…` |

`data/` is created at runtime and holds every job's uploaded files, extracted slides, audio, and rendered
video — it's disk state, not code, so it's `.gitignore`d, not committed.

## 3. Start n8n with this repo as its working directory

Two required environment variables: `NODE_FUNCTION_ALLOW_BUILTIN=crypto` (several Code nodes use
`require('crypto')` to generate job IDs) — some n8n versions also need `path` in that list
(`crypto,path`) if a Code node's `require('path')` call is rejected; try without it first.

**Plain process / screen / tmux (quickest to verify things work):**
```bash
cd /opt/medweight-shorts-creator
NODE_FUNCTION_ALLOW_BUILTIN=crypto n8n start
```

**systemd service (recommended for anything left running):**
```ini
# /etc/systemd/system/n8n.service
[Unit]
Description=n8n
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/medweight-shorts-creator
Environment=NODE_FUNCTION_ALLOW_BUILTIN=crypto
ExecStart=/usr/bin/n8n start
Restart=on-failure
User=n8n

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now n8n
```

If n8n was already running before you set `WorkingDirectory`, its working directory won't retroactively
change — restart it after this config change, and confirm with `ls -la /proc/$(pgrep -f 'n8n start')/cwd`
that it now points at `/opt/medweight-shorts-creator`.

## 4. Import the workflows

In the n8n UI: **Workflows → Import from File**, once for each of:
- `n8n/phase1-prepare.workflow.json`
- `n8n/render-trigger.workflow.json`
- `n8n/status.workflow.json`
- `n8n/files.workflow.json`

Toggle each one **Active** (top-right switch on the workflow canvas) — inactive workflows' webhooks only
respond on the `/webhook-test/...` URL for manual test runs from the editor, not the real `/webhook/...`
path the UI calls.

## 5. Credentials

Create these in n8n's **Credentials** section (left sidebar → Credentials → Add Credential), named
**exactly** as below so the imported workflows' credential references resolve without editing any JSON:

| Credential name | Type | What to enter |
|---|---|---|
| `Manus API` | Header Auth | Name: `x-manus-api-key`, Value: your Manus key |
| `ElevenLabs API` | Header Auth | Name: `xi-api-key`, Value: your ElevenLabs key |
| `Anthropic API` | Header Auth | Name: `x-api-key`, Value: your Anthropic key |

(`x-manus-api-key` is confirmed from Manus's own PHP SDK source — the docs site itself was unreachable
from where I built this. `xi-api-key` and `x-api-key` are ElevenLabs' and Anthropic's standard documented
header names respectively.) Use whatever keys you're issuing after rotating the ones pasted earlier in
this conversation — never keys that have appeared in a chat transcript.

**Base URL confirmed:** `https://api.manus.ai`, per Manus's own docs.

**File upload confirmed and fixed** against the real `file.upload` OpenAPI spec — it's a two-step flow,
not the one-shot multipart POST originally guessed: `POST /v2/file.upload` with just `{"filename": "..."}`
creates a file record and returns a presigned `upload_url` (expires in 3 minutes), then the actual image
bytes go in a separate `PUT` to that URL. The response wraps everything as
`{"ok": true, "request_id": "...", "file": {"id": "...", ...}, "upload_url": "...", ...}` — note `file.id`
is nested, not a flat `file_id`. `phase1-prepare.workflow.json`'s upload nodes (`Create File Record` →
`Merge Upload Url With Image` → `Upload File Bytes` → `Tag Upload Result`) now match this exactly. The
spec also states *every* v2 endpoint uses this `{ok, request_id, ...}` / `{ok:false, error:{code,message}}`
envelope, which almost certainly extends to task creation and polling too.

**Still needed:** the `task.create` and `task.get` (or equivalent status-polling) doc pages — same format
as the file.upload one pasted above (the OpenAPI YAML block is exactly what's useful). `Create Manus
Alignment Task`, `Get Manus Task`, and `Parse Alignment Result` in `phase1-prepare.workflow.json` still
assume unconfirmed field names (`task_id`, `structured_output_schema`, `agent_status: "stopped"` as the
completion signal) drafted from third-party summaries, not Manus's own reference. Given the envelope
pattern just confirmed, expect the real shape to wrap the task under a `task` key the same way `file.upload`
wraps under `file` — but that's an inference, not a read, so send the actual spec before relying on it.

## 6. The UI

`ui/` is static files — no build step, no server-side logic of its own. Serve it any way you like:

```bash
# quickest way to get it running for a test
cd /opt/medweight-shorts-creator/ui
python3 -m http.server 8080
```

For anything longer-lived, an nginx `server` block pointing its root at `ui/` works fine. Whatever serves
it, open it in a browser, click **Settings**, and set the webhook base URL to your n8n instance's webhook
prefix:

```
https://<your-n8n-host>/webhook
```

(No trailing slash — the UI appends `/jobs`, `/jobs/:id/status`, etc. itself.) That's `/webhook/...`, not
`/webhook-test/...` — the latter only works for one manual run at a time from inside the n8n editor.

## 7. Before you expose this to the internet

Both the job-creation webhook and the file server currently have **no authentication** — anyone who can
reach the URL can upload files and read anything under `data/jobs/`. Fine for testing on localhost or a
private network; before putting the n8n host's webhook URL on the public internet, put it behind at least
basic auth (nginx `auth_basic` in front of the webhook paths, or n8n's own webhook authentication options)
or keep it on a VPN/private network.

## 8. Verifying it actually works, in order

1. `curl -s -X POST https://<n8n-host>/webhook/jobs -F params='{"targetDurationSeconds":45,"narrationStyle":"","voice":{"mode":"preset","presetVoiceId":"21m00Tcm4TlvDq8ikWAM"},"transition":{"type":"cut"},"srtFilename":"t.srt","pdfs":[{"filename":"d.pdf","role":"primary","order":0,"binaryKey":"pdf_0"}]}' -F srt=@sample.srt -F pdf_0=@sample.pdf` — should return `{"jobId": "...", "phase": "prepare", ...}` immediately. If this hangs or 404s, the workflow isn't active or the path is wrong before anything else matters.
2. `curl https://<n8n-host>/webhook/jobs/<jobId>/status` — should return the job JSON, `phase` advancing over time. If `extracting_pdfs` never advances, check `pdftoppm`/`pdftotext`/`pdfinfo` are on PATH for the n8n process's user.
3. Once `phase: "ready_for_render"`, open the UI and confirm the review screen shows real slide thumbnails and playable audio (proof the `files` webhook and its `HEAD`/Range handling work).
4. Click Render (Preview quality first) and confirm it completes in seconds, not minutes — if it's slow even at 640x360, something's off (check `data/jobs/<jobId>/render.log`, which `render-trigger`'s background process writes to).
