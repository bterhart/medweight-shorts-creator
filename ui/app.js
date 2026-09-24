// Chatbot Shorts UI — talks to backend/app.py:
//   POST {apiBase}/jobs                                  create a job
//   GET  {apiBase}/jobs/:jobId/status                    poll job status
//   POST {apiBase}/jobs/:jobId/shorts                    create a short from the job's permanent narration
//   POST {apiBase}/jobs/:jobId/shorts/:shortId/render    trigger that short's render
//   PATCH  {apiBase}/jobs/:jobId/shorts/:shortId/segments/:seq        rewrite one segment's text (resynthesizes it)
//   POST   {apiBase}/jobs/:jobId/shorts/:shortId/segments/:seq/image  replace one segment's image (upload or deck slide)
//   DELETE {apiBase}/jobs/:jobId/shorts/:shortId/segments/:seq        remove one segment
//   POST   {apiBase}/jobs/:jobId/shorts/:shortId/segments             insert a new segment
//   GET  {apiBase}/files?path=...                        slide images/audio

const STEP_ORDER = [
  ["queued", "Queued"],
  ["saving_inputs", "Saving uploaded files"],
  ["parsing_srt", "Parsing transcript"],
  ["extracting_pdfs", "Extracting slide images"],
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
  textFields: [], // { textArea, original } for every editable segment currently on screen
  mediaEls: [], // every <video> the shorts list currently owns - released on rebuild, see releaseMediaElements
  audioPlayer: null, // the one shared <audio> for segment narration, moved to whichever segment is playing
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

// ---------- unsaved-edit guard ----------
// Every action that refreshes the review (render, save, delete, image swap,
// add, create short, switch job) rebuilds all cards, which would silently
// discard narration typed into any textarea but not yet saved. Dirty state
// is computed on demand against each segment's saved script - no event
// tracking to get out of sync.
function unsavedTextFields(except) {
  return state.textFields.filter((f) =>
    f.textArea !== except && f.textArea.isConnected && f.textArea.value.trim() !== f.original.trim());
}

function confirmDiscardUnsaved(except) {
  const n = unsavedTextFields(except).length;
  if (!n) return true;
  return window.confirm(
    `${n} slide${n === 1 ? " has" : "s have"} unsaved narration edits that won't be kept. Continue anyway?`);
}

window.addEventListener("beforeunload", (e) => {
  if (unsavedTextFields().length) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ---------- media players ----------
// Chrome caps a page at 1000 live media players on desktop (75 on mobile;
// crbug.com/1144736 - measured at exactly 1000 in headless Chromium). Every
// status poll while a short is mid-pipeline rebuilds all cards, and a
// dropped <audio>/<video> keeps its player until garbage collection gets to
// it - so a 4-minute render with a 10-segment short discarded ~800 players
// per render and, once a page had sat through a couple of renders, the
// freshly rendered <video> was refused ("Blocked attempt to create a
// WebMediaPlayer"). Two rules keep the count bounded: release players
// explicitly on every rebuild, and give segment narration one shared
// <audio> instead of one per segment.
function releaseMediaElements() {
  const els = state.mediaEls;
  if (state.audioPlayer) els.push(state.audioPlayer);
  for (const el of els) {
    el.pause();
    el.removeAttribute("src"); // removeAttribute, not src="": the latter fires an error event
    el.load(); // frees the player now instead of at GC time
  }
  state.mediaEls = [];
}

function playNarration(afterEl, url) {
  if (!state.audioPlayer) {
    state.audioPlayer = document.createElement("audio");
    state.audioPlayer.controls = true;
  }
  const player = state.audioPlayer;
  afterEl.after(player); // moving the element keeps its single player; no new one is created
  player.src = url;
  player.play().catch(() => {}); // autoplay refusal just leaves the controls for a manual click
}

// A presigned link expires an hour after the status read that produced it.
// The API re-signs on every read, so on a load error fetch the job once and
// swap in the fresh link - covers a page left open past the hour.
async function refreshVideoSource(video, shortId) {
  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/status`);
    if (!res.ok) return;
    const job = await res.json();
    const short = (job.shorts || []).find((s) => s.shortId === shortId);
    const url = short && short.render && short.render.outputUrl;
    if (!url || !video.isConnected) return;
    video.src = url;
    video.load();
  } catch (_) {
    // leave it; a page reload gets a fresh link anyway
  }
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
  // Both lists were fetched at page load against whatever base was set
  // then - a first-time or changed base would otherwise leave them empty
  // (or stale) until a reload. Clear any load-failure/empty-library text
  // from that first attempt so a now-successful load isn't contradicted.
  showError($("prompt-status"), "");
  loadPrompts();
  loadJobList();
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
      if (job.phase === "condensing" || job.phase === "editing" || job.phase === "rendering") {
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
  releaseMediaElements();
  container.innerHTML = "";
  state.textFields = [];
  const shorts = [...(job.shorts || [])].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  for (const short of shorts) {
    container.appendChild(buildShortCard(job, short, slidesById));
  }

  const blocked = Boolean(job.activeShortId);
  $("create-short-btn").disabled = blocked || prompts.length === 0;
  $("create-short-status").hidden = !blocked;
  if (blocked) $("create-short-status").textContent = "A short is currently processing — wait for it to finish before creating another.";
}

function buildCheckboxField(id, labelText) {
  const wrapper = document.createElement("label");
  wrapper.className = "checkbox-field";
  wrapper.htmlFor = id;
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  const span = document.createElement("span");
  span.textContent = labelText;
  wrapper.append(input, span);
  return { wrapper, input };
}

function segmentImagePath(n, slidesById) {
  if (n.customImagePath) return n.customImagePath;
  const slide = slidesById[n.slideId];
  return slide ? slide.imagePath : null;
}

function buildEditableSegment(job, short, n, slidesById, editable) {
  const seg = document.createElement("div");
  seg.className = "segment-card";

  const imagePath = segmentImagePath(n, slidesById);
  const img = document.createElement("img");
  if (imagePath) img.src = fileUrl(imagePath);
  img.alt = n.slideId || "custom slide";

  const body = document.createElement("div");
  body.className = "segment-body";

  const errorEl = document.createElement("p");
  errorEl.className = "error-text";
  errorEl.hidden = true;

  if (editable) {
    const textArea = document.createElement("textarea");
    textArea.className = "script-text-edit";
    textArea.rows = 3;
    textArea.value = n.script;
    body.appendChild(textArea);
    state.textFields.push({ textArea, original: n.script });

    const actions = document.createElement("div");
    actions.className = "field-row segment-actions";

    const saveTextBtn = document.createElement("button");
    saveTextBtn.type = "button";
    saveTextBtn.className = "secondary-btn";
    saveTextBtn.textContent = "Save text";
    saveTextBtn.addEventListener("click", () => {
      const text = textArea.value.trim();
      if (!text) { showError(errorEl, "Narration text can't be empty."); return; }
      if (text === n.script) return;
      if (!confirmDiscardUnsaved(textArea)) return;
      saveSegmentText(short.shortId, n.sequenceIndex, text, errorEl);
    });

    const deckPicker = document.createElement("select");
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "Replace with a deck slide…";
    deckPicker.appendChild(noneOpt);
    for (const s of job.slides || []) {
      const opt = document.createElement("option");
      opt.value = s.slideId;
      opt.textContent = `${s.pdfId} p.${s.pageNumber}`;
      deckPicker.appendChild(opt);
    }
    deckPicker.addEventListener("change", () => {
      if (!deckPicker.value) return;
      if (!confirmDiscardUnsaved()) { deckPicker.value = ""; return; }
      replaceSegmentImage(short.shortId, n.sequenceIndex, { sourceSlideId: deckPicker.value }, errorEl);
      deckPicker.value = "";
    });

    const uploadLabel = document.createElement("label");
    uploadLabel.className = "secondary-btn file-upload-btn";
    uploadLabel.textContent = "Upload image…";
    const uploadInput = document.createElement("input");
    uploadInput.type = "file";
    uploadInput.accept = "image/png,image/jpeg,image/webp";
    uploadInput.hidden = true;
    uploadInput.addEventListener("change", () => {
      if (!uploadInput.files.length) return;
      if (!confirmDiscardUnsaved()) { uploadInput.value = ""; return; }
      replaceSegmentImage(short.shortId, n.sequenceIndex, { file: uploadInput.files[0] }, errorEl);
      uploadInput.value = "";
    });
    uploadLabel.appendChild(uploadInput);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "secondary-btn";
    deleteBtn.textContent = "Delete slide";
    deleteBtn.addEventListener("click", () => {
      if (!confirmDiscardUnsaved(textArea)) return;
      if (!window.confirm("Delete this slide from the short?")) return;
      deleteSegment(short.shortId, n.sequenceIndex, errorEl);
    });

    actions.append(saveTextBtn, deckPicker, uploadLabel, deleteBtn);
    body.appendChild(actions);
  } else {
    const text = document.createElement("div");
    text.className = "script-text";
    text.textContent = n.script;
    body.appendChild(text);
  }

  const audio = (short.audio || []).find((a) => a.sequenceIndex === n.sequenceIndex);
  if (audio) {
    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.className = "secondary-btn play-narration-btn";
    playBtn.textContent = "Play narration";
    playBtn.addEventListener("click", () => playNarration(playBtn, fileUrl(audio.path)));
    body.appendChild(playBtn);
  }

  body.appendChild(errorEl);
  seg.append(img, body);
  return seg;
}

function buildAddSegmentForm(short) {
  const wrapper = document.createElement("div");
  wrapper.className = "field-group add-segment-form";

  const label = document.createElement("label");
  label.textContent = "Add a slide";
  wrapper.appendChild(label);

  const position = document.createElement("select");
  const count = short.script.length;
  for (let i = 0; i <= count; i++) {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = i === count ? "At the end" : `Before slide ${i + 1}`;
    if (i === count) opt.selected = true;
    position.appendChild(opt);
  }

  const textArea = document.createElement("textarea");
  textArea.rows = 2;
  textArea.placeholder = "Narration for the new slide";
  // A draft here has nothing "saved" to compare against, so any non-empty
  // text counts as unsaved work for the guard.
  state.textFields.push({ textArea, original: "" });

  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "image/png,image/jpeg,image/webp";

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "secondary-btn";
  addBtn.textContent = "Add slide";

  const errorEl = document.createElement("p");
  errorEl.className = "error-text";
  errorEl.hidden = true;

  addBtn.addEventListener("click", () => {
    const text = textArea.value.trim();
    if (!text) { showError(errorEl, "Narration text is required."); return; }
    if (!fileInput.files.length) { showError(errorEl, "An image is required."); return; }
    if (!confirmDiscardUnsaved(textArea)) return;
    addSegment(short.shortId, Number(position.value), text, fileInput.files[0], errorEl);
  });

  const row = document.createElement("div");
  row.className = "field-row";
  row.append(position, addBtn);

  wrapper.append(textArea, fileInput, row, errorEl);
  return wrapper;
}

async function saveSegmentText(shortId, seq, text, errorEl) {
  showError(errorEl, "");
  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/shorts/${shortId}/segments/${seq}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script: text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    startPolling("shorts");
  } catch (err) {
    showError(errorEl, `Failed to save text: ${err.message}`);
  }
}

async function replaceSegmentImage(shortId, seq, { file, sourceSlideId }, errorEl) {
  showError(errorEl, "");
  const form = new FormData();
  if (file) form.append("image", file, file.name);
  if (sourceSlideId) form.append("sourceSlideId", sourceSlideId);
  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/shorts/${shortId}/segments/${seq}/image`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    startPolling("shorts");
  } catch (err) {
    showError(errorEl, `Failed to replace image: ${err.message}`);
  }
}

async function deleteSegment(shortId, seq, errorEl) {
  showError(errorEl, "");
  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/shorts/${shortId}/segments/${seq}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    startPolling("shorts");
  } catch (err) {
    showError(errorEl, `Failed to delete slide: ${err.message}`);
  }
}

async function addSegment(shortId, position, text, file, errorEl) {
  showError(errorEl, "");
  const form = new FormData();
  form.append("script", text);
  form.append("position", String(position));
  form.append("image", file, file.name);
  try {
    const res = await fetch(`${getApiBase()}/jobs/${state.jobId}/shorts/${shortId}/segments`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Server responded ${res.status}`);
    startPolling("shorts");
  } catch (err) {
    showError(errorEl, `Failed to add slide: ${err.message}`);
  }
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

  if (short.phase === "editing") {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "Applying your edit — resynthesizing narration…";
    card.appendChild(p);
    return card;
  }

  // ready_for_render / rendering / done - the script (and usually audio)
  // exist. Editable once the pipeline isn't mid-flight for THIS short and no
  // other short on the job is active either - same window the render button
  // below is already enabled in.
  const editable = (short.phase === "ready_for_render" || short.phase === "done") && !job.activeShortId;
  for (const n of [...short.script].sort((a, b) => a.sequenceIndex - b.sequenceIndex)) {
    card.appendChild(buildEditableSegment(job, short, n, slidesById, editable));
  }

  if (editable) {
    card.appendChild(buildAddSegmentForm(short));
  }

  if (short.render && short.render.outputUrl) {
    // Kept visible even after phase drops back to ready_for_render (a
    // post-review edit) - it's the last real render, still valid to watch
    // or download, just possibly stale until the next render picks up the
    // edit.
    if (short.phase !== "done") {
      const stale = document.createElement("p");
      stale.className = "hint";
      stale.textContent = "This preview was rendered before your latest edit — render again to update it.";
      card.appendChild(stale);
    }
    const video = document.createElement("video");
    video.controls = true;
    video.src = short.render.outputUrl;
    video.addEventListener("error", () => refreshVideoSource(video, short.shortId), { once: true });
    state.mediaEls.push(video);
    const download = document.createElement("a");
    download.className = "primary-btn";
    download.href = short.render.outputUrl;
    download.download = `${short.topic.trim().replace(/\W+/g, "-").toLowerCase()}.mp4`;
    download.textContent = "Download video";
    card.append(video, download);
  }

  const introToggle = buildCheckboxField(`intro-${short.shortId}`, "Include intro");
  const outroToggle = buildCheckboxField(`outro-${short.shortId}`, "Include outro");
  const toggles = document.createElement("div");
  toggles.className = "field-row";
  toggles.append(introToggle.wrapper, outroToggle.wrapper);
  card.appendChild(toggles);

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
  renderBtn.addEventListener("click", () => {
    if (!confirmDiscardUnsaved()) return;
    triggerShortRender(
      short.shortId, transitionType.value, aspect.value, quality.value,
      introToggle.input.checked, outroToggle.input.checked, errorEl);
  });

  controls.append(transitionType, aspect, quality, renderBtn);
  card.appendChild(controls);

  const errorEl = document.createElement("p");
  errorEl.className = "error-text";
  errorEl.hidden = true;
  card.appendChild(errorEl);

  return card;
}

async function triggerShortRender(shortId, transitionType, aspect, quality, includeIntro, includeOutro, errorEl) {
  showError(errorEl, "");
  const resolution = RESOLUTION_MAP[aspect][quality];
  const t = state.currentJob.params.transition;
  const body = {
    transitionType,
    transitionSeconds: t.transitionSeconds,
    minSlideSeconds: t.minSlideSeconds,
    resolution,
    includeIntro,
    includeOutro,
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
  if (!$("short-prompt").value) {
    showError($("create-short-error"), "Save a narration prompt in Step 1 first — a short needs one to be written with.");
    return;
  }
  if (!confirmDiscardUnsaved()) return;

  $("create-short-btn").disabled = true;
  $("create-short-status").hidden = false;
  $("create-short-status").textContent = "Creating short — condensing narration and synthesizing voice…";

  const voiceMode = $("short-voice-preset").value === "custom" ? "custom" : "preset";
  const body = {
    topic,
    targetDurationSeconds: Number($("short-duration").value),
    promptId: $("short-prompt").value,
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
  if (!confirmDiscardUnsaved()) return;
  state.textFields = []; // the old cards stay in the (hidden) DOM - don't let them re-trigger the guard
  releaseMediaElements(); // ...and don't let their players count against Chrome's cap either
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

// The library entry a new short defaults to - the one build_short used to
// apply silently, so existing habits carry over; any saved prompt can be
// picked instead.
const DEFAULT_SHORT_PROMPT_NAME = "Second pass (CBT/MI/ACT/DBT narration)";

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
    fillShortPromptPicker();
    // An empty library and a failed load looked identical before (a bare
    // "Custom" entry, no message) - say which it is. Only ever set, never
    // cleared here: the save handlers write their own confirmation to this
    // element right before re-calling loadPrompts, and must not lose it.
    if (!prompts.length) showError($("prompt-status"), 'No saved prompts yet — write a style above and use "Save as new…".');
  } catch (err) {
    showError($("prompt-status"), `Failed to load saved prompts: ${err.message}`);
  }
}

function fillShortPromptPicker() {
  const picker = $("short-prompt");
  const previous = picker.value;
  picker.innerHTML = "";
  for (const p of prompts) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    picker.appendChild(opt);
  }
  const keep = prompts.find((p) => p.id === previous)
    || prompts.find((p) => p.name === DEFAULT_SHORT_PROMPT_NAME)
    || prompts[0];
  if (keep) picker.value = keep.id;
  $("short-prompt-hint").hidden = prompts.length > 0;
  const blocked = Boolean(state.currentJob && state.currentJob.activeShortId);
  $("create-short-btn").disabled = blocked || prompts.length === 0;
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
// resubmit and re-spend Claude/ElevenLabs credits on work that's
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
  if (!confirmDiscardUnsaved()) { $("job-picker").value = state.jobId || ""; return; }
  state.textFields = [];

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
    } else if (job.phase === "condensing" || job.phase === "editing" || job.phase === "rendering") {
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
