// Chatbot Shorts UI — talks to backend/app.py:
//   POST {apiBase}/jobs                                  create a job
//   GET  {apiBase}/jobs/:jobId/status                    poll job status
//   POST {apiBase}/jobs/:jobId/shorts                    create a short from the job's permanent narration
//   POST {apiBase}/jobs/:jobId/shorts/:shortId/render    trigger that short's render
//   GET  {apiBase}/files?path=...                        slide images/audio

const STEP_ORDER = [
  ["queued", "Queued"],
  ["saving_inputs", "Saving uploaded files"],
  ["parsing_srt", "Parsing transcript"],
  ["extracting_pdfs", "Extracting slide images"],
  ["uploading_slides_to_manus", "Uploading slides to Manus"],
  ["aligning", "Aligning transcript to slides"],
  ["cleaning_narration", "Cleaning narration text"],
  ["ready_for_review", "Ready for review"],
];

// Resolution is chosen at render time (Phase 2), not at intake - it doesn't
// affect alignment/narration/voice, and defaulting low keeps iteration fast.
// "Full" (1080p) took ~100s for a trivial 3-segment clip in testing; pick it
// only for the render you're keeping.
const RESOLUTION_MAP = {
  "16:9": { preview: "640x360", standard: "1280x720", full: "1920x1080" },
  "9:16": { preview: "360x640", standard: "720x1280", full: "1080x1920" },
  "1:1": { preview: "480x480", standard: "720x720", full: "1080x1080" },
};

const state = {
  pdfs: [], // { file, filename, role: 'primary'|'supplementary' }
  jobId: null,
  pollTimer: null,
  currentJob: null,
};

function getApiBase() {
  return (localStorage.getItem("apiBase") || "").replace(/\/+$/, "");
}

function fileUrl(path) {
  return `${getApiBase()}/files?path=${encodeURIComponent(path)}`;
}

function $(id) { return document.getElementById(id); }

function showError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

// ---------- settings ----------
$("settings-toggle").addEventListener("click", () => {
  $("api-base").value = getApiBase();
  $("settings-panel").hidden = !$("settings-panel").hidden;
});
$("settings-save").addEventListener("click", () => {
  localStorage.setItem("apiBase", $("api-base").value.trim());
  $("settings-panel").hidden = true;
  validateSetup();
});

// ---------- SRT dropzone ----------
let srtFile = null;
setupDropzone($("srt-dropzone"), $("srt-input"), (files) => {
  if (!files.length) return;
  srtFile = files[0];
  $("srt-filename").textContent = srtFile.name;
  $("srt-filename").hidden = false;
  validateSetup();
});

// ---------- PDF dropzone ----------
setupDropzone($("pdf-dropzone"), $("pdf-input"), (files) => {
  for (const file of files) {
    state.pdfs.push({ file, filename: file.name, role: state.pdfs.length === 0 ? "primary" : "supplementary" });
  }
  renderPdfList();
  validateSetup();
});

function setupDropzone(zoneEl, inputEl, onFiles) {
  zoneEl.addEventListener("click", () => inputEl.click());
  inputEl.addEventListener("change", () => onFiles(Array.from(inputEl.files)));
  zoneEl.addEventListener("dragover", (e) => { e.preventDefault(); zoneEl.classList.add("dragover"); });
  zoneEl.addEventListener("dragleave", () => zoneEl.classList.remove("dragover"));
  zoneEl.addEventListener("drop", (e) => {
    e.preventDefault();
    zoneEl.classList.remove("dragover");
    onFiles(Array.from(e.dataTransfer.files));
  });
}

