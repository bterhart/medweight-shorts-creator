// Waterway Narrator UI — talks to the three n8n webhooks:
//   POST {apiBase}/jobs                  (phase1-prepare.workflow.json)
//   GET  {apiBase}/jobs/:jobId/status    (status.workflow.json)
//   POST {apiBase}/jobs/:jobId/render    (render-trigger.workflow.json)
//   GET  {apiBase}/files?path=...        (files.workflow.json), for slide images/audio/video

const STEP_ORDER = [
  ["queued", "Queued"],
  ["saving_inputs", "Saving uploaded files"],
  ["parsing_srt", "Parsing transcript"],
  ["extracting_pdfs", "Extracting slide images"],
  ["uploading_slides_to_manus", "Uploading slides to Manus"],
  ["aligning", "Aligning transcript to slides"],
  ["condensing_narration", "Writing condensed narration"],
  ["resolving_voice", "Resolving voice"],
  ["synthesizing_audio", "Synthesizing narration audio"],
  ["probing_durations", "Measuring clip durations"],
  ["ready_for_render", "Ready for review"],
];

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

// ---------- duration segmented control ----------
$("duration-control").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-value]");
  if (!btn) return;
  state.duration = Number(btn.dataset.value);
  for (const b of $("duration-control").querySelectorAll("button")) b.classList.toggle("selected", b === btn);
});
// initialize default selection
$("duration-control").querySelector('button[data-value="90"]').classList.add("selected");

// ---------- voice preset/custom toggle ----------
$("voice-preset").addEventListener("change", () => {
  $("voice-custom").hidden = $("voice-preset").value !== "custom";
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
  const voiceMode = $("voice-preset").value === "custom" ? "custom" : "preset";
  const params = {
    targetDurationSeconds: state.duration,
    narrationStyle: $("narration-style").value.trim(),
    voice: {
      mode: voiceMode,
      presetVoiceId: voiceMode === "preset" ? $("voice-preset").value : null,
      customDescription: voiceMode === "custom" ? $("voice-custom").value.trim() : null,
    },
    transition: {
      type: $("transition-type").value,
      transitionSeconds: Number($("transition-seconds").value),
      minSlideSeconds: Number($("min-slide-seconds").value),
    },
    aspectRatio: $("aspect-select").selectedOptions[0].dataset.ratio,
    resolution: $("aspect-select").value,
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
    startPolling();
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
function startPolling() {
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
      showError($("progress-error"), job.error ? job.error.message : "Job failed.");
      return;
    }

    renderStepList(job.step);

    if (job.phase === "ready_for_render" || job.phase === "done") {
      clearInterval(state.pollTimer);
      $("progress-section").hidden = true;
      showReview(job);
    }
  } catch (err) {
    showError($("progress-error"), `Lost contact with server: ${err.message}`);
  }
}

// ---------- review (Phase 1 complete) ----------
function showReview(job) {
  $("review-section").hidden = false;
  const slidesById = Object.fromEntries(job.slides.map((s) => [s.slideId, s]));
  const audioBySeq = Object.fromEntries(job.audio.map((a) => [a.sequenceIndex, a]));

  const list = $("segment-list");
  list.innerHTML = "";
  for (const n of [...job.narration].sort((a, b) => a.sequenceIndex - b.sequenceIndex)) {
    const slide = slidesById[n.slideId];
    const audio = audioBySeq[n.sequenceIndex];

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

  if (job.phase === "done" && job.render && job.render.outputPath) {
    showResult(job);
  }
}

// ---------- render (Phase 2) ----------
$("render-btn").addEventListener("click", async () => {
  showError($("render-error"), "");
  $("render-btn").disabled = true;
  $("render-status").hidden = false;
  $("render-status").textContent = "Rendering… this runs locally and is usually quick, but can take a while for longer videos.";

  const body = {
    transitionType: $("render-transition-type").value,
    transitionSeconds: Number($("render-transition-seconds").value),
    minSlideSeconds: Number($("render-min-slide-seconds").value),
  };

  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    const job = await res.json();
    state.currentJob = job;
    $("render-status").hidden = true;
    showResult(job);
  } catch (err) {
    showError($("render-error"), `Render failed: ${err.message}`);
  } finally {
    $("render-btn").disabled = false;
  }
});

function showResult(job) {
  $("result-section").hidden = false;
  const url = fileUrl(job.render.outputPath);
  $("result-video").src = url;
  $("result-download").href = url;
  $("result-download").download = `waterway-narrator-${job.jobId}.mp4`;
  $("result-section").scrollIntoView({ behavior: "smooth" });
}

$("render-again-btn").addEventListener("click", () => {
  $("result-section").hidden = true;
  $("review-section").scrollIntoView({ behavior: "smooth" });
});

// initial state
validateSetup();
