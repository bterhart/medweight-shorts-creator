# Archive — abandoned n8n approach

The first working end-to-end build of this project used n8n for orchestration. It's kept here for
reference, not deleted, but it's not what's deployed or maintained going forward.

**Why abandoned**: the intended host turned out to be n8n Cloud (the managed SaaS product), which
permanently disables the `Execute Command` node and only lets `Read/Write Files from Disk` touch the same
server n8n itself runs on — both load-bearing for this project's PDF extraction and video rendering.
Beyond that specific incompatibility, building and debugging four hand-authored node-graph JSON files (see
`n8n/`) surfaced repeated friction that plain Python code doesn't have: multipart field parsing quirks,
ambiguous node-to-node data passthrough requiring workarounds, and needing an explicit env var
(`NODES_EXCLUDE=[]`) just to re-enable a core node on self-hosted n8n 2.0+.

The actual orchestration logic these workflows encoded — SRT/PDF parsing, the Manus/ElevenLabs/Anthropic
API calls (with their exact confirmed request/response shapes, learned the hard way against Manus's real
API), and the job-state machine — carried forward directly into `backend/`, which is what's actually run
now. `render/render.py` never needed to change at all; it was always plain Python.

- `n8n/` — the four workflow JSON files (Phase 1 prepare, Phase 2 render trigger, status, file server)
- `n8n-deploy/` — the Docker/docker-compose setup for self-hosting n8n, superseded by `backend/`'s much
  simpler deployment (a Flask WSGI app + a cron-invoked worker script, no message-broker or node-runtime
  needed)
