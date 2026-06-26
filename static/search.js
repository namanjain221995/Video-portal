/* search.js — drives the search page against the /api/* endpoints in app.py */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");
  const resultsArea = $("results-area");
  const summary = $("summary");
  const bulkBar = $("bulk-bar");
  const bulkCount = $("bulk-count");
  const bulkSize = $("bulk-size");

  // Friendly file-type categories (mirrors CATEGORY_LABELS in s3_service.py).
  const CATEGORY_SHORT = {
    video: "Video", audio: "Audio", transcript: "Transcript", chat: "Chat",
    questions: "Questions", summary: "Summary", notes: "Notes", other: "Other",
  };

  // key -> record, for the rows currently rendered
  let currentRows = new Map();
  // set of selected keys
  const selected = new Set();
  // monotonic id so a slow/old response can never clobber a newer search
  let searchSeq = 0;

  // ── helpers ──────────────────────────────────────────────────────────────
  function fmtSize(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1024) return bytes + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let n = bytes / 1024, i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i];
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showNotice(msg, kind) {
    notice.textContent = msg;
    notice.className = "notice show " + (kind === "ok" ? "notice-ok" : "notice-error");
  }
  function clearNotice() { notice.className = "notice"; }

  function showEmpty(msg, icon) {
    resultsArea.innerHTML =
      `<div class="empty"><div class="big">${icon || "🔍"}</div>${esc(msg)}</div>`;
  }

  function showLoading() {
    resultsArea.innerHTML =
      '<div class="empty"><div class="spinner spinner-lg"></div>Searching…</div>';
  }

  function setCacheInfo(cache) {
    if (!cache) return;
    const el = $("cache-info");
    if (cache.demo) { el.textContent = `${cache.count} demo records`; return; }
    if (!cache.ready) { el.textContent = "indexing bucket…"; return; }
    const age = cache.age_sec == null ? "—" : `${cache.age_sec}s ago`;
    el.textContent = `${cache.count} files · indexed ${age}`;
  }

  function fillSelect(sel, values, allLabel) {
    sel.innerHTML = `<option value="">${allLabel}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  }

  // ── load filter dropdowns (just hosts — company is free-text, types are static)
  async function loadFilters() {
    try {
      const resp = await fetch("/api/filters");
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Could not load filters.", "error"); return; }
      const cur = $("f-host").value;
      fillSelect($("f-host"), data.hosts || [], "All hosts");
      if (cur) $("f-host").value = cur;
      setCacheInfo(data.cache);
      // On a cold boot the index is still warming; hosts arrive on a quiet retry.
      if (data.cache && data.cache.ready === false) setTimeout(loadFilters, 4000);
    } catch (e) {
      showNotice("Network error loading filters — is the server running?", "error");
    }
  }

  // ── search ───────────────────────────────────────────────────────────────
  async function runSearch(e) {
    if (e) e.preventDefault();
    clearNotice();

    const params = new URLSearchParams({
      candidate: $("f-candidate").value,
      company: $("f-company").value,
      host: $("f-host").value,
      date: $("f-date").value,
      meeting_id: $("f-meeting").value,
      file_type: $("f-filetype").value,
    });

    // Empty-query guard: a blank search never hits S3 or serialises the bucket.
    if (![...params.values()].some((v) => v.trim() !== "")) {
      summary.innerHTML = "";
      selected.clear(); currentRows = new Map(); updateBulkBar();
      showEmpty("Enter a candidate, company, date, meeting ID, or pick a type, then Search.");
      return;
    }

    const seq = ++searchSeq;
    const btn = $("btn-search");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Searching…';
    showLoading();

    try {
      const resp = await fetch("/api/search?" + params.toString());
      if (seq !== searchSeq) return;                 // a newer search superseded this one
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json();
      if (seq !== searchSeq) return;
      if (!resp.ok) {
        showNotice(data.error || "Search failed.", "error");
        showEmpty("Search failed — see the message above.", "⚠️");
        return;
      }
      renderResults(data);
    } catch (err) {
      if (seq !== searchSeq) return;
      showNotice("Network error during search.", "error");
      showEmpty("Could not reach the server.", "⚠️");
    } finally {
      if (seq === searchSeq) {
        btn.disabled = false;
        btn.textContent = "Search";
      }
    }
  }

  function renderResults(data) {
    const rows = data.results || [];
    selected.clear();
    currentRows = new Map(rows.map((r) => [r.key, r]));
    updateBulkBar();

    const total = data.total != null ? data.total : data.count;
    summary.innerHTML =
      `<strong>${total}</strong> file${total === 1 ? "" : "s"} · ${fmtSize(data.total_size)} total` +
      (data.truncated ? ` <span class="trunc-note">(showing first ${data.count})</span>` : "");

    if (rows.length === 0) {
      showEmpty("No recordings match those filters.", "📭");
      return;
    }

    const body = rows.map((r) => {
      const cat = r.category || "other";
      const dl = "/api/download?key=" + encodeURIComponent(r.key);
      return `<tr data-key="${esc(r.key)}">
        <td class="col-check"><input type="checkbox" class="row-check" aria-label="Select"></td>
        <td class="candidate">${esc(r.candidate)}</td>
        <td>${esc(r.company)}</td>
        <td>${esc(r.date)}</td>
        <td>${esc(r.round)}</td>
        <td>${esc(r.meeting_id)}</td>
        <td><span class="ft-tag ft-${esc(cat)}">${esc(CATEGORY_SHORT[cat] || cat)}</span></td>
        <td class="wrap">${esc(r.filename)}</td>
        <td>${fmtSize(r.size)}</td>
        <td><a class="btn btn-ghost btn-sm" href="${dl}">⬇</a></td>
      </tr>`;
    }).join("");

    resultsArea.innerHTML = `
      <div class="table-wrap">
        <table class="results">
          <thead>
            <tr>
              <th class="col-check"><input type="checkbox" id="check-all" aria-label="Select all"></th>
              <th>Candidate</th><th>Company</th><th>Date</th><th>Round</th>
              <th>Meeting ID</th><th>Type</th><th>File</th><th>Size</th><th></th>
            </tr>
          </thead>
          <tbody>${body}</tbody>
        </table>
      </div>`;

    resultsArea.querySelectorAll(".row-check").forEach((cb) => {
      cb.addEventListener("change", onRowToggle);
    });
    $("check-all").addEventListener("change", onCheckAll);
  }

  function onRowToggle(e) {
    const key = e.target.closest("tr").dataset.key;
    if (e.target.checked) selected.add(key); else selected.delete(key);
    const all = $("check-all");
    if (all) all.checked = selected.size === currentRows.size && currentRows.size > 0;
    updateBulkBar();
  }

  function onCheckAll(e) {
    const on = e.target.checked;
    resultsArea.querySelectorAll("tr[data-key]").forEach((tr) => {
      const cb = tr.querySelector(".row-check");
      cb.checked = on;
      if (on) selected.add(tr.dataset.key); else selected.delete(tr.dataset.key);
    });
    updateBulkBar();
  }

  function updateBulkBar() {
    if (selected.size === 0) { bulkBar.style.display = "none"; return; }
    bulkBar.style.display = "flex";
    bulkCount.textContent = `${selected.size} selected`;
    let total = 0;
    selected.forEach((k) => { const r = currentRows.get(k); if (r) total += Number(r.size) || 0; });
    bulkSize.textContent = fmtSize(total);
  }

  // ── bulk zip download ────────────────────────────────────────────────────
  async function downloadZip() {
    if (selected.size === 0) return;
    const btn = $("btn-zip");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Building zip…';
    clearNotice();
    try {
      const resp = await fetch("/api/download/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: Array.from(selected) }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        showNotice(data.error || "Could not build the zip.", "error");
        return;
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "interview-recordings.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showNotice(`Downloaded ${selected.size} file(s) as a zip.`, "ok");
    } catch (err) {
      showNotice("Network error while downloading the zip.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "⬇ Download selected (.zip)";
    }
  }

  // ── refresh index ────────────────────────────────────────────────────────
  async function refreshIndex() {
    const btn = $("btn-refresh");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Refreshing…';
    try {
      const resp = await fetch("/api/refresh", { method: "POST" });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Refresh failed.", "error"); return; }
      setCacheInfo(data.cache);
      await loadFilters();
      showNotice("Index refreshed.", "ok");
    } catch (e) {
      showNotice("Network error during refresh.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "↻ Refresh index";
    }
  }

  function clearFilters() {
    ["f-candidate", "f-company", "f-date", "f-meeting", "f-host", "f-filetype"]
      .forEach((id) => ($(id).value = ""));
    selected.clear();
    currentRows = new Map();
    summary.innerHTML = "";
    clearNotice();
    updateBulkBar();
    showEmpty("Run a search to see recordings.");
  }

  // ── wire up ──────────────────────────────────────────────────────────────
  $("search-form").addEventListener("submit", runSearch);
  $("btn-clear").addEventListener("click", clearFilters);
  $("btn-refresh").addEventListener("click", refreshIndex);
  $("btn-zip").addEventListener("click", downloadZip);
  $("btn-deselect").addEventListener("click", () => {
    selected.clear();
    resultsArea.querySelectorAll(".row-check, #check-all").forEach((cb) => (cb.checked = false));
    updateBulkBar();
  });

  // Populate the host dropdown (non-blocking on the server). NO auto-search:
  // the page stays on its empty state until the user submits a query.
  loadFilters();
})();
