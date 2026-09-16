// Chatbot Shorts UI — talks to the backend's four endpoints (backend/app.py):
//   POST {apiBase}/jobs                  create a job
//   GET  {apiBase}/jobs/:jobId/status    poll job status
//   POST {apiBase}/jobs/:jobId/render    trigger a render
//   GET  {apiBase}/files?path=...        slide images/audio/video

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
  duration: 90,
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

// ---------- voice preset/custom toggle (Step 3 - finalize narration) ----------
$("condense-voice-preset").addEventListener("change", () => {
  $("condense-voice-custom").hidden = $("condense-voice-preset").value !== "custom";
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
    // Neither of these has a UI control at intake anymore - duration and
    // voice are chosen later, in Step 3's "Finalize narration" (see the
    // condense-btn handler below). The backend still requires both fields
    // at job creation, so these are harmless placeholders overwritten by
    // POST /jobs/:id/condense once the job reaches ready_for_review.
    targetDurationSeconds: state.duration,
    narrationStyle: $("narration-style").value.trim(),
    voice: { mode: "preset", presetVoiceId: "21m00Tcm4TlvDq8ikWAM", customDescription: null },
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
  state.pollContext = context; // 'prepare' (Phase 1) or 'render' (Phase 2)
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

    if (job.phase === "failed") {
      clearInterval(state.pollTimer);
      const message = job.error ? job.error.message : "Job failed.";
      if (state.pollContext === "render") {
        $("render-status").hidden = true;
        $("render-btn").disabled = false;
        showError($("render-error"), message);
      } else if (state.pollContext === "condense") {
        $("condense-status").hidden = true;
        $("condense-btn").disabled = false;
        showError($("condense-error"), message);
      } else {
        showError($("progress-error"), message);
      }
      return;
    }

    if (state.pollContext === "render") {
      if (job.phase === "done") {
        clearInterval(state.pollTimer);
        $("render-status").hidden = true;
        $("render-btn").disabled = false;
        showResult(job);
      }
      // still "rendering" - nothing to update, #render-status already shows the in-progress message
      return;
    }

    if (state.pollContext === "condense") {
      if (job.phase === "ready_for_render") {
        clearInterval(state.pollTimer);
        $("condense-status").hidden = true;
        $("condense-btn").disabled = false;
        showReview(job); // refreshes the segment list with shortened narration + audio, reveals render-group
      }
      // still "condensing" - nothing to update, #condense-status already shows the in-progress message
      return;
    }

    renderStepList(job.step);
    if (job.phase === "ready_for_review" || job.phase === "done") {
      clearInterval(state.pollTimer);
      $("progress-section").hidden = true;
      showReview(job);
    }
  } catch (err) {
    const target = state.pollContext === "render" ? $("render-error")
      : state.pollContext === "condense" ? $("condense-error")
      : $("progress-error");
    showError(target, `Lost contact with server: ${err.message}`);
  }
}

// ---------- review (Phase 1 complete) ----------
function showReview(job) {
  $("review-section").hidden = false;
  const slidesById = Object.fromEntries(job.slides.map((s) => [s.slideId, s]));
  const audioBySeq = Object.fromEntries(job.audio.map((a) => [a.sequenceIndex, a]));
  const alignmentBySeq = Object.fromEntries(job.alignment.map((a) => [a.sequenceIndex, a]));

  const list = $("segment-list");
  list.innerHTML = "";
  for (const n of [...job.narration].sort((a, b) => a.sequenceIndex - b.sequenceIndex)) {
    const slide = slidesById[n.slideId];
    const audio = audioBySeq[n.sequenceIndex];
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

    if (audio) {
      const audioEl = document.createElement("audio");
      audioEl.controls = true;
      audioEl.src = fileUrl(audio.path);
      body.appendChild(audioEl);
    }

    card.append(img, body);
    list.appendChild(card);
  }

  $("render-transition-type").value = job.params.transition.type;
  $("render-transition-seconds").value = job.params.transition.transitionSeconds;
  $("render-min-slide-seconds").value = job.params.transition.minSlideSeconds;

  // condense-group (duration/voice/TTS) only makes sense before that stage
  // has run; render-group (transition/render) only makes sense once audio
  // exists to render with.
  $("condense-group").hidden = job.phase !== "ready_for_review";
  $("render-group").hidden = !(job.phase === "ready_for_render" || job.phase === "done");

  if (job.phase === "done" && job.render && (job.render.outputUrl || job.render.outputPath)) {
    showResult(job);
  }
}

