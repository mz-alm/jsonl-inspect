// jsonl-inspect frontend — vanilla JS, no build step.

"use strict";

// --- bootstrap: picker or inspector mode based on whether a session is loaded ---

async function bootstrap() {
  let file;
  try {
    file = await fetch("/api/file").then(r => r.json());
  } catch (err) {
    document.body.textContent = `Failed to reach server: ${err.message}`;
    return;
  }
  if (file.loaded) {
    document.getElementById("inspector").hidden = false;
    document.getElementById("picker").hidden = true;
    initInspector();
  } else {
    document.getElementById("inspector").hidden = true;
    document.getElementById("picker").hidden = false;
    initPicker();
  }
}

async function initPicker() {
  const projectsEl = document.getElementById("picker-projects-list");
  const sessionsEl = document.getElementById("picker-sessions-list");
  const sessionsHeading = document.getElementById("picker-sessions-heading");
  const subtitle = document.getElementById("picker-subtitle");

  let projectsData;
  try {
    projectsData = await fetch("/api/browse").then(r => r.json());
  } catch (err) {
    projectsEl.innerHTML = `<li class="picker-empty">error: ${escapeHTML(err.message)}</li>`;
    return;
  }
  subtitle.textContent = `browsing ${projectsData.claude_projects_dir}`;

  if (!projectsData.projects.length) {
    projectsEl.innerHTML = `<li class="picker-empty">no projects found</li>`;
    return;
  }

  for (const p of projectsData.projects) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="pl-primary">${escapeHTML(p.decoded_path)}</div>
      <div class="pl-secondary">${escapeHTML(p.key)}</div>
      <div class="pl-meta">
        <span>${p.session_count} session${p.session_count === 1 ? "" : "s"}</span>
      </div>
    `;
    li.addEventListener("click", async () => {
      // Selected state
      for (const sib of projectsEl.querySelectorAll("li.selected")) sib.classList.remove("selected");
      li.classList.add("selected");
      sessionsHeading.textContent = `sessions — ${p.decoded_path}`;
      sessionsEl.innerHTML = `<li class="picker-empty">loading…</li>`;
      try {
        const data = await fetch(`/api/browse/${encodeURIComponent(p.key)}`).then(r => r.json());
        renderPickerSessions(data, sessionsEl);
      } catch (err) {
        sessionsEl.innerHTML = `<li class="picker-empty">error: ${escapeHTML(err.message)}</li>`;
      }
    });
    projectsEl.appendChild(li);
  }
}

function renderPickerSessions(data, sessionsEl) {
  sessionsEl.innerHTML = "";
  if (!data.sessions.length) {
    sessionsEl.innerHTML = `<li class="picker-empty">no sessions in this project</li>`;
    return;
  }
  for (const s of data.sessions) {
    const li = document.createElement("li");
    const title = s.title || `<no title — ${s.session_id.slice(0, 12)}…>`;
    const age = fmtAge(s.mtime * 1000);
    li.innerHTML = `
      <div class="pl-primary">${escapeHTML(title)}</div>
      <div class="pl-secondary">${escapeHTML(s.session_id)}</div>
      <div class="pl-meta">
        <span>${fmtBytes(s.size_bytes)}</span>
        <span class="dot">·</span>
        <span>${escapeHTML(age)}</span>
      </div>
    `;
    li.addEventListener("click", async () => {
      li.classList.add("selected");
      li.querySelector(".pl-primary").textContent = `loading…`;
      try {
        const resp = await fetch("/api/load", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: s.path }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
          throw new Error(body.error || `HTTP ${resp.status}`);
        }
        // Reload the page; bootstrap will see the loaded session and render inspector
        window.location.reload();
      } catch (err) {
        alert(`Load failed: ${err.message}`);
        li.classList.remove("selected");
        li.querySelector(".pl-primary").textContent = title;
      }
    });
    sessionsEl.appendChild(li);
  }
}

// fmtBytes, fmtAge, and escapeHTML are defined later — they need to be available
// during initPicker. They're declared with `function` so they're hoisted.

const state = {
  records: [],
  stats: null,
  recordCache: new Map(),
  totalBytes: 0,
  // Multi-select bulk-pluck state
  selectionMode: false,
  selectedIndices: new Set(),
  // Index of the most recently selected card (for shift+click range select)
  lastSelectedIdx: null,
  // Block-type filter: set of block types currently UNCHECKED (hidden).
  // Empty set means all visible. Cards are filtered out if every block type
  // they have is in this set.
  hiddenBlockTypes: new Set(),
};

const els = {
  filename: document.getElementById("filename"),
  recordCount: document.getElementById("record-count"),
  wireBytes: document.getElementById("wire-bytes"),
  totalBytes: document.getElementById("total-bytes"),
  preflightTokens: document.getElementById("preflight-tokens"),
  pendingControls: document.getElementById("pending-controls"),
  pendingCount: document.getElementById("pending-count"),
  saveBtn: document.getElementById("save-btn"),
  discardBtn: document.getElementById("discard-btn"),
  undoBtn: document.getElementById("undo-btn"),
  pendingModal: document.getElementById("pending-modal"),
  pendingList: document.getElementById("pending-list"),
  modalSaveBtn: document.getElementById("modal-save-btn"),
  modalDiscardBtn: document.getElementById("modal-discard-btn"),
  modalUndoBtn: document.getElementById("modal-undo-btn"),
  modalCloseBtn: null,  // set after DOMContentLoaded since it's inside .modal-close
  selectionToggle: document.getElementById("selection-toggle"),
  selectionBar: document.getElementById("selection-bar"),
  selectionCount: document.getElementById("selection-count"),
  bulkPluckBtn: document.getElementById("bulk-pluck-btn"),
  clearSelectionBtn: document.getElementById("clear-selection-btn"),
  exitSelectionBtn: document.getElementById("exit-selection-btn"),
  pruneBtn: document.getElementById("prune-btn"),
  pruneKeepThinking: document.getElementById("prune-keep-thinking"),
  pruneKeepTools: document.getElementById("prune-keep-tools"),
  stripThinkingBtn: document.getElementById("strip-thinking-btn"),
  stripThinkingKeep: document.getElementById("strip-thinking-keep"),
  trimToolCallsBtn: document.getElementById("trim-tool-calls-btn"),
  trimToolCallsKeep: document.getElementById("trim-tool-calls-keep"),
  trimToolCallsThreshold: document.getElementById("trim-tool-calls-threshold"),
  trimToolCallsImages: document.getElementById("trim-tool-calls-images"),
  refreshPreflightBtn: document.getElementById("refresh-preflight-btn"),
  refreshPreflightTokens: document.getElementById("refresh-preflight-tokens"),
  backupsBtn: document.getElementById("backups-btn"),
  backupsModal: document.getElementById("backups-modal"),
  backupsList: document.getElementById("backups-list"),
  records: document.getElementById("records"),
  cardTemplate: document.getElementById("record-card-template"),
  blockTemplate: document.getElementById("block-template"),
  editPanelTemplate: document.getElementById("edit-panel-template"),
  pluckPanelTemplate: document.getElementById("pluck-panel-template"),
  searchInput: document.getElementById("search-input"),
  searchCount: document.getElementById("search-count"),
  composition: document.getElementById("composition-blocks"),
  filterToggles: document.getElementById("filter-toggles"),
  blockFilterToggles: document.getElementById("block-filter-toggles"),
  topRecords: document.getElementById("top-records"),
  compactionPanel: document.getElementById("compaction-panel"),
  compactionEvents: document.getElementById("compaction-events"),
  health: document.getElementById("health"),
};

// Local-metadata record types — filtered out by default to surface conversation.
const DEFAULT_HIDDEN_TYPES = new Set([
  "file-history-snapshot",
  "agent-name",
  "custom-title",
  "ai-title",
  "last-prompt",
  "permission-mode",
  "queue-operation",
]);

// --- utilities ---

function fmtBytes(n) {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / (1024 * 1024)).toFixed(2)}MB`;
}