function renderPdfList() {
  const list = $("pdf-list");
  list.innerHTML = "";
  state.pdfs.forEach((pdf, i) => {
    const li = document.createElement("li");

    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "primary-pdf";
    radio.checked = pdf.role === "primary";
    radio.addEventListener("change", () => {
      state.pdfs.forEach((p) => (p.role = "supplementary"));
      pdf.role = "primary";
      renderPdfList();
    });

    const label = document.createElement("span");
    label.textContent = "Primary";
    label.style.fontSize = "0.8rem";

    const name = document.createElement("span");
    name.className = "filename";
    name.textContent = pdf.filename;

    const up = document.createElement("button");
    up.type = "button";
    up.textContent = "↑";
    up.disabled = i === 0;
    up.addEventListener("click", () => { [state.pdfs[i - 1], state.pdfs[i]] = [state.pdfs[i], state.pdfs[i - 1]]; renderPdfList(); });

    const down = document.createElement("button");
    down.type = "button";
    down.textContent = "↓";
    down.disabled = i === state.pdfs.length - 1;
    down.addEventListener("click", () => { [state.pdfs[i + 1], state.pdfs[i]] = [state.pdfs[i], state.pdfs[i + 1]]; renderPdfList(); });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "✕";
    remove.addEventListener("click", () => {
      const wasPrimary = pdf.role === "primary";
      state.pdfs.splice(i, 1);
      if (wasPrimary && state.pdfs.length) state.pdfs[0].role = "primary";
      renderPdfList();
      validateSetup();
    });

    li.append(radio, label, name, up, down, remove);
    list.appendChild(li);
  });
}

// ---------- voice preset/custom toggle (Step 3 - create a short) ----------
$("short-voice-preset").addEventListener("change", () => {
  $("short-voice-custom").hidden = $("short-voice-preset").value !== "custom";
});

// ---------- setup validation ----------
function validateSetup() {
  // apiBase may legitimately be empty (UI served from the same origin as the webhooks)
  const ok = Boolean(srtFile && state.pdfs.length > 0 && state.pdfs.some((p) => p.role === "primary"));
  $("submit-btn").disabled = !ok;
}

// ---------- submit (Phase 1) ----------
$("submit-btn").addEventListener("click", async () => {
  showError($("setup-error"), "");
  const params = {
    // Duration and voice have no control here - they're chosen per short,
    // later, in Step 3 (see create-short-btn below).
    narrationStyle: $("narration-style").value.trim(),
    transition: {
      type: $("transition-type").value,
      transitionSeconds: Number($("transition-seconds").value),
      minSlideSeconds: Number($("min-slide-seconds").value),
    },
    srtFilename: srtFile.name,
    pdfs: state.pdfs.map((p, i) => ({ filename: p.filename, role: p.role, order: i, binaryKey: `pdf_${i}` })),
  };

  const form = new FormData();
  form.append("params", JSON.stringify(params));
  form.append("srt", srtFile, srtFile.name);
  state.pdfs.forEach((p, i) => form.append(`pdf_${i}`, p.file, p.filename));

  $("submit-btn").disabled = true;
  $("submit-btn").textContent = "Submitting…";
  try {
    const res = await fetch(`${getApiBase()}/jobs`, { method: "POST", body: form });
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    const data = await res.json();
    state.jobId = data.jobId;
    $("setup-section").hidden = true;
    $("progress-section").hidden = false;
    $("progress-job-id").textContent = state.jobId;
    renderStepList(data.step);
    startPolling("prepare");
    loadJobList();
  } catch (err) {
    showError($("setup-error"), `Failed to start job: ${err.message}`);
    $("submit-btn").disabled = false;
    $("submit-btn").textContent = "Start preparing";
  }
});

function renderStepList(currentStep) {
  const list = $("step-list");
  list.innerHTML = "";
  const currentIndex = STEP_ORDER.findIndex(([key]) => key === currentStep);
  STEP_ORDER.forEach(([key, label], i) => {
    const li = document.createElement("li");
    if (i < currentIndex) li.className = "done";
    else if (i === currentIndex) li.className = "active";
    const dot = document.createElement("span");
    dot.className = "dot";
    li.append(dot, document.createTextNode(label));
    list.appendChild(li);
  });
}

// ---------- polling ----------
function startPolling(context) {
  state.pollContext = context; // 'prepare' (Phase 1) or 'shorts' (any short's condense/render)
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(pollStatus, 3000);
  pollStatus();
}