// ---------- finalize narration (condense + voice + TTS) ----------
$("condense-btn").addEventListener("click", async () => {
  showError($("condense-error"), "");
  $("condense-btn").disabled = true;
  $("condense-status").hidden = false;
  $("condense-status").textContent = "Shortening narration and synthesizing voice audio…";

  const voiceMode = $("condense-voice-preset").value === "custom" ? "custom" : "preset";
  const body = {
    targetDurationSeconds: Number($("condense-duration").value),
    voice: {
      mode: voiceMode,
      presetVoiceId: voiceMode === "preset" ? $("condense-voice-preset").value : null,
      customDescription: voiceMode === "custom" ? $("condense-voice-custom").value.trim() : null,
    },
  };

  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/condense`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json()).error || `Server responded ${res.status}`);
    startPolling("condense");
  } catch (err) {
    $("condense-status").hidden = true;
    $("condense-btn").disabled = false;
    showError($("condense-error"), `Failed to start: ${err.message}`);
  }
});

// ---------- render (Phase 2) ----------
// The render-trigger webhook acks almost immediately (it backgrounds the
// actual render and returns phase="rendering") specifically so this never
// risks an HTTP timeout, no matter how long a Full/1080p render takes -
// completion is picked up by polling the same status endpoint Phase 1 uses.
$("render-btn").addEventListener("click", async () => {
  showError($("render-error"), "");
  $("render-btn").disabled = true;
  $("render-status").hidden = false;

  const quality = $("render-quality").value;
  const aspect = $("render-aspect").value;
  const resolution = RESOLUTION_MAP[aspect][quality];
  $("render-status").textContent = `Rendering at ${resolution} (${quality})… `
    + (quality === "full" ? "Full/1080p renders can take a minute or more." : "should only take a few seconds.");

  const body = {
    transitionType: $("render-transition-type").value,
    transitionSeconds: Number($("render-transition-seconds").value),
    minSlideSeconds: Number($("render-min-slide-seconds").value),
    resolution,
  };

  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    startPolling("render");
  } catch (err) {
    $("render-status").hidden = true;
    $("render-btn").disabled = false;
    showError($("render-error"), `Failed to start render: ${err.message}`);
  }
});

function showResult(job) {
  $("result-section").hidden = false;
  // outputUrl (a presigned S3 URL from the Fargate render) is used directly;
  // outputPath (an old in-process render's local server path) still needs
  // the /files proxy - kept for any job rendered before this change.
  const url = job.render.outputUrl || fileUrl(job.render.outputPath);
  $("result-video").src = url;
  $("result-download").href = url;
  $("result-download").download = `chatbot-shorts-${job.jobId}.mp4`;
  $("result-section").scrollIntoView({ behavior: "smooth" });
}

$("render-again-btn").addEventListener("click", () => {
  $("result-section").hidden = true;
  $("review-section").scrollIntoView({ behavior: "smooth" });
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
  $("result-section").hidden = true;
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
  $("result-section").hidden = true;

  try {
    const res = await fetch(`${getApiBase()}/jobs/${jobId}/status`);
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    const job = await res.json();
    state.currentJob = job;

    if (job.phase === "failed") {
      $("progress-section").hidden = false;
      renderStepList(job.step);
      showError($("progress-error"), job.error ? job.error.message : "Job failed.");
    } else if (job.phase === "ready_for_review" || job.phase === "ready_for_render" || job.phase === "done") {
      showReview(job);
    } else if (job.phase === "condensing") {
      showReview(job);
      $("condense-btn").disabled = true;
      $("condense-status").hidden = false;
      $("condense-status").textContent = "Shortening narration and synthesizing voice audio…";
      startPolling("condense");
    } else if (job.phase === "rendering") {
      showReview(job);
      $("render-btn").disabled = true;
      $("render-status").hidden = false;
      $("render-status").textContent = "Rendering…";
      startPolling("render");
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