function fmtTimestamp(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function sizeClass(size, total) {
  // Hybrid thresholds: relative (% of total) clamped at an absolute minimum.
  // Small sessions hit the floor (so a 500-byte record doesn't get flagged
  // even though it's 10% of a tiny file); large sessions scale up (a 100KB
  // record in a 50MB session isn't "huge" — it's only 0.2%).
  const hugeThreshold = Math.max(total * 0.005, 100_000);
  const largeThreshold = Math.max(total * 0.001, 10_000);
  if (size > hugeThreshold) return "huge";
  if (size > largeThreshold) return "large";
  return "";
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

// --- rendering: record cards ---

function renderRecord(rec) {
  const node = els.cardTemplate.content.firstElementChild.cloneNode(true);
  node.dataset.idx = rec.index;
  // Set an id too for O(1) lookup via getElementById in hot paths (e.g.,
  // the affected_summaries loop after a quick action). querySelector against
  // a [data-idx="N"] attribute is O(N) over the card list, which becomes
  // hang-level on sessions with thousands of cards × hundreds of mutations.
  node.id = `card-${rec.index}`;
  node.dataset.type = rec.type;
  if (rec.type === "system" && rec.subtype === "compact_boundary") {
    node.classList.add("compact-boundary");
  }
  if (rec.is_pre_compaction) node.classList.add("pre-compaction");
  if (rec.is_plucked) node.classList.add("plucked");
  if (rec.is_mutated) node.classList.add("mutated");

  // Tag each card with the unique block types it contains (for #19 filter).
  // Stored as a sorted comma-separated string on a data attribute.
  if (rec.blocks && rec.blocks.length > 0) {
    const types = [...new Set(rec.blocks.map((b) => b.type))].sort();
    node.dataset.blockTypes = types.join(",");
  }

  node.querySelector(".idx").textContent = rec.index;
  const typeTag = node.querySelector(".type-tag");
  typeTag.textContent = rec.type;
  typeTag.dataset.type = rec.type;
  node.querySelector(".subtype").textContent = rec.subtype || "";
  const sizeEl = node.querySelector(".size");
  sizeEl.textContent = fmtBytes(rec.size);
  const sc = sizeClass(rec.size, state.totalBytes);
  if (sc) {
    sizeEl.classList.add(sc);
    if (sc === "huge") node.classList.add("size-large");
  }
  node.querySelector(".timestamp").textContent = fmtTimestamp(rec.timestamp);
  const previewEl = node.querySelector(".preview");
  previewEl.textContent = rec.preview || "";
  // store raw preview for search; we mutate innerHTML during search.
  previewEl.dataset.raw = rec.preview || "";

  // Toggle expand/collapse from header clicks only — body clicks shouldn't
  // collapse the card (lets the user select text in the expanded content
  // without losing what they're trying to copy).
  const header = node.querySelector(".card-header");
  header.addEventListener("click", (e) => {
    if (e.target.closest(".edit-btn")) return;
    if (e.target.closest(".pluck-btn")) return;
    if (e.target.closest(".reattach-badge")) return;
    toggleCard(node, rec);
  });

  const reattachBadge = node.querySelector(".reattach-badge");
  updateReattachBadge(reattachBadge, rec);
  reattachBadge.addEventListener("click", (e) => {
    e.stopPropagation();
    handleReattachBadgeClick(rec);
  });

  const editBtn = node.querySelector(".edit-btn");
  editBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (node.classList.contains("editing")) {
      exitEditMode(node, rec);
    } else {
      enterEditMode(node, rec);
    }
  });

  const pluckBtn = node.querySelector(".pluck-btn");
  pluckBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const current = state.records[rec.index] || rec;
    if (node.classList.contains("plucking")) {
      exitPluckMode(node, current);
    } else if (current.is_plucked) {
      enterUnpluckMode(node, current);
    } else {
      enterPluckMode(node, current);
    }
  });

  // Selection checkbox: stop propagation so toggling doesn't expand the card.
  const checkbox = node.querySelector(".card-checkbox");
  if (state.selectedIndices.has(rec.index)) {
    checkbox.checked = true;
    node.classList.add("selected");
  }
  checkbox.addEventListener("click", (e) => {
    e.stopPropagation();
    handleCheckboxClick(rec.index, e.shiftKey);
  });

  return node;
}

// --- backups browser ---

async function openBackupsModal() {
  els.backupsModal.hidden = false;
  await refreshBackupsModal();
}

function closeBackupsModal() {
  els.backupsModal.hidden = true;
}

async function refreshBackupsModal() {
  let data;
  try {
    data = await fetchJSON("/api/backups");
  } catch (err) {
    els.backupsList.innerHTML = `<li class="empty-state">error: ${escapeHTML(err.message)}</li>`;
    return;
  }
  els.backupsList.innerHTML = "";
  if (!data.backups.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "no backups yet — they're created on each save";
    els.backupsList.appendChild(empty);
    return;
  }
  for (const b of data.backups) {
    const li = document.createElement("li");
    const d = new Date(b.timestamp);
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    const human = d.toLocaleString("en-GB", {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }) + `.${ms}`;
    // Age uses filename-derived timestamp so a freshly-made post-restore
    // backup doesn't show as old (mtime inherits the restored content's mtime).
    const age = fmtAge(d.getTime());
    const restoreBtn = `<button class="b-restore" data-path="${escapeHTML(b.path)}">restore</button>`;
    li.innerHTML = `
      <div>
        <div class="b-timestamp">${escapeHTML(human)}</div>
        <div class="b-age">${escapeHTML(age)}</div>
      </div>
      <span class="b-size">${fmtBytes(b.size)}</span>
      ${restoreBtn}
    `;
    li.querySelector(".b-restore").addEventListener("click", () => handleRestore(b));
    els.backupsList.appendChild(li);
  }
}

