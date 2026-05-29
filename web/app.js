// ── Modal helpers ─────────────────────────────────────────────────────────────

function openModal(name) {
  document.getElementById("modal-" + name).classList.add("open");
}

function closeModal(name) {
  const el = document.getElementById(name) || document.getElementById("modal-" + name);
  if (el) el.classList.remove("open");
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const open = document.querySelector(".modal-overlay.open");
  if (open) open.classList.remove("open");
});

function handleOverlayClick(e, name) {
  if (e.target === e.currentTarget) closeModal(name);
}

// ── Audio import ──────────────────────────────────────────────────────────────

let _audioFile = null;

function onAudioFileSelected(input) {
  _audioFile = input.files[0];
  if (!_audioFile) return;
  const drop = document.getElementById("audio-drop");
  drop.classList.add("has-file");
  document.getElementById("audio-drop-label").textContent = _audioFile.name;
  document.getElementById("audio-submit-btn").disabled = false;
}

document.addEventListener("DOMContentLoaded", () => {
  const drop = document.getElementById("audio-drop");
  if (!drop) return;
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag-over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file) {
      _audioFile = file;
      drop.classList.add("has-file");
      document.getElementById("audio-drop-label").textContent = file.name;
      document.getElementById("audio-submit-btn").disabled = false;
    }
  });
});

function submitAudio() {
  if (!_audioFile) return;
  closeModal("audio");
  showProcessing("Uploading file…");

  const form = new FormData();
  form.append("file", _audioFile);
  form.append("meeting_name", document.getElementById("audio-name").value.trim());
  form.append("language", document.getElementById("audio-lang").value);

  fetch("/api/import/audio", { method: "POST", body: form })
    .then((r) => r.json())
    .then((data) => {
      if (data.error) { hideProcessing(); alert(data.error); return; }
      pollJob(data.job_id);
    })
    .catch(() => { hideProcessing(); alert("Upload failed."); });
}

// ── Transcript import ─────────────────────────────────────────────────────────

function onTranscriptFileSelected(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => { document.getElementById("transcript-content").value = e.target.result; };
  reader.readAsText(file);
}

function submitTranscript() {
  const content = document.getElementById("transcript-content").value.trim();
  if (!content) { alert("Paste a transcript first."); return; }
  closeModal("transcript");
  showProcessing("Generating client update…");

  const form = new FormData();
  form.append("content", content);
  form.append("meeting_name", document.getElementById("transcript-name").value.trim());

  fetch("/api/import/transcript", { method: "POST", body: form })
    .then((r) => r.json())
    .then((data) => {
      if (data.error) { hideProcessing(); alert(data.error); return; }
      pollJob(data.job_id);
    })
    .catch(() => { hideProcessing(); alert("Request failed."); });
}

// ── Multi-select & combined update ────────────────────────────────────────────

const _selected = new Set();

window.toggleSelect = function (id, checkbox) {
  const card = document.getElementById("card-" + id);
  if (checkbox.checked) {
    _selected.add(id);
    card && card.classList.add("selected");
  } else {
    _selected.delete(id);
    card && card.classList.remove("selected");
  }
  updateSelectionBar();
};

window.clearSelection = function () {
  _selected.clear();
  document.querySelectorAll(".card-checkbox").forEach((cb) => (cb.checked = false));
  document.querySelectorAll(".update-card").forEach((c) => c.classList.remove("selected"));
  updateSelectionBar();
};

window.generateCombined = function () {
  if (_selected.size < 2) return;
  showProcessing("Generating combined update…");

  fetch("/api/generate-combined", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: [..._selected], name: "" }),
  })
    .then((r) => r.json())
    .then((data) => {
      if (data.error) { hideProcessing(); alert(data.error); return; }
      clearSelection();
      pollJob(data.job_id);
    })
    .catch(() => { hideProcessing(); alert("Request failed."); });
};

function updateSelectionBar() {
  const btn = document.getElementById("btn-combined");
  const label = document.getElementById("btn-combined-label");
  if (!btn) return;
  if (_selected.size >= 2) {
    btn.disabled = false;
    label.textContent = `Generate Combined (${_selected.size})`;
  } else {
    btn.disabled = true;
    label.textContent = "Generate Combined";
  }
}

// ── Filters ───────────────────────────────────────────────────────────────────

let _filterOwner  = "me";
let _filterStatus = "pending";

window.setOwner = function (val) {
  _filterOwner = val;
  document.getElementById("tab-me").classList.toggle("active", val === "me");
  document.getElementById("tab-team").classList.toggle("active", val === "team");
  renderList(_allUpdates);
};

window.setStatus = function (val) {
  _filterStatus = val;
  document.getElementById("tab-pending").classList.toggle("active", val === "pending");
  document.getElementById("tab-archived").classList.toggle("active", val === "archived");
  renderList(_allUpdates);
};

// ── Index page ────────────────────────────────────────────────────────────────

let _allUpdates = [];

if (document.getElementById("updates-list")) {
  loadData().then((updates) => {
    _allUpdates = updates;
    renderList(updates);
  });
}

