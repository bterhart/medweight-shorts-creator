# Deployment (cPanel / CloudLinux Python App)

Written for the setup confirmed from the existing medweight.ca Twilio webhook app: cPanel with CloudLinux's
"Setup Python App" (Passenger/WSGI), MySQL available, `/home/medweight/...` as the home directory
convention. Adjust paths if your actual layout differs.

## 1. Get the code onto the server

```bash
cd /home/medweight
git clone https://github.com/bterhart/medweight-shorts-creator.git chatbot-shorts
```

## 2. Create the Python App in cPanel

cPanel → **Setup Python App** → Create Application:
- Application root: `chatbot-shorts/backend`
- Application URL: whatever path/subdomain you want this served at (e.g. `chatbot-shorts` → `medweight.ca/chatbot-shorts`)
- Application startup file: `app_entry.py`
- Application Entry point: `application`

**Do not name the startup file `passenger_wsgi.py`.** cPanel generates its own file with that exact name at
the Application Root — a stub that does `imp.load_source('wsgi', '<startup file>')` to load whatever you
point it at. If the startup file is also named `passenger_wsgi.py`, that stub loads itself, which loads
itself, forever, until Python raises `RecursionError: maximum recursion depth exceeded` and Passenger fails
to start with no useful log output at first glance. `app_entry.py` (this repo's actual entry file) sidesteps
the collision entirely.

cPanel provisions a virtualenv and generates its own wrapper script (the same shape as the existing Twilio
app's) — you don't write that part by hand. It also prints the exact `pip install` command for that
virtualenv; use it (not a bare system `pip3 install`) for the next step:

```bash
# cPanel shows you the real path; it looks like this:
source /home/medweight/virtualenv/chatbot-shorts/backend/3.9/bin/activate
pip install -r /home/medweight/chatbot-shorts/backend/requirements.txt
```

## 3. Edit `app_entry.py` for your actual path

`backend/app_entry.py` hardcodes `/home/medweight/chatbot-shorts` — update both `sys.path.insert` lines if
your clone lives somewhere else, matching the existing Twilio app's wrapper script convention.

## 4. Create the database

```bash
mysql -u root -p -e "
CREATE DATABASE chatbot_shorts;
CREATE USER 'chatbot_shorts'@'localhost' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL ON chatbot_shorts.* TO 'chatbot_shorts'@'localhost';
"
mysql -u chatbot_shorts -p chatbot_shorts < /home/medweight/chatbot-shorts/backend/schema.sql
```

(Or use cPanel's MySQL Databases UI instead of the CLI — same end state. `MedWeight MySQL` already exists
per your n8n credentials; either reuse that instance with a new database, or create a dedicated one. A
separate database is cleaner isolation, but either works — `backend/config.py` just needs whichever
host/user/password/database you choose.)

## 5. Configure secrets

```bash
cp /home/medweight/chatbot-shorts/backend/.env.example /home/medweight/chatbot-shorts/backend/.env
```

Fill in `.env`: DB credentials from step 4, plus `MANUS_API_KEY` / `ELEVENLABS_API_KEY` /
`ANTHROPIC_API_KEY` (use freshly rotated keys — never ones that have appeared in a chat transcript).
`.env` is gitignored; it never gets committed.

## 6. Restart the app and smoke-test it

cPanel's Python App page has a **Restart** button (touches a `tmp/restart.txt` file Passenger watches).
Then:

```bash
curl -s https://medweight.ca/chatbot-shorts/jobs/nonexistent/status
# expect: {"error": "not found"} with a 404 - confirms the Flask app is actually running
```

## 7. Set up the cron worker

cPanel → **Cron Jobs** → Add New Cron Job, every minute:

```
* * * * * /home/medweight/virtualenv/chatbot-shorts/backend/3.9/bin/python3 /home/medweight/chatbot-shorts/backend/worker.py >> /home/medweight/chatbot-shorts/worker.log 2>&1
```

Use the **virtualenv's** python (the path cPanel showed you in step 2), not the system `python3` — the
venv is where `requirements.txt` actually got installed.

## 8. Point the UI at it

Serve `ui/` however you like (it's static files — could even be another cPanel subdomain, or served from
your own machine while testing). Open it, click **Settings**, set the webhook base URL to wherever the
Flask app is reachable — e.g. `https://medweight.ca/chatbot-shorts` (no trailing slash; `app.js` appends
`/jobs`, `/files`, etc. itself).

## 9. Verifying a real end-to-end run

1. Submit a job through the UI (or `curl -F params=... -F srt=@... -F pdf_0=@...` directly against
   `POST /chatbot-shorts/jobs`) — should get `{"jobId": ..., "phase": "prepare", "step": "saving_inputs"}`
   back immediately.
2. Watch `worker.log` — the next cron tick (within a minute) should print `claimed job <id> (phase=prepare)`
   and start working through it. If it never claims anything, check the cron job actually points at the
   venv's Python and that `backend/.env` has real DB credentials.
3. Poll `GET /chatbot-shorts/jobs/<id>/status` (or just watch the UI) until `phase` reaches
   `ready_for_review` or `failed`. A `failed` phase's `error.detail` field has the full Python traceback —
   that's the first place to look. `job.narration` at this point is permanent - nothing later ever rewrites
   it in place.
4. From `ready_for_review`, create a short via `POST /chatbot-shorts/jobs/<id>/shorts` (UI: the "Shorts"
   panel in Step 3) with a required `topic`, target duration, and voice - this writes one coherent condensed
   script from the full narration (not a per-slide shrink), grounds it onto whichever original slides it
   actually covers, resolves the voice, and synthesizes audio via the next cron tick(s), same claim/process
   pattern as step 2. A job can hold any number of shorts; only one may be mid-pipeline at a time.
5. Once that short reaches `ready_for_render`, trigger its render (UI button on the short's card, or
   `POST /chatbot-shorts/jobs/<id>/shorts/<shortId>/render`) and watch the next cron tick pick it up the
   same way. That endpoint rejects a short not yet at `ready_for_render`, and rejects any request while
   another short on the same job is still mid-pipeline.

## Before this is anything but a test

Both `POST /jobs` and `GET /files` are unauthenticated right now — anyone who can reach the URL can submit
jobs and read anything under `data/jobs/`. Fine while testing on a URL nobody else knows; add real access
control (HTTP Basic Auth via `.htaccess`, or a shared-secret header the UI sends and Flask checks) before
this is anywhere someone might stumble onto it.