function fmtAge(ms) {
  const diff = Date.now() - ms;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

async function handleRestore(backup) {
  const pendingCount = state.stats?.pending_ops_count || 0;
  const warning = pendingCount > 0
    ? `\n\nWARNING: You have ${pendingCount} pending change${pendingCount === 1 ? "" : "s"} that will be discarded.`
    : "";
  const ok = confirm(`Restore from ${backup.filename}?${warning}\n\nA fresh backup of the current state will be taken first, so this restore is itself reversible.`);
  if (!ok) return;
  try {
    const resp = await fetch("/api/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backup_path: backup.path }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    // Re-fetch everything since the file is fundamentally different
    state.stats = result.stats;
    await reloadRecordsAfterBoundaryChange();
    renderTopBar();
    renderComposition();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();
    renderFilterToggles();
    renderBlockFilterToggles();
    closeBackupsModal();
  } catch (err) {
    alert(`Restore failed: ${err.message}`);
  }
}

// --- multi-select bulk-pluck ---

function setSelectionMode(on) {
  state.selectionMode = on;
  document.body.classList.toggle("selection-mode", on);
  if (!on) clearSelection();
  els.selectionBar.hidden = !on;
}

function handleCheckboxClick(idx, shift) {
  if (shift && state.lastSelectedIdx !== null && state.lastSelectedIdx !== idx) {
    // Range select: select all VISIBLE cards between lastSelected and idx.
    // Visibility = card exists in DOM and isn't display:none.
    const lo = Math.min(state.lastSelectedIdx, idx);
    const hi = Math.max(state.lastSelectedIdx, idx);
    for (let i = lo; i <= hi; i++) {
      const card = els.records.querySelector(`.card[data-idx="${i}"]`);
      if (!card) continue;
      // Skip cards hidden by filters or search
      const cs = window.getComputedStyle(card);
      if (cs.display === "none") continue;
      addToSelection(i, card);
    }
  } else {
    const card = els.records.querySelector(`.card[data-idx="${idx}"]`);
    if (state.selectedIndices.has(idx)) {
      removeFromSelection(idx, card);
    } else {
      addToSelection(idx, card);
    }
  }
  state.lastSelectedIdx = idx;
  renderSelectionBar();
}

function addToSelection(idx, card) {
  state.selectedIndices.add(idx);
  if (card) {
    card.classList.add("selected");
    const cb = card.querySelector(".card-checkbox");
    if (cb) cb.checked = true;
  }
}

function removeFromSelection(idx, card) {
  state.selectedIndices.delete(idx);
  if (card) {
    card.classList.remove("selected");
    const cb = card.querySelector(".card-checkbox");
    if (cb) cb.checked = false;
  }
}

function clearSelection() {
  for (const idx of state.selectedIndices) {
    const card = els.records.querySelector(`.card[data-idx="${idx}"]`);
    if (card) {
      card.classList.remove("selected");
      const cb = card.querySelector(".card-checkbox");
      if (cb) cb.checked = false;
    }
  }
  state.selectedIndices.clear();
  state.lastSelectedIdx = null;
  renderSelectionBar();
}

function renderSelectionBar() {
  const n = state.selectedIndices.size;
  els.selectionCount.textContent = `${n} selected`;
  const empty = n === 0;
  els.bulkPluckBtn.disabled = empty;
  els.clearSelectionBtn.disabled = empty;
}

async function handleBulkPluck() {
  if (state.selectedIndices.size === 0) return;
  const indices = Array.from(state.selectedIndices).sort((a, b) => a - b);
  els.bulkPluckBtn.disabled = true;
  try {
    const resp = await fetch("/api/bulk-pluck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ indices }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    // Reuse the pluck-completed logic; it already handles the post-state updates.
    await onPluckCompleted(result);
    clearSelection();
  } catch (err) {
    alert(`Bulk pluck failed: ${err.message}`);
    renderSelectionBar();
  }
}

async function runQuickAction({ button, endpoint, body, confirmText, opNoun, formatSuccess }) {
  if (!window.confirm(confirmText)) return;

  button.disabled = true;
  const origLabel = button.textContent;
  button.textContent = "working…";
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(errBody.error || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    const r = result.result;

    if (result.stats) {
      state.stats = result.stats;
      state.wireBytes = result.stats.wire_messages_bytes;
      renderTopBar();
      renderComposition();
      renderTopRecords();
      renderCompactionEvents();
      renderHealth();
    }
    if (result.affected_summaries) {
      // Hot path. state.records is indexed by position (record.index ===
      // array index), so we use direct array access (O(1)) instead of
      // findIndex (O(N)). Card lookup via getElementById (O(1) browser-
      // builtin) instead of querySelector on a data-idx attribute (O(D)
      // over the card list). The naive O(N²) version hung the browser on
      // sessions with thousands of records × hundreds of mutations.
      for (const summary of result.affected_summaries) {
        if (summary.index >= 0 && summary.index < state.records.length) {
          state.records[summary.index] = summary;
        }
        const card = document.getElementById(`card-${summary.index}`);
        if (card) {
          updateCardHeader(card, summary);
          card.classList.toggle("mutated", !!summary.is_mutated);
          card.classList.toggle("plucked", !!summary.is_plucked);
        }
      }
    }

    // Support both mutation-based (n_mutated) and pluck-based (n_plucked)
    // actions; either being >0 means something happened.
    const nDone = (r.n_mutated || 0) + (r.n_plucked || 0);
    if (nDone === 0) {
      alert(`Nothing to ${opNoun} — nothing matched, or all candidate records already done.`);
    } else {
      const successMsg = formatSuccess
        ? formatSuccess(r)
        : (() => {
            const kb = ((r.bytes_freed_est || 0) / 1024).toFixed(1);
            const verb = r.n_plucked ? "plucked" : "affected";
            return `${opNoun.charAt(0).toUpperCase() + opNoun.slice(1)}: ` +
              `${nDone} records ${verb}, ~${kb} KB freed on the wire ` +
              `(after save). Open the pending list to inspect or undo.`;
          })();
      alert(successMsg);
    }
  } catch (err) {
    alert(`${opNoun} failed: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = origLabel;
  }
}


async function handleStripThinking() {
  const keepN = parseInt(els.stripThinkingKeep.value, 10);
  const keep_last_n = Number.isFinite(keepN) && keepN >= 0 ? keepN : 1;
  await runQuickAction({
    button: els.stripThinkingBtn,
    endpoint: "/api/quick-actions/strip-thinking",
    body: { keep_last_n },
    confirmText:
      `Strip thinking blocks from all chain-reachable assistant records, ` +
      `preserving the last ${keep_last_n} intact?\n\n` +
      `Reversible from the pending list. First post-save request rebuilds cache once.`,
    opNoun: "strip thinking",
  });
}


async function handleRefreshPreflight() {
  const raw = els.refreshPreflightTokens.value.trim();
  const parsed = raw === "" ? null : parseInt(raw, 10);
  const new_input_tokens =
    parsed !== null && Number.isFinite(parsed) && parsed >= 0 ? parsed : null;

  const tokenDesc = new_input_tokens === null
    ? "the auto-computed estimate (wire bytes / 4)"
    : `${new_input_tokens.toLocaleString()}`;

  await runQuickAction({
    button: els.refreshPreflightBtn,
    endpoint: "/api/quick-actions/refresh-preflight-usage",
    body: new_input_tokens === null ? {} : { new_input_tokens },
    confirmText:
      `Reset usage.input_tokens on the latest in-chain assistant to ${tokenDesc} ` +
      `(and zero cache_creation/cache_read)?\n\n` +
      `Use this when strip/trim has shrunk the wire payload but the statusline ` +
      `still reports the stale value. Reversible from the pending list. ` +
      `On your next API request, fresh usage data overwrites the tampered values.`,
    opNoun: "refresh preflight",
    formatSuccess: (r) =>
      `Refreshed preflight: latest assistant (idx ${r.target_idx})'s ` +
      `usage.input_tokens set to ${r.after_input_tokens.toLocaleString()} ` +
      `(was ${(r.before_usage.input_tokens ?? "?").toLocaleString?.() ?? "?"}). ` +
      `Cache fields zeroed. Save to commit, or undo from the pending list.`,
  });
}


async function handleTrimToolCalls() {
  const keepN = parseInt(els.trimToolCallsKeep.value, 10);
  const keep_last_n = Number.isFinite(keepN) && keepN >= 0 ? keepN : 3;
  const thresholdN = parseInt(els.trimToolCallsThreshold.value, 10);
  const threshold_bytes =
    Number.isFinite(thresholdN) && thresholdN >= 0 ? thresholdN : 500;
  const trim_images = els.trimToolCallsImages?.checked === true;
  const imagesNote = trim_images
    ? "\n\nIMAGE SUB-BLOCKS WILL ALSO BE TRIMMED — vision context for those " +
      "images will be lost."
    : "";
  await runQuickAction({
    button: els.trimToolCallsBtn,
    endpoint: "/api/quick-actions/trim-tool-calls",
    body: { keep_last_n, threshold_bytes, trim_images },
    confirmText:
      `Trim strings over ${threshold_bytes} bytes in tool_use inputs and ` +
      `tool_result content, preserving the last ${keep_last_n} records intact?` +
      `\n\nThe calls stay in the transcript; only bulky values are replaced ` +
      `with a placeholder. File paths and short fields pass through. ` +
      `Reversible from the pending list.${imagesNote}`,
    opNoun: "trim tool calls",
  });
}

async function handlePrune() {
  const kt = parseInt(els.pruneKeepThinking.value, 10);
  const keep_last_thinking = Number.isFinite(kt) && kt >= 0 ? kt : 1;
  const ko = parseInt(els.pruneKeepTools.value, 10);
  const keep_last_tools = Number.isFinite(ko) && ko >= 0 ? ko : 3;
  await runQuickAction({
    button: els.pruneBtn,
    endpoint: "/api/quick-actions/prune",
    body: { keep_last_thinking, keep_last_tools },
    confirmText:
      `Prune: strip thinking blocks (keep last ${keep_last_thinking}) AND pluck ` +
      `tool exchanges (keep last ${keep_last_tools}) in one pass?\n\n` +
      `Stages two reversible ops. Undo reverses them one at a time.`,
    opNoun: "prune",
    formatSuccess: (r) => {
      const kb = ((r.bytes_freed_est || 0) / 1024).toFixed(1);
      return `Pruned: ${r.n_thinking_stripped || 0} thinking records stripped, ` +
        `${r.n_tools_trimmed || 0} tool records trimmed, ~${kb} KB freed on the wire ` +
        `(after save). Open the pending list to inspect or undo.`;
    },
  });
}

async function toggleCard(card, rec) {
  const wasCollapsed = card.classList.contains("collapsed");
  card.classList.toggle("collapsed");
  const body = card.querySelector(".card-body");
  body.hidden = !wasCollapsed;
  if (!wasCollapsed) return;
  if (body.dataset.loaded === "true") return;

  body.innerHTML = '<div class="loading">loading…</div>';
  try {
    const raw = await getFullRecord(rec.index);
    renderCardBody(body, rec, raw);
    body.dataset.loaded = "true";
  } catch (err) {
    body.innerHTML = `<div class="loading">error: ${escapeHTML(err.message)}</div>`;
  }
}

async function getFullRecord(idx) {
  if (state.recordCache.has(idx)) return state.recordCache.get(idx);
  const raw = await fetchJSON(`/api/records/${idx}`);
  state.recordCache.set(idx, raw);
  return raw;
}

function renderCardBody(body, rec, raw) {
  body.innerHTML = "";
  if (rec.blocks && rec.blocks.length > 0) {
    const message = raw.message || {};
    // Anthropic API shorthand: message.content can be a plain string,
    // equivalent to [{type: "text", text: content}].
    let content = message.content;
    if (typeof content === "string") {
      content = [{ type: "text", text: content }];
    } else if (!Array.isArray(content)) {
      content = [];
    }
    for (const block of rec.blocks) {
      const blockData = content[block.index];
      body.appendChild(renderBlock(block, blockData));
    }
    return;
  }
  const pre = document.createElement("pre");
  pre.className = "block-body";
  pre.hidden = false;
  pre.style.background = "var(--bg-block)";
  pre.style.borderRadius = "3px";
  pre.style.padding = "8px";
  pre.textContent = JSON.stringify(raw, null, 2);
  body.appendChild(pre);
}

function renderBlock(blockSummary, blockData) {
  const node = els.blockTemplate.content.firstElementChild.cloneNode(true);
  node.querySelector(".block-idx").textContent = blockSummary.index;
  const tag = node.querySelector(".block-type-tag");
  tag.textContent = blockSummary.type;
  tag.dataset.type = blockSummary.type;
  node.querySelector(".block-size").textContent = fmtBytes(blockSummary.size);
  node.querySelector(".block-preview").textContent = blockSummary.preview;

  const bodyEl = node.querySelector(".block-body");
  // Only the header toggles. Body clicks pass through so text-selection works.
  const header = node.querySelector(".block-header");
  header.addEventListener("click", (e) => {
    e.stopPropagation();
    if (bodyEl.hidden) {
      bodyEl.hidden = false;
      bodyEl.textContent = renderBlockBody(blockSummary, blockData);
    } else {
      bodyEl.hidden = true;
    }
  });
  // Stop card-toggle propagation on body clicks too, so clicking text doesn't
  // bubble up to the card click handler (which would collapse the whole card).
  bodyEl.addEventListener("click", (e) => e.stopPropagation());
  return node;
}

// --- pluck mode ---

async function enterPluckMode(card, rec) {
  card.classList.remove("collapsed");
  const body = card.querySelector(".card-body");
  body.hidden = false;
  body.innerHTML = '<div class="loading">computing pluck set…</div>';

  let preview;
  try {
    preview = await fetchJSON(`/api/records/${rec.index}/pluck-preview`);
  } catch (err) {
    body.innerHTML = `<div class="loading">error: ${escapeHTML(err.message)}</div>`;
    return;
  }

  card.classList.add("plucking");
  const panel = els.pluckPanelTemplate.content.firstElementChild.cloneNode(true);
  const list = panel.querySelector(".pluck-list");
  const status = panel.querySelector(".pluck-status");
  const cancelBtn = panel.querySelector(".pluck-cancel");
  const confirmBtn = panel.querySelector(".pluck-confirm");

  for (const s of preview.summaries) {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="p-idx">${s.index}</span>
      <span class="p-type">${escapeHTML(s.type)}</span>
      <span class="p-size">${fmtBytes(s.size)}</span>
      <span class="p-preview">${escapeHTML(s.preview || "")}</span>
    `;
    list.appendChild(li);
  }

  confirmBtn.textContent = `pluck ${preview.pluck_indices.length} record${preview.pluck_indices.length === 1 ? "" : "s"}`;

  cancelBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    exitPluckMode(card, rec);
  });

  confirmBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    status.textContent = "plucking…";
    status.className = "pluck-status working";
    try {
      const resp = await fetch(`/api/records/${rec.index}`, { method: "DELETE" });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
        throw new Error(body.error || `HTTP ${resp.status}`);
      }
      const result = await resp.json();
      onPluckCompleted(result);
      status.textContent = `plucked ${result.plucked_indices.length}`;
      status.className = "pluck-status success";
      setTimeout(() => exitPluckMode(card, rec), 500);
    } catch (err) {
      status.textContent = `failed: ${err.message}`;
      status.className = "pluck-status error";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  body.innerHTML = "";
  body.appendChild(panel);
}

function exitPluckMode(card, rec) {
  card.classList.remove("plucking");
  const body = card.querySelector(".card-body");
  body.innerHTML = "";
  body.dataset.loaded = "false";
  card.classList.add("collapsed");
  body.hidden = true;
}

async function enterUnpluckMode(card, rec) {
  card.classList.remove("collapsed");
  const body = card.querySelector(".card-body");
  body.hidden = false;
  body.innerHTML = '<div class="loading">computing un-pluck set…</div>';

  let preview;
  try {
    preview = await fetchJSON(`/api/records/${rec.index}/unpluck-preview`);
  } catch (err) {
    body.innerHTML = `<div class="loading">error: ${escapeHTML(err.message)}</div>`;
    return;
  }

  card.classList.add("plucking");
  const panel = els.pluckPanelTemplate.content.firstElementChild.cloneNode(true);
  const header = panel.querySelector(".pluck-header");
  const list = panel.querySelector(".pluck-list");
  const explainer = panel.querySelector(".pluck-explainer");
  const status = panel.querySelector(".pluck-status");
  const cancelBtn = panel.querySelector(".pluck-cancel");
  const confirmBtn = panel.querySelector(".pluck-confirm");

  header.textContent = "about to un-pluck:";
  explainer.textContent = `Un-plucking will remove the pluck marker and restore ${preview.rethreads.length} re-threaded ${preview.rethreads.length === 1 ? "child's" : "children's"} parentUuid back to the plucked records. The records return to the chain.`;
  confirmBtn.textContent = `un-pluck ${preview.unpluck_indices.length} record${preview.unpluck_indices.length === 1 ? "" : "s"}`;

  for (const s of preview.summaries) {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="p-idx">${s.index}</span>
      <span class="p-type">${escapeHTML(s.type)}</span>
      <span class="p-size">${fmtBytes(s.size)}</span>
      <span class="p-preview">${escapeHTML(s.preview || "")}</span>
    `;
    list.appendChild(li);
  }

  cancelBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    exitPluckMode(card, rec);
  });

  confirmBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    status.textContent = "un-plucking…";
    status.className = "pluck-status working";
    try {
      const resp = await fetch(`/api/records/${rec.index}/unpluck`, { method: "POST" });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
        throw new Error(body.error || `HTTP ${resp.status}`);
      }
      const result = await resp.json();
      onUnpluckCompleted(result);
      status.textContent = `restored ${result.unplucked_indices.length}`;
      status.className = "pluck-status success";
      setTimeout(() => exitPluckMode(card, rec), 500);
    } catch (err) {
      status.textContent = `failed: ${err.message}`;
      status.className = "pluck-status error";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  body.innerHTML = "";
  body.appendChild(panel);
}

async function onUnpluckCompleted(result) {
  for (const summary of result.unplucked_summaries) {
    state.records[summary.index] = summary;
    state.recordCache.delete(summary.index);
    const card = els.records.querySelector(`.card[data-idx="${summary.index}"]`);
    if (card) {
      card.classList.remove("plucked");
      updateCardHeader(card, summary);
    }
  }
  for (const summary of result.summaries) {
    state.records[summary.index] = summary;
    state.recordCache.delete(summary.index);
    const card = els.records.querySelector(`.card[data-idx="${summary.index}"]`);
    if (card) updateCardHeader(card, summary);
  }
  if (result.stats) {
    state.stats = result.stats;
    renderTopBar();
    renderComposition();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();
  }
  // Un-plucking a compact_boundary re-flips is_pre_compaction back across
  // many records and the boundary's divider needs to come back — re-fetch
  // and re-render.
  const boundaryAffected = result.unplucked_summaries.some(
    s => s.type === "system" && s.subtype === "compact_boundary"
  );
  if (boundaryAffected) await reloadRecordsAfterBoundaryChange();
}

async function onPluckCompleted(result) {
  for (const summary of result.plucked_summaries) {
    state.records[summary.index] = summary;
    state.recordCache.delete(summary.index);
    const card = els.records.querySelector(`.card[data-idx="${summary.index}"]`);
    if (card) {
      if (summary.is_pre_compaction) card.classList.add("pre-compaction");
      if (summary.is_plucked) card.classList.add("plucked");
      updateCardHeader(card, summary);
    }
  }
  for (const summary of result.summaries) {
    state.records[summary.index] = summary;
    state.recordCache.delete(summary.index);
    const card = els.records.querySelector(`.card[data-idx="${summary.index}"]`);
    if (card) updateCardHeader(card, summary);
  }
  if (result.stats) {
    state.stats = result.stats;
    state.wireBytes = result.stats.wire_messages_bytes;
    renderTopBar();
    renderComposition();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();
  }
  // Plucking a compact_boundary flips is_pre_compaction across potentially
  // thousands of records — re-fetch and re-render so the "compaction erased"
  // view is reflected everywhere.
  const boundaryAffected = result.plucked_summaries.some(
    s => s.type === "system" && s.subtype === "compact_boundary"
  );
  if (boundaryAffected) await reloadRecordsAfterBoundaryChange();
}

async function handleBackToPicker() {
  const pending = state.stats?.pending_ops_count || 0;
  if (pending > 0) {
    const ok = confirm(`You have ${pending} pending change${pending === 1 ? "" : "s"} that will be discarded if you return to the picker. Continue?`);
    if (!ok) return;
  }
  try {
    await fetch("/api/unload", { method: "POST" });
  } catch {
    // best effort — proceed to reload anyway
  }
  window.location.reload();
}

function renderTopBar() {
  if (!state.stats) return;
  const wire = state.stats.wire_messages_count.toLocaleString();
  const total = state.stats.total_records.toLocaleString();
  els.recordCount.textContent = `${wire} wire / ${total} total records`;
  els.wireBytes.textContent = `${fmtBytes(state.stats.wire_messages_bytes)} wire`;
  els.totalBytes.textContent = `${fmtBytes(state.totalBytes)} file`;
  renderPreflightTokens();
  renderPendingControls();
}

function renderPreflightTokens() {
  const cached = state.stats.latest_input_tokens;
  const cacheCreate = state.stats.latest_cache_creation_tokens ?? 0;
  const cacheRead = state.stats.latest_cache_read_tokens ?? 0;
  if (cached == null) {
    els.preflightTokens.textContent = "no preflight";
    els.preflightTokens.classList.remove("preflight-stale");
    return;
  }
  const total = cached + cacheCreate + cacheRead;
  // Estimated wire tokens, rough: bytes / 4. Compare against preflight total
  // to detect a stale-cache disconnect.
  const wireTokens = Math.round(state.stats.wire_messages_bytes / 4);
  els.preflightTokens.textContent = `${total.toLocaleString()} preflight`;

  // Stale: the preflight total is much LARGER than current wire tokens
  // (i.e. you've shrunk the wire but the cached value hasn't refreshed).
  // Threshold: preflight > 1.5x wire AND difference > 5000 tokens.
  const stale = total > wireTokens * 1.5 && total - wireTokens > 5000;
  els.preflightTokens.classList.toggle("preflight-stale", stale);
  els.preflightTokens.title =
    `Preflight breakdown (latest assistant idx ${state.stats.latest_input_tokens_idx ?? "?"}):\n` +
    `  input_tokens: ${cached.toLocaleString()}\n` +
    `  cache_creation: ${cacheCreate.toLocaleString()}\n` +
    `  cache_read: ${cacheRead.toLocaleString()}\n` +
    `  total: ${total.toLocaleString()}\n` +
    `\nEstimated wire tokens (bytes/4): ${wireTokens.toLocaleString()}` +
    (stale ? "\n\n⚠ Preflight is significantly higher than estimated wire — likely stale after a recent strip/trim. Use 'refresh preflight usage' to reset." : "");
}

function renderPendingControls() {
  const n = state.stats ? state.stats.pending_ops_count : 0;
  els.pendingControls.dataset.pending = String(n);
  els.pendingCount.textContent = n === 0
    ? "no changes"
    : `${n} change${n === 1 ? "" : "s"} pending`;
  const disabled = n === 0;
  els.saveBtn.disabled = disabled;
  els.discardBtn.disabled = disabled;
  els.undoBtn.disabled = disabled;
  els.pendingCount.disabled = disabled;
  // If modal is open, refresh its contents to reflect current state
  if (!els.pendingModal.hidden && n > 0) {
    refreshPendingModal();
  } else if (!els.pendingModal.hidden && n === 0) {
    // No changes left — close the modal
    closePendingModal();
  }
}

async function openPendingModal() {
  if (state.stats && state.stats.pending_ops_count === 0) return;
  els.pendingModal.hidden = false;
  await refreshPendingModal();
}

function closePendingModal() {
  els.pendingModal.hidden = true;
}

async function refreshPendingModal() {
  let data;
  try {
    data = await fetchJSON("/api/pending");
  } catch (err) {
    els.pendingList.innerHTML = `<li>error: ${escapeHTML(err.message)}</li>`;
    return;
  }
  els.pendingList.innerHTML = "";
  data.ops.forEach((op, i) => {
    els.pendingList.appendChild(renderPendingOpRow(op, i));
  });
}

function renderPendingOpRow(op, i) {
  const li = document.createElement("li");
  // Bulk threshold: if the op covers many records, render as expandable summary
  const BULK_THRESHOLD = 5;
  let detail = "";
  let subdetail = "";
  let fullIndicesList = null;

  if (op.type === "pluck") {
    const idxs = op.plucked_indices;
    if (idxs.length > BULK_THRESHOLD) {
      const head = idxs.slice(0, 3).join(", ");
      detail = `${idxs.length} records (idx ${head}, … +${idxs.length - 3} more)`;
      fullIndicesList = idxs;
    } else {
      detail = `idx ${idxs.join(", ")}`;
      const previews = idxs
        .map(idx => state.records[idx]?.preview)
        .filter(Boolean)
        .map(p => p.length > 80 ? p.slice(0, 80) + "…" : p);
      if (previews.length) subdetail = previews.join(" • ");
    }
    const ch = op.children_updated.length;
    if (ch) subdetail = (subdetail ? subdetail + " — " : "") + `${ch} child${ch === 1 ? "" : "ren"} re-threaded`;
  } else if (op.type === "unpluck") {
    const idxs = op.unplucked_indices;
    if (idxs.length > BULK_THRESHOLD) {
      const head = idxs.slice(0, 3).join(", ");
      detail = `${idxs.length} records (idx ${head}, … +${idxs.length - 3} more)`;
      fullIndicesList = idxs;
    } else {
      detail = `idx ${idxs.join(", ")}`;
    }
    const ch = op.children_restored.length;
    if (ch) subdetail = `${ch} child${ch === 1 ? "" : "ren"} restored`;
  } else if (op.type === "edit") {
    detail = `idx ${op.idx}`;
    const preview = state.records[op.idx]?.preview;
    if (preview) subdetail = preview.length > 100 ? preview.slice(0, 100) + "…" : preview;
  }

  // Warning indicator: edits with non-trivial validation warnings get a
  // ⚠ next to the op type. Hover/click expands to the warning text.
  const warnings = op.warnings || [];
  const warnCount = warnings.filter(w => w.severity === "warn").length;
  const warningBadge = warnCount > 0
    ? `<span class="op-warning-badge" title="${escapeHTML(warnings.map(w => `${w.severity === "warn" ? "⚠" : "ℹ"} ${w.text}`).join("\n"))}">⚠ ${warnCount}</span>`
    : "";

  li.innerHTML = `
    <div class="op-row">
      <span class="op-num">${i + 1}</span>
      <span class="op-type" data-type="${escapeHTML(op.type)}">${escapeHTML(op.type)}</span>
      <div>
        <div class="op-detail">${escapeHTML(detail)}${warningBadge}</div>
        ${subdetail ? `<div class="op-subdetail">${escapeHTML(subdetail)}</div>` : ""}
        ${fullIndicesList ? `<details class="op-indices"><summary>show full index list</summary><div class="op-indices-list">${escapeHTML(fullIndicesList.join(", "))}</div></details>` : ""}
        ${warnings.length ? `<details class="op-warnings-detail"><summary>show ${warnings.length} warning${warnings.length === 1 ? "" : "s"}</summary><ul>${warnings.map(w => `<li data-severity="${escapeHTML(w.severity)}">${w.severity === "warn" ? "⚠" : "ℹ"} ${escapeHTML(w.text)}</li>`).join("")}</ul></details>` : ""}
      </div>
    </div>
  `;
  return li;
}

async function handleSave() {
  if (els.saveBtn.disabled) return;
  els.saveBtn.disabled = true;
  els.pendingCount.textContent = "saving…";
  try {
    const resp = await fetch("/api/save", { method: "POST" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const result = await resp.json();
    state.stats = result.stats;
    renderTopBar();
  } catch (err) {
    alert(`Save failed: ${err.message}`);
    renderPendingControls();
  }
}

async function handleDiscard() {
  if (els.discardBtn.disabled) return;
  const n = state.stats ? state.stats.pending_ops_count : 0;
  if (!confirm(`Discard all ${n} pending change${n === 1 ? "" : "s"}? This cannot be undone.`)) return;
  els.discardBtn.disabled = true;
  try {
    const resp = await fetch("/api/discard", { method: "POST" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const result = await resp.json();
    // Records may have changed structurally — full re-render
    state.records = result.records;
    state.stats = result.stats;
    state.recordCache.clear();
    rerenderAllRecords();
    renderTopBar();
    renderComposition();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();
    renderFilterToggles();
    renderBlockFilterToggles();
  } catch (err) {
    alert(`Discard failed: ${err.message}`);
    renderPendingControls();
  }
}

async function handleUndo() {
  if (els.undoBtn.disabled) return;
  els.undoBtn.disabled = true;
  try {
    const resp = await fetch("/api/undo", { method: "POST" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const result = await resp.json();
    let boundaryAffected = false;
    for (const summary of result.affected_summaries || []) {
      state.records[summary.index] = summary;
      state.recordCache.delete(summary.index);
      const card = els.records.querySelector(`.card[data-idx="${summary.index}"]`);
      if (card) {
        card.classList.toggle("plucked", summary.is_plucked);
        card.classList.toggle("pre-compaction", summary.is_pre_compaction);
        updateCardHeader(card, summary);
      }
      if (summary.type === "system" && summary.subtype === "compact_boundary") {
        boundaryAffected = true;
      }
    }
    state.stats = result.stats;
    renderTopBar();
    renderComposition();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();
    if (boundaryAffected) await reloadRecordsAfterBoundaryChange();
  } catch (err) {
    alert(`Undo failed: ${err.message}`);
    renderPendingControls();
  }
}

function rerenderAllRecords() {
  const container = els.records;
  container.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const rec of state.records) {
    // Skip the divider for plucked boundaries — the "boundary" is no longer
    // functioning (records before it are now chain-reachable again).
    if (rec.type === "system" && rec.subtype === "compact_boundary" && !rec.is_plucked) {
      frag.appendChild(renderCompactDivider(rec));
    }
    frag.appendChild(renderRecord(rec));
  }
  container.appendChild(frag);
}

async function reloadRecordsAfterBoundaryChange() {
  // Compact-boundary plucks/unplucks shift is_pre_compaction across hundreds
  // or thousands of records. Cheapest correctness: re-fetch all summaries
  // and re-render.
  const data = await fetchJSON("/api/records");
  state.records = data.records;
  state.recordCache.clear();
  rerenderAllRecords();
}

// --- edit mode ---

// --- edit-time validation ---
//
// Runs client-side against state.records on every parse-valid edit. Warnings
// are informational (save stays enabled), surfaced in a panel under the
// editor. Severity options: "warn" (likely orphan/break), "info" (FYI).

function _editValidationBlocks(rec) {
  const c = rec?.message?.content;
  return Array.isArray(c) ? c.filter((b) => b && typeof b === "object") : [];
}

function _toolUseIdsIn(rec) {
  return new Set(
    _editValidationBlocks(rec)
      .filter((b) => b.type === "tool_use")
      .map((b) => b.id)
      .filter(Boolean)
  );
}

function _toolResultIdsIn(rec) {
  return new Set(
    _editValidationBlocks(rec)
      .filter((b) => b.type === "tool_result")
      .map((b) => b.tool_use_id)
      .filter(Boolean)
  );
}

function validateRecordEdit(originalRecord, newRecord, editIdx) {
  const warnings = [];
  if (!newRecord || typeof newRecord !== "object") return warnings;

  const origUuid = originalRecord.uuid;
  const newUuid = newRecord.uuid;

  // 1. uuid changed and old uuid had other children
  if (origUuid && origUuid !== newUuid) {
    const children = state.records.filter(
      (r) => r.parent_uuid === origUuid && r.index !== editIdx
    );
    if (children.length > 0) {
      const shown = children.map((c) => c.index).slice(0, 3).join(", ");
      const more = children.length > 3 ? `, …+${children.length - 3} more` : "";
      warnings.push({
        severity: "warn",
        text: `uuid changed; ${children.length} child${children.length === 1 ? "" : "ren"} will be orphaned (idx ${shown}${more})`,
      });
    }
  }

  // 2. parentUuid changed to a uuid that doesn't resolve
  const newParent = newRecord.parentUuid;
  const origParent = originalRecord.parentUuid;
  if (newParent && newParent !== origParent) {
    const exists = state.records.some((r) => r.uuid === newParent);
    if (!exists) {
      warnings.push({
        severity: "warn",
        text: `parentUuid set to ${newParent.slice(0, 12)}…; no record with that uuid exists in this session`,
      });
    }
  }

  // 3. tool_use removed but its tool_result still exists elsewhere
  const origToolUses = _toolUseIdsIn(originalRecord);
  const newToolUses = _toolUseIdsIn(newRecord);
  for (const id of origToolUses) {
    if (newToolUses.has(id)) continue;
    const orphans = state.records.filter(
      (r) =>
        r.index !== editIdx &&
        (r.blocks || []).some(
          (b) => b.type === "tool_result" && b.tool_result_for === id
        )
    );
    if (orphans.length > 0) {
      warnings.push({
        severity: "warn",
        text: `tool_use ${id.slice(0, 12)}… removed; tool_result at idx ${orphans[0].index} now unpaired`,
      });
    }
  }

  // 4. tool_result removed but its tool_use still exists elsewhere
  const origToolResults = _toolResultIdsIn(originalRecord);
  const newToolResults = _toolResultIdsIn(newRecord);
  for (const id of origToolResults) {
    if (newToolResults.has(id)) continue;
    const orphan = state.records.find(
      (r) =>
        r.index !== editIdx &&
        (r.blocks || []).some(
          (b) => b.type === "tool_use" && b.tool_use_id === id
        )
    );
    if (orphan) {
      warnings.push({
        severity: "warn",
        text: `tool_result for ${id.slice(0, 12)}… removed; tool_use at idx ${orphan.index} now unpaired`,
      });
    }
  }

  // 5. type changed (info)
  if (originalRecord.type !== newRecord.type) {
    warnings.push({
      severity: "info",
      text: `record type changed: ${originalRecord.type} → ${newRecord.type}`,
    });
  }

  return warnings;
}

function renderEditWarnings(listEl, warnings, saveBtn) {
  listEl.innerHTML = "";
  if (warnings.length === 0) {
    listEl.hidden = true;
    saveBtn.textContent = "apply";
    return;
  }
  listEl.hidden = false;
  for (const w of warnings) {
    const li = document.createElement("li");
    li.dataset.severity = w.severity;
    const glyph = w.severity === "warn" ? "⚠" : "ℹ";
    li.innerHTML = `<span class="severity-glyph">${glyph}</span><span>${escapeHTML(w.text)}</span>`;
    listEl.appendChild(li);
  }
  saveBtn.textContent = `apply (${warnings.length} warning${warnings.length === 1 ? "" : "s"})`;
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function enterEditMode(card, rec) {
  card.classList.remove("collapsed");
  const body = card.querySelector(".card-body");
  body.hidden = false;
  body.innerHTML = '<div class="loading">loading…</div>';

  let raw;
  try {
    raw = await getFullRecord(rec.index);
  } catch (err) {
    body.innerHTML = `<div class="loading">error: ${escapeHTML(err.message)}</div>`;
    return;
  }

  // Wait for the CodeMirror module to finish loading (deferred async import).
  // Usually a no-op since the user has to interact before reaching here.
  if (!window.JsonEditor?.ready) {
    await new Promise((res) => {
      if (window.JsonEditor?.ready) return res();
      window.addEventListener("jsoneditor-ready", res, { once: true });
    });
  }

  card.classList.add("editing");
  const panel = els.editPanelTemplate.content.firstElementChild.cloneNode(true);
  const editorContainer = panel.querySelector(".edit-editor");
  const warningsList = panel.querySelector(".edit-warnings");
  const status = panel.querySelector(".edit-status");
  const cancelBtn = panel.querySelector(".edit-cancel");
  const saveBtn = panel.querySelector(".edit-save");

  body.innerHTML = "";
  body.appendChild(panel);

  const initialJson = JSON.stringify(raw, null, 2);

  // Validation is debounced so the editor stays snappy on every keystroke.
  const runValidation = debounce((newValue) => {
    let parsed;
    try {
      parsed = JSON.parse(newValue);
    } catch {
      return;  // invalid-JSON path already handles status; no validation
    }
    const warnings = validateRecordEdit(raw, parsed, rec.index);
    renderEditWarnings(warningsList, warnings, saveBtn);
  }, 150);

  const editor = window.JsonEditor.create(editorContainer, initialJson, (newValue) => {
    try {
      JSON.parse(newValue);
      editorContainer.classList.remove("invalid");
      status.textContent = "";
      status.className = "edit-status";
      saveBtn.disabled = false;
      runValidation(newValue);
    } catch (e) {
      editorContainer.classList.add("invalid");
      status.textContent = `invalid JSON: ${e.message}`;
      status.className = "edit-status error";
      saveBtn.disabled = true;
    }
  });

  cancelBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    editor.destroy();
    exitEditMode(card, rec);
  });

  saveBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    let parsed;
    try {
      parsed = JSON.parse(editor.getValue());
    } catch (err) {
      status.textContent = `invalid JSON: ${err.message}`;
      status.className = "edit-status error";
      return;
    }
    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    status.textContent = "applying…";
    status.className = "edit-status saving";
    // Re-compute the warnings at the moment of apply, ship along with the
    // record so the pending modal can surface them.
    const currentWarnings = validateRecordEdit(raw, parsed, rec.index);
    try {
      const resp = await fetch(`/api/records/${rec.index}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ record: parsed, warnings: currentWarnings }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
        throw new Error(errBody.error || `HTTP ${resp.status}`);
      }
      const result = await resp.json();
      onRecordSaved(card, rec, result);
      status.textContent = "staged";
      status.className = "edit-status success";
      setTimeout(() => {
        editor.destroy();
        exitEditMode(card, state.records[rec.index]);
      }, 600);
    } catch (err) {
      status.textContent = `apply failed: ${err.message}`;
      status.className = "edit-status error";
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  editor.focus();
}

function exitEditMode(card, rec) {
  card.classList.remove("editing");
  const body = card.querySelector(".card-body");
  body.innerHTML = "";
  body.dataset.loaded = "false";
  // Re-render the block view (will lazy-load on next toggle if needed)
  // Easier: just collapse and let the user re-expand
  card.classList.add("collapsed");
  body.hidden = true;
}

function onRecordSaved(card, oldRec, result) {
  const newSummary = result.summary;
  // Patch state.records[idx] in place
  state.records[newSummary.index] = newSummary;
  // Invalidate cache for this record
  state.recordCache.delete(newSummary.index);
  // Update the card header in place
  updateCardHeader(card, newSummary);
  // Refresh stats from the response
  if (result.stats) {
    state.stats = result.stats;
    renderTopBar();
    renderComposition();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();
    // filters stay as-is (counts could drift but rarely meaningfully)
  }
}

function updateCardHeader(card, rec) {
  card.querySelector(".idx").textContent = rec.index;
  const typeTag = card.querySelector(".type-tag");
  typeTag.textContent = rec.type;
  typeTag.dataset.type = rec.type;
  card.querySelector(".subtype").textContent = rec.subtype || "";
  const sizeEl = card.querySelector(".size");
  sizeEl.textContent = fmtBytes(rec.size);
  sizeEl.className = "size";
  const sc = sizeClass(rec.size, state.totalBytes);
  if (sc) sizeEl.classList.add(sc);
  card.classList.toggle("size-large", sc === "huge");
  card.querySelector(".timestamp").textContent = fmtTimestamp(rec.timestamp);
  const previewEl = card.querySelector(".preview");
  previewEl.textContent = rec.preview || "";
  previewEl.dataset.raw = rec.preview || "";
  card.dataset.type = rec.type;
  const badge = card.querySelector(".reattach-badge");
  if (badge) updateReattachBadge(badge, rec);
}

// Reattach classification (#32): show a small badge in the card header when
// a record's wire inclusion is NOT via plain chain walk. Clicking the badge
// triggers the corresponding surgical primitive (#30/#31) to break the
// reattach mechanism.
function updateReattachBadge(badge, rec) {
  const reason = rec.wire_inclusion_reason;
  if (!reason || reason === "chain") {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  if (reason === "sibling_merge") {
    badge.textContent = `↔${rec.sibling_count}`;
    badge.dataset.reattach = "sibling";
    badge.title =
      `This record's content reaches the wire via Claude Code's the message.id sibling merge ` +
      `message.id sibling merge. Its message.id is shared with ${rec.sibling_count + 1} ` +
      `record(s) total — content from all of them gets concatenated into one wire message.\n\n` +
      `Click to MUTATE message.id (assigns a fresh one), breaking the merge for this record. ` +
      `Reversible from the pending list.`;
  } else if (reason === "parent_reattach") {
    badge.textContent = "↰tr";
    badge.dataset.reattach = "parent";
    badge.title =
      `This record reaches the wire via Claude Code's tool_result parent-reattach ` +
      `(off-chain user record with tool_result content whose parentUuid matches an in-chain uuid).\n\n` +
      `Click to MUTATE parentUuid (assigns a fresh non-matching one), breaking the reattach. ` +
      `Reversible from the pending list.`;
  } else {
    badge.hidden = true;
  }
}

async function handleReattachBadgeClick(rec) {
  const reason = rec.wire_inclusion_reason;
  if (!reason) return;
  let endpoint, label;
  if (reason === "sibling_merge") {
    endpoint = `/api/records/${rec.index}/break-sibling-merge`;
    label = "break the message.id sibling-merge for this record";
  } else if (reason === "parent_reattach") {
    endpoint = `/api/records/${rec.index}/break-parent-reattach`;
    label = "break the tool_result parentUuid reattach for this record";
  } else {
    return;
  }
  if (!window.confirm(
    `${label.charAt(0).toUpperCase() + label.slice(1)}?\n\n` +
    `This mutates a single field on idx ${rec.index} to a fresh UUID; the record stays on disk. ` +
    `Reversible from the pending list.`,
  )) return;
  try {
    const resp = await fetch(endpoint, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    // Update the record locally + refresh stats so the badge disappears
    if (result.summary) {
      const i = state.records.findIndex((rr) => rr.index === result.summary.index);
      if (i >= 0) state.records[i] = result.summary;
      const card = els.records.querySelector(`.card[data-idx="${result.summary.index}"]`);
      if (card) {
        updateCardHeader(card, result.summary);
        if (result.summary.is_mutated) card.classList.add("mutated");
        else card.classList.remove("mutated");
      }
    }
    if (result.stats) {
      state.stats = result.stats;
      state.wireBytes = result.stats.wire_messages_bytes;
      renderTopBar();
      renderComposition();
    }
  } catch (err) {
    alert(`Failed to break reattach: ${err.message}`);
  }
}

function renderBlockBody(blockSummary, blockData) {
  if (!blockData) return "(block data not available)";
  switch (blockSummary.type) {
    case "text":
      return blockData.text || "";
    case "thinking":
      return blockData.thinking || "(thinking text redacted by server; signature only)";
    case "tool_use":
      return `${blockData.name}(\n${JSON.stringify(blockData.input, null, 2)}\n)`;
    case "tool_result": {
      const c = blockData.content;
      if (typeof c === "string") return c;
      if (Array.isArray(c)) {
        return c.map(sub => {
          if (sub && sub.type === "text") return sub.text;
          return JSON.stringify(sub, null, 2);
        }).join("\n\n");
      }
      return JSON.stringify(c, null, 2);
    }
    case "image":
      return `<image>\nmedia_type: ${blockData.source?.media_type || "?"}\ndata: ${blockData.source?.data ? `(${blockData.source.data.length} chars base64)` : "?"}`;
    default:
      return JSON.stringify(blockData, null, 2);
  }
}

// --- sidebar: composition ---

function renderComposition() {
  const blockBytes = state.stats.bytes_by_block_type;
  const total = Object.values(blockBytes).reduce((a, b) => a + b, 0);
  const ordered = Object.keys(blockBytes).sort((a, b) => blockBytes[b] - blockBytes[a]);

  els.composition.innerHTML = "";
  for (const t of ordered) {
    const bytes = blockBytes[t] || 0;
    const pct = total > 0 ? (100 * bytes / total) : 0;
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `
      <div class="bar-label">${escapeHTML(t)}</div>
      <div class="bar-track"><div class="bar-fill" data-type="${escapeHTML(t)}" style="width:${pct.toFixed(1)}%"></div></div>
      <div class="bar-value">${pct.toFixed(1)}%</div>
    `;
    els.composition.appendChild(row);
  }
}

// --- sidebar: filter toggles ---

function renderFilterToggles() {
  const types = Object.entries(state.stats.type_counts)
    .sort((a, b) => b[1] - a[1]);  // by count descending

  els.filterToggles.innerHTML = "";
  for (const [type, count] of types) {
    const isOn = !DEFAULT_HIDDEN_TYPES.has(type);
    const label = document.createElement("label");
    label.className = "filter-toggle";
    label.innerHTML = `
      <input type="checkbox" data-type="${escapeHTML(type)}" ${isOn ? "checked" : ""} />
      <span class="filter-label">${escapeHTML(type)}</span>
      <span class="filter-count">${count}</span>
    `;
    els.filterToggles.appendChild(label);

    const checkbox = label.querySelector("input");
    checkbox.addEventListener("change", () => {
      applyTypeFilter(type, checkbox.checked);
    });
    // Apply initial state
    applyTypeFilter(type, isOn);
  }
}

function applyTypeFilter(type, show) {
  const cls = `hide-${type}`;
  if (show) {
    els.records.classList.remove(cls);
  } else {
    els.records.classList.add(cls);
  }
}

// --- sidebar: filter by block sub-type (#19) ---

function renderBlockFilterToggles() {
  if (!state.stats || !els.blockFilterToggles) return;
  const counts = state.stats.block_type_counts || {};
  const types = Object.entries(counts).sort((a, b) => b[1] - a[1]);

  els.blockFilterToggles.innerHTML = "";
  for (const [btype, count] of types) {
    const isOn = !state.hiddenBlockTypes.has(btype);
    const label = document.createElement("label");
    label.className = "filter-toggle";
    label.innerHTML = `
      <input type="checkbox" data-block-type="${escapeHTML(btype)}" ${isOn ? "checked" : ""} />
      <span class="filter-label">${escapeHTML(btype)}</span>
      <span class="filter-count">${count}</span>
    `;
    els.blockFilterToggles.appendChild(label);
    const checkbox = label.querySelector("input");
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.hiddenBlockTypes.delete(btype);
      else state.hiddenBlockTypes.add(btype);
      applyBlockTypeFilters();
    });
  }
  applyBlockTypeFilters();
}

function applyBlockTypeFilters() {
  // A card is hidden iff it has block types AND every block type it has is
  // in `hiddenBlockTypes`. Cards without blocks (system, file-history etc.)
  // are not affected.
  const hidden = state.hiddenBlockTypes;
  for (const card of els.records.querySelectorAll(".card")) {
    const raw = card.dataset.blockTypes;
    if (!raw) {
      card.classList.remove("filtered-by-block");
      continue;
    }
    const types = raw.split(",");
    const anyVisible = types.some((t) => !hidden.has(t));
    if (anyVisible) {
      card.classList.remove("filtered-by-block");
    } else {
      card.classList.add("filtered-by-block");
    }
  }
}

// --- sidebar: top records ranking ---

function renderTopRecords() {
  const top = state.stats.top_records_by_size.slice(0, 15);
  els.topRecords.innerHTML = "";
  for (const [idx, size] of top) {
    const rec = state.records[idx];
    if (!rec) continue;
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="r-idx">${idx}</span>
      <span class="r-type">${escapeHTML(rec.type)}</span>
      <span class="r-preview">${escapeHTML(rec.preview || "")}</span>
      <span class="r-size">${fmtBytes(size)}</span>
    `;
    li.addEventListener("click", () => scrollToRecord(idx));
    els.topRecords.appendChild(li);
  }
}

function renderCompactDivider(rec) {
  const el = document.createElement("div");
  el.className = "compact-divider";
  el.textContent = `compact boundary · ${rec.preview || "compaction"}`;
  return el;
}

function renderCompactionEvents() {
  const events = state.stats.compaction_events || [];
  if (events.length === 0) {
    els.compactionPanel.hidden = true;
    return;
  }
  els.compactionPanel.hidden = false;
  els.compactionEvents.innerHTML = "";
  for (const ev of events) {
    const li = document.createElement("li");
    const pre = typeof ev.pre_tokens === "number" ? ev.pre_tokens.toLocaleString() : "?";
    const post = typeof ev.post_tokens === "number" ? ev.post_tokens.toLocaleString() : "?";
    const ratio = (typeof ev.pre_tokens === "number" && typeof ev.post_tokens === "number" && ev.post_tokens > 0)
      ? `${(ev.pre_tokens / ev.post_tokens).toFixed(0)}×`
      : "?";
    li.innerHTML = `
      <span class="r-idx">${ev.index}</span>
      <span class="r-type">${escapeHTML(ev.trigger || "?")}</span>
      <span class="r-preview">${pre} → ${post}</span>
      <span class="r-size">${ratio}</span>
    `;
    li.addEventListener("click", () => scrollToRecord(ev.index));
    els.compactionEvents.appendChild(li);
  }
}

function scrollToRecord(idx) {
  const card = els.records.querySelector(`.card[data-idx="${idx}"]`);
  if (!card) return;
  card.scrollIntoView({ block: "center", behavior: "smooth" });
  card.classList.remove("search-flash");
  // force reflow so animation re-fires
  void card.offsetWidth;
  card.classList.add("search-flash");
}

// --- sidebar: health ---

function renderHealth() {
  const s = state.stats;
  const items = [];

  if (s.orphan_parent_uuids.length === 0) {
    items.push({ ok: true, text: "parent chain clean" });
  } else {
    items.push({ ok: false, text: `${s.orphan_parent_uuids.length} orphaned parent refs` });
  }

  const tuOrphans = s.tool_use_without_result.length;
  const trOrphans = s.tool_result_without_use.length;
  if (tuOrphans === 0 && trOrphans === 0) {
    items.push({ ok: true, text: "tool_use ↔ tool_result paired" });
  } else {
    if (tuOrphans) items.push({ ok: false, text: `${tuOrphans} tool_use without result` });
    if (trOrphans) items.push({ ok: false, text: `${trOrphans} tool_result without use` });
  }

  els.health.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    li.className = item.ok ? "ok" : "warn";
    li.textContent = item.text;
    els.health.appendChild(li);
  }
}

// --- search ---

function applySearch(query) {
  query = query.trim().toLowerCase();
  let hitCount = 0;
  const cards = els.records.querySelectorAll(".card");

  if (!query) {
    els.records.classList.remove("search-active");
    for (const card of cards) {
      card.classList.remove("search-hit");
      const prev = card.querySelector(".preview");
      prev.textContent = prev.dataset.raw;
    }
    els.searchCount.textContent = "";
    return;
  }

  els.records.classList.add("search-active");
  for (const card of cards) {
    const prev = card.querySelector(".preview");
    const raw = prev.dataset.raw || "";
    const lower = raw.toLowerCase();
    const idx = lower.indexOf(query);
    if (idx >= 0) {
      card.classList.add("search-hit");
      // highlight all occurrences
      const escaped = escapeHTML(raw);
      const re = new RegExp(escapeRegex(query), "gi");
      prev.innerHTML = escaped.replace(re, (m) => `<mark>${escapeHTML(m)}</mark>`);
      hitCount++;
    } else {
      card.classList.remove("search-hit");
      prev.textContent = raw;
    }
  }
  els.searchCount.textContent = `${hitCount} / ${cards.length}`;
}

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// --- init ---

async function initInspector() {
  try {
    const [file, recordsResp, stats] = await Promise.all([
      fetchJSON("/api/file"),
      fetchJSON("/api/records"),
      fetchJSON("/api/stats"),
    ]);

    state.records = recordsResp.records;
    state.stats = stats;
    state.totalBytes = file.size_bytes;

    els.filename.textContent = file.filename;
    renderTopBar();

    rerenderAllRecords();

    renderComposition();
    renderFilterToggles();
    renderBlockFilterToggles();
    renderTopRecords();
    renderCompactionEvents();
    renderHealth();

    els.searchInput.addEventListener("input", () => applySearch(els.searchInput.value));
    els.saveBtn.addEventListener("click", handleSave);
    els.discardBtn.addEventListener("click", handleDiscard);
    els.undoBtn.addEventListener("click", handleUndo);
    els.pendingCount.addEventListener("click", openPendingModal);
    els.modalSaveBtn.addEventListener("click", handleSave);
    els.modalDiscardBtn.addEventListener("click", handleDiscard);
    els.modalUndoBtn.addEventListener("click", handleUndo);
    els.selectionToggle.addEventListener("click", () => setSelectionMode(!state.selectionMode));
    els.exitSelectionBtn.addEventListener("click", () => setSelectionMode(false));
    els.clearSelectionBtn.addEventListener("click", clearSelection);
    els.bulkPluckBtn.addEventListener("click", handleBulkPluck);
    els.pruneBtn.addEventListener("click", handlePrune);
    els.stripThinkingBtn.addEventListener("click", handleStripThinking);
    els.trimToolCallsBtn.addEventListener("click", handleTrimToolCalls);
    els.refreshPreflightBtn.addEventListener("click", handleRefreshPreflight);
    els.backupsBtn.addEventListener("click", openBackupsModal);
    const backupsCloseBtn = els.backupsModal.querySelector(".modal-close");
    backupsCloseBtn.addEventListener("click", closeBackupsModal);
    els.backupsModal.addEventListener("click", (e) => {
      if (e.target === els.backupsModal) closeBackupsModal();
    });
    document.getElementById("back-to-picker").addEventListener("click", handleBackToPicker);
    const closeBtn = els.pendingModal.querySelector(".modal-close");
    closeBtn.addEventListener("click", closePendingModal);
    els.pendingModal.addEventListener("click", (e) => {
      if (e.target === els.pendingModal) closePendingModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!els.pendingModal.hidden) closePendingModal();
      else if (!els.backupsModal.hidden) closeBackupsModal();
    });
    window.addEventListener("beforeunload", (e) => {
      if (state.stats && state.stats.pending_ops_count > 0) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
    wireCollapsiblePanels();
  } catch (err) {
    els.records.innerHTML = `<div class="empty">error: ${escapeHTML(err.message)}</div>`;
  }
}

function wireCollapsiblePanels() {
  for (const h2 of document.querySelectorAll("#sidebar .panel:not(.panel-static) > h2")) {
    h2.addEventListener("click", () => {
      h2.closest(".panel").classList.toggle("collapsed");
    });
  }
}

bootstrap();