function renderList(updates) {
  const list = document.getElementById("updates-list");
  if (!list) return;

  const filtered = updates.filter((u) => {
    const ownerOk = _filterOwner === "me"
      ? (u.owner === "me" || !u.owner)
      : u.owner === "team";
    const statusOk = _filterStatus === "pending"
      ? u.status === "pending"
      : u.status !== "pending";
    return ownerOk && statusOk;
  });

  if (filtered.length === 0) {
    list.innerHTML = '<p class="empty-state">No updates here.</p>';
    return;
  }

  if (_filterStatus === "pending") {
    list.innerHTML = filtered.map(pendingCardHTML).join("");
  } else {
    list.innerHTML = filtered.map(archivedCardHTML).join("");
  }
}

function pendingCardHTML(u) {
  return `
    <div class="update-card update-card-link" id="card-${esc(u.id)}"
         onclick="location.href='/update.html?id=${esc(u.id)}'">
      <label class="card-check-wrap" onclick="event.stopPropagation()">
        <input type="checkbox" class="card-checkbox" onchange="toggleSelect('${esc(u.id)}', this)">
        <span class="card-checkmark"></span>
      </label>
      <div class="update-card-body">
        <p class="update-title">${esc(u.meeting_name || "Untitled")}</p>
        <p class="update-date">${esc(u.date)}</p>
        <p class="update-preview">${esc(u.content.slice(0, 220))}${u.content.length > 220 ? "…" : ""}</p>
      </div>
      <div class="card-actions" onclick="event.stopPropagation()">
        <svg class="card-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9,18 15,12 9,6"/></svg>
        <button class="card-delete-btn" onclick="deleteUpdate('${esc(u.id)}')" title="Delete">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3,6 5,6 21,6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
        </button>
      </div>
    </div>`;
}

function archivedCardHTML(u) {
  return `
    <div class="archive-card" id="card-${esc(u.id)}">
      <div>
        <p class="update-title">${esc(u.meeting_name || "Untitled")}</p>
        <p class="update-date">${esc(u.date)}</p>
      </div>
      <div class="archive-card-right">
        ${u.status === "sent"
          ? '<span class="badge badge-sent">Sent</span>'
          : '<span class="badge badge-archived">Archived</span>'}
        <a href="/update.html?id=${esc(u.id)}" class="link-view">View</a>
        <button class="card-delete-btn card-delete-btn--visible" onclick="deleteUpdate('${esc(u.id)}')" title="Delete">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3,6 5,6 21,6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
        </button>
      </div>
    </div>`;
}

window.deleteUpdate = function (id) {
  if (!confirm("Delete this update?")) return;
  fetch("/api/delete/" + id, { method: "POST" })
    .then((r) => r.json())
    .then(() => {
      _allUpdates = _allUpdates.filter((u) => u.id !== id);
      renderList(_allUpdates);
    });
};

// ── Detail page ───────────────────────────────────────────────────────────────

if (document.getElementById("content")) {
  const uid = new URLSearchParams(location.search).get("id");

  loadData().then((updates) => {
    const u = updates.find((u) => u.id === uid);
    if (!u) {
      document.body.innerHTML = '<p style="padding:40px;color:#4d6275;font-size:13px">Update not found.</p>';
      return;
    }

    document.title = (u.meeting_name || "Update") + " — cloud++";
    const name = u.meeting_name || "Untitled Meeting";
    document.getElementById("meeting-name").textContent    = name;
    document.getElementById("meeting-date").textContent    = u.date;
    document.getElementById("breadcrumb-name").textContent = name;
    document.getElementById("content").value               = u.content;

    if (u.status === "sent") {
      setBadge("Email sent", "sent");
      document.getElementById("actions").style.display = "none";
      document.getElementById("content").readOnly = true;
    } else if (u.status === "archived") {
      setBadge("Archived", "archived");
      document.getElementById("actions").style.display = "none";
      document.getElementById("content").readOnly = true;
    } else {
      setBadge("Pending review", "pending");
    }
  });

  let saveTimer;
  document.getElementById("content").addEventListener("input", () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveContent, 800);
  });

  function saveContent() {
    fetch("/api/save/" + uid, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: document.getElementById("content").value }),
    });
  }

  // Load raw transcript
  fetch("/api/transcript/" + uid)
    .then((r) => r.json())
    .then((data) => {
      if (data.content) {
        document.getElementById("raw-transcript").value = data.content;
        document.getElementById("btn-raw-toggle").style.display = "";
        const btnR = document.getElementById("btn-reprocess");
        if (btnR) btnR.style.display = "";
      }
    });

  let _rawViewActive = false;

  window.toggleRawView = function () {
    _rawViewActive = !_rawViewActive;
    const panels  = document.getElementById("content-panels");
    const section = document.getElementById("transcript-section");
    const label   = document.getElementById("raw-toggle-label");
    const btn     = document.getElementById("btn-raw-toggle");

    if (_rawViewActive) {
      section.style.display = "";
      panels.classList.add("split-view");
      label.textContent = "Hide raw";
      btn.classList.add("btn-active");
    } else {
      section.style.display = "none";
      panels.classList.remove("split-view");
      label.textContent = "Raw";
      btn.classList.remove("btn-active");
    }
  };

  window.openReprocessModal = function () {
    const currentContent = document.getElementById("content").value;
    document.getElementById("reprocess-hint").value = currentContent;
    openModal("reprocess");
  };

  window.submitReprocess = function () {
    const transcript = document.getElementById("raw-transcript").value.trim();
    const hint = document.getElementById("reprocess-hint").value.trim();
    if (!transcript) { alert("No transcript content to reprocess."); return; }
    closeModal("reprocess");
    showProcessing("Reprocessing with Claude…");

    fetch("/api/reprocess/" + uid, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript, hint }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.error) { hideProcessing(); alert(data.error); return; }
        pollJob(data.job_id);
      })
      .catch(() => { hideProcessing(); alert("Request failed."); });
  };

  window.sendUpdate = function () {
    saveContent();
    fetch("/api/send/" + uid, { method: "POST" }).then(() => {
      document.getElementById("actions").style.display = "none";
      document.getElementById("content").readOnly = true;
      setBadge("Email sent", "sent");
      showToast("Email sent to client. Update archived.", "green");
    });
  };

  window.archiveUpdate = function () {
    saveContent();
    fetch("/api/archive/" + uid, { method: "POST" }).then(() => {
      document.getElementById("actions").style.display = "none";
      document.getElementById("content").readOnly = true;
      setBadge("Archived", "archived");
      showToast("Update archived.", "gray");
    });
  };
}