async function pollStatus() {
  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/status`);
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    const job = await res.json();
    state.currentJob = job;

    if (state.pollContext === "shorts") {
      // A short failing sets job.phase back to ready_for_review with that
      // short marked failed (see worker.fail_short) - job.phase=="failed"
      // here would mean prepare itself broke, which shouldn't happen this
      // late, but is handled the same way either way: stop polling, show
      // whatever the server has.
      if (job.phase === "condensing" || job.phase === "rendering") {
        showReview(job); // refresh status badges in place
        return;
      }
      clearInterval(state.pollTimer);
      showReview(job);
      return;
    }

    renderStepList(job.step);
    if (job.phase === "failed") {
      clearInterval(state.pollTimer);
      showError($("progress-error"), job.error ? job.error.message : "Job failed.");
      return;
    }
    if (job.phase === "ready_for_review" || job.phase === "done") {
      clearInterval(state.pollTimer);
      $("progress-section").hidden = true;
      showReview(job);
    }
  } catch (err) {
    showError(state.pollContext === "shorts" ? $("create-short-error") : $("progress-error"),
      `Lost contact with server: ${err.message}`);
  }
}

// ---------- review (Phase 1 complete): permanent 1:1 narration ----------
function showReview(job) {
  $("review-section").hidden = false;
  const slidesById = Object.fromEntries(job.slides.map((s) => [s.slideId, s]));
  const alignmentBySeq = Object.fromEntries(job.alignment.map((a) => [a.sequenceIndex, a]));

  const list = $("segment-list");
  list.innerHTML = "";
  for (const n of [...job.narration].sort((a, b) => a.sequenceIndex - b.sequenceIndex)) {
    const slide = slidesById[n.slideId];
    const alignment = alignmentBySeq[n.sequenceIndex];

    const card = document.createElement("div");
    card.className = "segment-card";

    const img = document.createElement("img");
    img.src = fileUrl(slide.imagePath);
    img.alt = n.slideId;

    const body = document.createElement("div");
    body.className = "segment-body";
    const text = document.createElement("div");
    text.className = "script-text";
    text.textContent = n.script;
    body.appendChild(text);

    // Cleaning (filler/personal-reference removal) is lossy by nature - the
    // raw excerpt alignment produced is kept untouched precisely so it can
    // be compared here, not just discarded once cleaning runs.
    if (alignment) {
      const details = document.createElement("details");
      details.className = "raw-excerpt";
      const summary = document.createElement("summary");
      summary.textContent = "Show original excerpt (before cleaning)";
      const raw = document.createElement("div");
      raw.className = "raw-excerpt-text";
      raw.textContent = alignment.transcriptExcerpt;
      details.append(summary, raw);
      body.appendChild(details);
    }

    card.append(img, body);
    list.appendChild(card);
  }

  renderShortsList(job, slidesById);
}

// ---------- shorts: create, list, render ----------
function renderShortsList(job, slidesById) {
  const container = $("shorts-list");
  container.innerHTML = "";
  const shorts = [...(job.shorts || [])].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  for (const short of shorts) {
    container.appendChild(buildShortCard(job, short, slidesById));
  }

  const blocked = Boolean(job.activeShortId);
  $("create-short-btn").disabled = blocked;
  $("create-short-status").hidden = !blocked;
  if (blocked) $("create-short-status").textContent = "A short is currently processing — wait for it to finish before creating another.";
}

function buildShortCard(job, short, slidesById) {
  const card = document.createElement("div");
  card.className = "short-card";

  const header = document.createElement("h4");
  header.textContent = `${short.topic} — ${short.targetDurationSeconds}s — ${short.phase.replace(/_/g, " ")}`;
  card.appendChild(header);

  if (short.phase === "failed") {
    const err = document.createElement("p");
    err.className = "error-text";
    err.textContent = short.error ? short.error.message : "This short failed.";
    card.appendChild(err);
    return card;
  }

  if (short.phase === "condensing") {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "Condensing narration and synthesizing voice…";
    card.appendChild(p);
    return card;
  }

  // ready_for_render / rendering / done - the script (and usually audio) exist
  for (const n of [...short.script].sort((a, b) => a.sequenceIndex - b.sequenceIndex)) {
    const slide = slidesById[n.slideId];
    const audio = (short.audio || []).find((a) => a.sequenceIndex === n.sequenceIndex);

    const seg = document.createElement("div");
    seg.className = "segment-card";
    const img = document.createElement("img");
    img.src = fileUrl(slide.imagePath);
    img.alt = n.slideId;
    const body = document.createElement("div");
    body.className = "segment-body";
    const text = document.createElement("div");
    text.className = "script-text";
    text.textContent = n.script;
    body.appendChild(text);
    if (audio) {
      const audioEl = document.createElement("audio");
      audioEl.controls = true;
      audioEl.src = fileUrl(audio.path);
      body.appendChild(audioEl);
    }
    seg.append(img, body);
    card.appendChild(seg);
  }

  if (short.phase === "done" && short.render && short.render.outputUrl) {
    const video = document.createElement("video");
    video.controls = true;
    video.src = short.render.outputUrl;
    const download = document.createElement("a");
    download.className = "primary-btn";
    download.href = short.render.outputUrl;
    download.download = `${short.topic.trim().replace(/\W+/g, "-").toLowerCase()}.mp4`;
    download.textContent = "Download video";
    card.append(video, download);
  }

  const controls = document.createElement("div");
  controls.className = "field-row";

  const transitionType = document.createElement("select");
  [["cut", "Cut"], ["fade", "Fade to black"], ["crossfade", "Crossfade"], ["wipe", "Wipe"], ["slide", "Slide"]].forEach(([v, label]) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = label;
    if (v === job.params.transition.type) opt.selected = true;
    transitionType.appendChild(opt);
  });

  const aspect = document.createElement("select");
  ["16:9", "9:16", "1:1"].forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    if (a === (job.params.aspectRatio || "16:9")) opt.selected = true;
    aspect.appendChild(opt);
  });

  const quality = document.createElement("select");
  [["preview", "Preview"], ["standard", "Standard"], ["full", "Full (slow)"]].forEach(([v, label]) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = label;
    quality.appendChild(opt);
  });

  const renderBtn = document.createElement("button");
  renderBtn.type = "button";
  renderBtn.className = "primary-btn";
  renderBtn.textContent = short.phase === "rendering" ? "Rendering…" : "Render video";
  renderBtn.disabled = short.phase === "rendering" || Boolean(job.activeShortId);
  renderBtn.addEventListener("click", () =>
    triggerShortRender(short.shortId, transitionType.value, aspect.value, quality.value, errorEl));

  controls.append(transitionType, aspect, quality, renderBtn);
  card.appendChild(controls);

  const errorEl = document.createElement("p");
  errorEl.className = "error-text";
  errorEl.hidden = true;
  card.appendChild(errorEl);

  return card;
}

async function triggerShortRender(shortId, transitionType, aspect, quality, errorEl) {
  showError(errorEl, "");
  const resolution = RESOLUTION_MAP[aspect][quality];
  const t = state.currentJob.params.transition;
  const body = {
    transitionType,
    transitionSeconds: t.transitionSeconds,
    minSlideSeconds: t.minSlideSeconds,
    resolution,
  };

  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/shorts/${shortId}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    startPolling("shorts");
  } catch (err) {
    showError(errorEl, `Failed to start render: ${err.message}`);
  }
}

$("create-short-btn").addEventListener("click", async () => {
  showError($("create-short-error"), "");
  const topic = $("short-topic").value.trim();
  if (!topic) {
    showError($("create-short-error"), "Topic is required.");
    return;
  }

  $("create-short-btn").disabled = true;
  $("create-short-status").hidden = false;
  $("create-short-status").textContent = "Creating short — condensing narration and synthesizing voice…";

  const voiceMode = $("short-voice-preset").value === "custom" ? "custom" : "preset";
  const body = {
    topic,
    targetDurationSeconds: Number($("short-duration").value),
    voice: {
      mode: voiceMode,
      presetVoiceId: voiceMode === "preset" ? $("short-voice-preset").value : null,
      customDescription: voiceMode === "custom" ? $("short-voice-custom").value.trim() : null,
    },
  };

  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/shorts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    $("short-topic").value = "";
    startPolling("shorts");
  } catch (err) {
    $("create-short-status").hidden = true;
    $("create-short-btn").disabled = false;
    showError($("create-short-error"), `Failed to create short: ${err.message}`);
  }
});

// ---------- back to setup ----------
// Not non-destructive by design - this abandons the current job entirely
// rather than trying to preserve/resume it.
$("back-btn").addEventListener("click", () => {
  clearInterval(state.pollTimer);
  state.jobId = null;
  state.currentJob = null;
  state.pdfs = [];
  srtFile = null;
  $("srt-input").value = "";
  $("pdf-input").value = "";
  $("srt-filename").hidden = true;
  renderPdfList();
  $("review-section").hidden = true;
  $("progress-section").hidden = true;
  $("setup-section").hidden = false;
  validateSetup();
  $("setup-section").scrollIntoView({ behavior: "smooth" });
});

// ---------- narration-style prompt library ----------
let prompts = [];

async function loadPrompts(selectId) {
  try {
    const res = await fetch(`${getApiBase()}/prompts`);
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    prompts = await res.json();
    const picker = $("prompt-picker");
    picker.innerHTML = '<option value="">Custom (not saved)</option>';
    for (const p of prompts) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      picker.appendChild(opt);
    }
    if (selectId) picker.value = selectId;
  } catch (err) {
    showError($("prompt-status"), `Failed to load saved prompts: ${err.message}`);
  }
}

$("prompt-picker").addEventListener("change", () => {
  const prompt = prompts.find((p) => p.id === $("prompt-picker").value);
  $("narration-style").value = prompt ? prompt.text : "";
  showError($("prompt-status"), "");
});

$("prompt-save-btn").addEventListener("click", async () => {
  const id = $("prompt-picker").value;
  const text = $("narration-style").value.trim();
  if (!id) {
    showError($("prompt-status"), 'No saved prompt selected — use "Save as new…" to create one.');
    return;
  }
  if (!text) {
    showError($("prompt-status"), "Narration style text is empty.");
    return;
  }
  const prompt = prompts.find((p) => p.id === id);
  try {
    const res = await fetch(`${getApiBase()}/prompts/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: prompt.name, text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    showError($("prompt-status"), `Saved "${data.name}".`);
    await loadPrompts(id);
  } catch (err) {
    showError($("prompt-status"), `Failed to save: ${err.message}`);
  }
});

