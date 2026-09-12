# Self-hosted n8n on a VM (Docker)

This replaces `docs/deployment.md`'s original bare-metal/npm assumptions with a concrete, Docker-based
setup — the realistic way almost anyone self-hosts n8n today, and the only way to cleanly bundle ffmpeg/
poppler-utils/python3 alongside it without touching a shared host.

## 1. Provision a VM

Any provider works — DigitalOcean, Hetzner, AWS Lightsail, a spare box. Sizing: n8n itself is light, but
`moviepy` rendering is CPU-bound (measured ~100s for a trivial 1080p clip — see `ui/README.md`), so **2
vCPUs / 4GB RAM minimum**, more if you'll run renders concurrently. Ubuntu 22.04/24.04 LTS is the easiest
target for the commands below.

(DigitalOcean also has an official one-click n8n Marketplace image, pre-wired with Postgres and HTTPS. It's
a reasonable starting point if you'd rather begin from that and layer the custom Docker image below on top
of it — but its internal layout isn't something I can give exact commands for sight unseen. The plain-VM
path below is fully concrete and gets you the same end state either way.)

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in for the group change to take effect
```

## 3. Clone the repo and start n8n

```bash
git clone https://github.com/bterhart/medweight-shorts-creator.git /opt/medweight-shorts-creator
cd /opt/medweight-shorts-creator/deploy
docker compose up -d --build
```

The first `--build` bakes ffmpeg/poppler-utils/python3+moviepy into the image per `deploy/Dockerfile` — takes
a few minutes. n8n comes up on port `5678`.

Confirm it's actually usable, not just running:

```bash
docker compose exec n8n sh -c 'ffmpeg -version && pdftoppm -v && python3 -c "import moviepy; print(moviepy.__version__)"'
docker compose exec n8n pwd   # must print /repo
```

## 4. Open n8n and finish setup

Visit `http://<your-vm-ip>:5678`, create the owner account n8n asks for on first run, then follow
`docs/deployment.md` sections 4–6 exactly as written (import the 4 workflow JSON files and activate them,
create the 3 credentials, point the UI's Settings at this instance's webhook base URL) — those steps are
identical regardless of how n8n itself is hosted.

## 5. Before this is anything but a test

`N8N_SECURE_COOKIE=false` in `docker-compose.yml` and plain HTTP on port 5678 are fine for kicking the
tires from your own machine, not for anything real. Put a reverse proxy (Caddy or nginx) in front with a
real TLS certificate, remove `N8N_SECURE_COOKIE=false` once that's in place, and see `docs/deployment.md`
section 7 for the auth gap on the webhook/file-serving endpoints themselves — hosting n8n yourself doesn't
fix that part.

## Updating later

```bash
cd /opt/medweight-shorts-creator
git pull
cd deploy
docker compose up -d --build   # rebuilds only if Dockerfile/requirements.txt changed
```

n8n's own workflows/credentials/execution history live in the `n8n_data` named volume, separate from the
git-tracked repo — a `git pull` never touches them, and they survive `docker compose up` restarts.
