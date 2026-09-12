# Deployment (cPanel / CloudLinux Python App)

Written for the setup confirmed from the existing medweight.ca Twilio webhook app: cPanel with CloudLinux's
"Setup Python App" (Passenger/WSGI), MySQL available, `/home/medweight/...` as the home directory
convention. Adjust paths if your actual layout differs.

## 1. Get the code onto the server

```bash
cd /home/medweight
git clone https://github.com/bterhart/medweight-shorts-creator.git waterway-narrator
```

## 2. Create the Python App in cPanel

cPanel → **Setup Python App** → Create Application:
- Application root: `waterway-narrator/backend`
- Application URL: whatever path/subdomain you want this served at (e.g. `waterway` → `medweight.ca/waterway`)
- Application startup file: `wsgi.py`
- Application Entry point: `application`

cPanel provisions a virtualenv and generates its own wrapper script (the same shape as the existing Twilio
app's) — you don't write that part by hand. It also prints the exact `pip install` command for that
virtualenv; use it (not a bare system `pip3 install`) for the next step:

```bash
# cPanel shows you the real path; it looks like this:
source /home/medweight/virtualenv/waterway-narrator/backend/3.9/bin/activate
pip install -r /home/medweight/waterway-narrator/backend/requirements.txt
```

## 3. Edit `wsgi.py` for your actual path

`backend/wsgi.py` hardcodes `/home/medweight/waterway-narrator` — update both `sys.path.insert` lines if
your clone lives somewhere else, matching the existing Twilio app's wrapper script convention.

## 4. Create the database

```bash
mysql -u root -p -e "
CREATE DATABASE waterway_narrator;
CREATE USER 'waterway'@'localhost' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL ON waterway_narrator.* TO 'waterway'@'localhost';
"
mysql -u waterway -p waterway_narrator < /home/medweight/waterway-narrator/backend/schema.sql
```

(Or use cPanel's MySQL Databases UI instead of the CLI — same end state. `MedWeight MySQL` already exists
per your n8n credentials; either reuse that instance with a new database, or create a dedicated one. A
separate database is cleaner isolation, but either works — `backend/config.py` just needs whichever
host/user/password/database you choose.)

## 5. Configure secrets

```bash
cp /home/medweight/waterway-narrator/backend/.env.example /home/medweight/waterway-narrator/backend/.env
```

Fill in `.env`: DB credentials from step 4, plus `MANUS_API_KEY` / `ELEVENLABS_API_KEY` /
`ANTHROPIC_API_KEY` (use freshly rotated keys — never ones that have appeared in a chat transcript).
`.env` is gitignored; it never gets committed.

## 6. Restart the app and smoke-test it

cPanel's Python App page has a **Restart** button (touches a `tmp/restart.txt` file Passenger watches).
Then:

```bash
curl -s https://medweight.ca/waterway/jobs/nonexistent/status
# expect: {"error": "not found"} with a 404 - confirms the Flask app is actually running
```

## 7. Set up the cron worker

cPanel → **Cron Jobs** → Add New Cron Job, every minute:

```
* * * * * /home/medweight/virtualenv/waterway-narrator/backend/3.9/bin/python3 /home/medweight/waterway-narrator/backend/worker.py >> /home/medweight/waterway-narrator/worker.log 2>&1
```

Use the **virtualenv's** python (the path cPanel showed you in step 2), not the system `python3` — the
venv is where `requirements.txt` actually got installed.

## 8. Point the UI at it

Serve `ui/` however you like (it's static files — could even be another cPanel subdomain, or served from
your own machine while testing). Open it, click **Settings**, set the webhook base URL to wherever the
Flask app is reachable — e.g. `https://medweight.ca/waterway` (no trailing slash; `app.js` appends `/jobs`,
`/files`, etc. itself).

## 9. Verifying a real end-to-end run

1. Submit a job through the UI (or `curl -F params=... -F srt=@... -F pdf_0=@...` directly against
   `POST /waterway/jobs`) — should get `{"jobId": ..., "phase": "prepare", "step": "saving_inputs"}` back
   immediately.
2. Watch `worker.log` — the next cron tick (within a minute) should print `claimed job <id> (phase=prepare)`
   and start working through it. If it never claims anything, check the cron job actually points at the
   venv's Python and that `backend/.env` has real DB credentials.
3. Poll `GET /waterway/jobs/<id>/status` (or just watch the UI) until `phase` reaches `ready_for_render` or
   `failed`. A `failed` phase's `error.detail` field has the full Python traceback — that's the first place
   to look.
4. Once `ready_for_render`, trigger a render (UI button, or `POST /waterway/jobs/<id>/render`) and watch
   the next cron tick pick it up the same way.

## Before this is anything but a test

Both `POST /jobs` and `GET /files` are unauthenticated right now — anyone who can reach the URL can submit
jobs and read anything under `data/jobs/`. Fine while testing on a URL nobody else knows; add real access
control (HTTP Basic Auth via `.htaccess`, or a shared-secret header the UI sends and Flask checks) before
this is anywhere someone might stumble onto it.