$("prompt-save-as-btn").addEventListener("click", async () => {
  const text = $("narration-style").value.trim();
  if (!text) {
    showError($("prompt-status"), "Narration style text is empty.");
    return;
  }
  const name = window.prompt("Name for this prompt:");
  if (!name || !name.trim()) return;
  try {
    const res = await fetch(`${getApiBase()}/prompts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    showError($("prompt-status"), `Saved "${data.name}".`);
    await loadPrompts(data.id);
  } catch (err) {
    showError($("prompt-status"), `Failed to save: ${err.message}`);
  }
});

// ---------- existing-job picker ----------
// Refreshing the page loses state.jobId (in-memory only), which otherwise
// means the only way back to an already-prepared job's review screen is to
// resubmit and re-spend Manus/Claude/ElevenLabs credits on work that's
// already done - this picker is the fix for that.
async function loadJobList() {
  try {
    const res = await fetch(`${getApiBase()}/jobs`);
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    const jobs = await res.json();
    const picker = $("job-picker");
    const previousValue = picker.value;
    picker.innerHTML = '<option value="">— New job (use the form below) —</option>';
    for (const j of jobs) {
      const opt = document.createElement("option");
      opt.value = j.jobId;
      const when = new Date(j.createdAt).toLocaleString();
      opt.textContent = `${when} — ${j.phase} — ${j.jobId.slice(0, 8)}`;
      picker.appendChild(opt);
    }
    picker.value = previousValue;
  } catch (err) {
    showError($("job-picker-error"), `Failed to load job list: ${err.message}`);
  }
}

$("job-picker").addEventListener("change", async () => {
  const jobId = $("job-picker").value;
  showError($("job-picker-error"), "");
  if (!jobId) return;

  clearInterval(state.pollTimer);
  state.jobId = jobId;
  $("setup-section").hidden = true;
  $("progress-section").hidden = true;
  $("review-section").hidden = true;

  try {
    const res = await fetch(`${getApiBase()}/jobs/${jobId}/status`);
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    const job = await res.json();
    state.currentJob = job;

    if (job.phase === "failed") {
      $("progress-section").hidden = false;
      renderStepList(job.step);
      showError($("progress-error"), job.error ? job.error.message : "Job failed.");
    } else if (job.phase === "condensing" || job.phase === "rendering") {
      // a short is mid-pipeline; job.phase always returns to ready_for_review
      // once it finishes or fails (see worker.fail_short/run_render)
      showReview(job);
      startPolling("shorts");
    } else if (job.phase === "ready_for_review" || job.phase === "ready_for_render" || job.phase === "done") {
      // the latter two are legacy values from jobs created before shorts existed
      showReview(job);
    } else {
      $("progress-section").hidden = false;
      $("progress-job-id").textContent = jobId;
      startPolling("prepare");
    }
  } catch (err) {
    showError($("job-picker-error"), `Failed to load job: ${err.message}`);
  }
});

// initial state
validateSetup();
loadPrompts();
loadJobList();