// ── Job polling ───────────────────────────────────────────────────────────────

function pollJob(jobId) {
  const interval = setInterval(() => {
    fetch("/api/job/" + jobId)
      .then((r) => r.json())
      .then((job) => {
        if (job.status === "processing" || job.status === "queued") {
          const stepEl = document.getElementById("processing-step");
          if (stepEl) stepEl.textContent = job.step || "Processing…";
        } else if (job.status === "done") {
          clearInterval(interval);
          hideProcessing();
          location.href = "/update.html?id=" + job.update_id;
        } else if (job.status === "error") {
          clearInterval(interval);
          hideProcessing();
          alert("Processing failed: " + job.message);
        }
      });
  }, 3000);
}

function showProcessing(step) {
  const stepEl = document.getElementById("processing-step");
  if (stepEl) stepEl.textContent = step;
  const el = document.getElementById("modal-processing");
  if (el) el.classList.add("open");
}

function hideProcessing() {
  const el = document.getElementById("modal-processing");
  if (el) el.classList.remove("open");
}

// ── Explorer mode ─────────────────────────────────────────────────────────────

let _explorerActive = false;

window.toggleExplorer = function () {
  _explorerActive = !_explorerActive;
  const btn      = document.getElementById("btn-explorer");
  const panel    = document.getElementById("explorer-panel");
  const list     = document.getElementById("updates-list");
  const tabWrap  = document.querySelector(".tab-bar-wrap");
  const divider  = document.querySelector(".header-divider");

  if (_explorerActive) {
    panel.style.display   = "";
    list.style.display    = "none";
    if (tabWrap)  tabWrap.style.display  = "none";
    if (divider)  divider.style.display  = "none";
    btn.classList.add("btn-active");
    document.getElementById("explorer-input").focus();
  } else {
    panel.style.display   = "none";
    list.style.display    = "";
    if (tabWrap)  tabWrap.style.display  = "";
    if (divider)  divider.style.display  = "";
    btn.classList.remove("btn-active");
  }
};

window.fillExplore = function (question) {
  const input = document.getElementById("explorer-input");
  input.value = question;
  input.focus();
};

window.submitExplore = function () {
  const input = document.getElementById("explorer-input");
  const query = input.value.trim();
  if (!query) return;

  const results = document.getElementById("explorer-results");
  results.style.display = "";
  results.innerHTML = '<div class="explorer-loading"><div class="spinner" style="width:20px;height:20px;border-width:2px"></div><span>Searching transcripts…</span></div>';

  fetch("/api/explore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  })
    .then((r) => r.json())
    .then((data) => {
      if (data.error) { results.textContent = "Error: " + data.error; return; }
      pollExploreJob(data.job_id, results);
    })
    .catch(() => { results.textContent = "Request failed."; });
};

function pollExploreJob(jobId, resultsEl) {
  const interval = setInterval(() => {
    fetch("/api/job/" + jobId)
      .then((r) => r.json())
      .then((job) => {
        if (job.status === "done") {
          clearInterval(interval);
          resultsEl.textContent = job.answer || "No answer found.";
        } else if (job.status === "error") {
          clearInterval(interval);
          resultsEl.textContent = "Error: " + job.message;
        }
      });
  }, 3000);
}

// ── Shared helpers ────────────────────────────────────────────────────────────

async function loadData() {
  const res = await fetch("/api/data");
  return res.json();
}

function setBadge(text, type) {
  const badge = document.getElementById("status-badge");
  if (!badge) return;
  badge.textContent = text;
  badge.className = "badge badge-" + type;
}

function showToast(msg, color) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast toast-" + color;
}

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
