/* admin.js — lists/creates/deletes users and manages per-user department +
   download access via /api/admin/users. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");

  // All departments the bucket exposes (from the server). Used to render the
  // checkbox grids both on the create form and on every user row.
  let allDepartments = [];
  // department -> [hosts] from the S3 index; drives the per-department host
  // pickers (empty while the index is still warming).
  let hostsByDept = {};

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showNotice(msg, kind) {
    notice.textContent = msg;
    notice.className = "notice show " + (kind === "ok" ? "notice-ok" : "notice-error");
  }

  // Build a set of department checkboxes inside `container`, ticking `selected`.
  function renderDeptChecks(container, selected) {
    const chosen = new Set(selected || []);
    if (!allDepartments.length) {
      container.innerHTML = '<span class="user-meta">No departments configured.</span>';
      return;
    }
    container.innerHTML = allDepartments.map((d) => `
      <label class="dept-check">
        <input type="checkbox" value="${esc(d)}" ${chosen.has(d) ? "checked" : ""}>
        <span>${esc(d)}</span>
      </label>`).join("");
  }

  function readDeptChecks(container) {
    return Array.from(container.querySelectorAll('input[type="checkbox"]:checked'))
      .map((cb) => cb.value);
  }

  // Per-department host pickers: one collapsible box per SELECTED department.
  // Ticking hosts LIMITS the user to them; all unticked = the whole department.
  function renderHostBoxes(container, depts, selectedHosts) {
    selectedHosts = selectedHosts || {};
    if (!depts || !depts.length) {
      container.innerHTML =
        '<span class="user-meta">Select a department above to restrict its hosts.</span>';
      return;
    }
    container.innerHTML = depts.map((d) => {
      const hosts = hostsByDept[d] || [];
      const chosen = new Set(selectedHosts[d] || []);
      // Union: granted hosts missing from the (possibly still-warming) index stay
      // visible and ticked, so saving never silently drops an existing restriction.
      const display = hosts.slice();
      chosen.forEach((h) => { if (display.indexOf(h) === -1) display.push(h); });
      const inner = display.length
        ? display.map((h) => `
            <label class="dept-check">
              <input type="checkbox" value="${esc(h)}"${chosen.has(h) ? " checked" : ""}>
              <span>${esc(h)}</span>
            </label>`).join("")
        : '<span class="user-meta">No hosts indexed yet for this department.</span>';
      const n = chosen.size;
      const sum = n ? `${n} host${n === 1 ? "" : "s"} only` : "All hosts";
      return `
        <details class="host-box" data-dept="${esc(d)}"${n ? " open" : ""}>
          <summary>${esc(d)} — <span class="host-sum">${esc(sum)}</span></summary>
          <p class="user-meta host-hint">Tick hosts to limit this user to them; leave all unticked for the whole department.</p>
          <div class="host-list dept-checks">${inner}</div>
        </details>`;
    }).join("");
    // Live "N hosts only / All hosts" summary as boxes are ticked.
    container.querySelectorAll(".host-box").forEach((det) => {
      det.addEventListener("change", () => {
        const n = det.querySelectorAll("input:checked").length;
        det.querySelector(".host-sum").textContent =
          n ? `${n} host${n === 1 ? "" : "s"} only` : "All hosts";
      });
    });
  }

  function readHostChecks(container) {
    const out = {};
    container.querySelectorAll(".host-box").forEach((det) => {
      const picked = Array.from(det.querySelectorAll("input:checked")).map((cb) => cb.value);
      if (picked.length) out[det.dataset.dept] = picked;
    });
    return out;
  }

  // ── Individually shared meetings ───────────────────────────────────────────
  // A department grant is all-or-nothing; this shares ONE meeting (every file of
  // it) with someone who should not get the whole department, with its own
  // view/download choice. Grants are stored as { meeting_id, can_download }.
  function meetingLabel(detail, meetingId) {
    if (!detail) return "not in the current index";
    const who = (detail.candidates || []).slice(0, 2).join(", ");
    const when = (detail.dates || [])[0] || "";
    const dept = (detail.departments || []).join(", ");
    const bits = [who, when, dept].filter(Boolean);
    const files = `${detail.files} file${detail.files === 1 ? "" : "s"}`;
    return bits.join(" · ") + " · " + files;
  }

  function renderChips(box) {
    const grants = box._grants || [];
    if (!grants.length) {
      box.innerHTML = '<span class="user-meta">No individual meetings shared.</span>';
      return;
    }
    box.innerHTML = grants.map((g, i) => {
      const detail = g.detail;
      // A recurring Zoom id is reused by every session booked under it, so
      // granting it shares them all. Say so rather than let it surprise anyone.
      const repeat = detail && detail.occurrences > 1
        ? `<span class="mg-warn" title="This meeting ID was reused across ${detail.occurrences} sessions — all of them are shared">⚠ ${detail.occurrences} sessions</span>`
        : "";
      return `
        <div class="mg-chip" data-i="${i}">
          <div class="mg-chip-main">
            <strong>${esc(g.meeting_id)}</strong> ${repeat}
            <span class="user-meta">${esc(meetingLabel(detail, g.meeting_id))}</span>
          </div>
          <label class="mg-dl" title="Allow downloading this meeting's files">
            <input type="checkbox" data-dl ${g.can_download ? "checked" : ""}><span>Download</span>
          </label>
          <button type="button" class="mg-remove" data-remove title="Stop sharing this meeting">✕</button>
        </div>`;
    }).join("");

    box.querySelectorAll(".mg-chip").forEach((chip) => {
      const i = Number(chip.dataset.i);
      chip.querySelector("[data-dl]").addEventListener("change", (e) => {
        box._grants[i].can_download = e.target.checked;
      });
      chip.querySelector("[data-remove]").addEventListener("click", () => {
        box._grants.splice(i, 1);
        renderChips(box);
      });
    });
  }

  function renderMeetingResults(panel, box, meetings, ready) {
    if (!meetings.length) {
      panel.innerHTML = ready
        ? '<div class="mg-empty">No meetings match that.</div>'
        : '<div class="mg-empty">The bucket index is still building — try again shortly.</div>';
      panel.hidden = false;
      return;
    }
    panel.innerHTML = meetings.map((m, i) => `
      <button type="button" class="mg-result" data-i="${i}">
        <span class="mg-result-id">${esc(m.meeting_id)}</span>
        <span class="mg-result-meta">${esc(meetingLabel(m, m.meeting_id))}</span>
      </button>`).join("");
    panel.hidden = false;
    panel.querySelectorAll(".mg-result").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = meetings[Number(btn.dataset.i)];
        const grants = box._grants;
        if (!grants.some((g) => g.meeting_id === m.meeting_id)) {
          // Default to view-only: sharing one meeting is usually about letting
          // someone WATCH it, and download is the choice that needs a deliberate tick.
          grants.push({ meeting_id: m.meeting_id, can_download: false, detail: m });
          renderChips(box);
        }
        panel.hidden = true;
      });
    });
  }

  function wireMeetingPicker(root, grants) {
    const input = root.querySelector(".mg-q");
    const panel = root.querySelector(".mg-results");
    const box = root.querySelector(".mg-chips");
    box._grants = (grants || []).map((g) => ({
      meeting_id: g.meeting_id, can_download: !!g.can_download, detail: g.detail,
    }));
    renderChips(box);

    let timer = 0;
    let seq = 0;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { panel.hidden = true; return; }
      // Debounced: each lookup walks the whole index server-side, so a keystroke
      // per request would put real load on the box for no benefit.
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const resp = await fetch("/api/admin/meetings?q=" + encodeURIComponent(q));
          if (mine !== seq) return;            // a newer keystroke won
          const data = await resp.json();
          if (mine !== seq) return;
          if (!resp.ok) { panel.hidden = true; return; }
          renderMeetingResults(panel, box, data.meetings || [], data.ready !== false);
        } catch (e) { panel.hidden = true; }
      }, 300);
    });
    document.addEventListener("click", (e) => {
      if (!panel.hidden && !root.contains(e.target)) panel.hidden = true;
    });
  }

  function readMeetingGrants(root) {
    const box = root.querySelector(".mg-chips");
    return (box._grants || []).map((g) => ({
      meeting_id: g.meeting_id, can_download: !!g.can_download,
    }));
  }

  // The results panel is anchored to the INPUT, not to the whole widget, or it
  // would drop below the chip list instead of under what was typed.
  const MEETING_PICKER_HTML = `
    <div class="meeting-grant">
      <div class="mg-search">
        <input class="input mg-q" type="search" autocomplete="off"
               placeholder="Find a meeting — ID, candidate, company or date">
        <div class="mg-results" hidden></div>
      </div>
      <div class="mg-chips"></div>
    </div>`;

  // Keep a host container in sync with its department checkboxes (preserving
  // any host ticks already made for departments that stay selected).
  function wireHostSync(deptBox, hostBox) {
    deptBox.addEventListener("change", () => {
      renderHostBoxes(hostBox, readDeptChecks(deptBox), readHostChecks(hostBox));
    });
  }

  async function load() {
    try {
      const resp = await fetch("/api/admin/users");
      if (resp.status === 401) { location.href = "/login"; return; }
      if (resp.status === 403) { location.href = "/search"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Could not load users.", "error"); return; }
      allDepartments = data.departments || [];
      hostsByDept = data.hosts_by_department || {};
      renderDeptChecks($("new-depts"), []);     // create-form checkboxes
      renderHostBoxes($("new-hosts"), [], {});
      renderUsers(data.users || []);
      renderAdmins(data.admins || []);
    } catch (e) {
      showNotice("Network error loading users.", "error");
    }
  }

  function renderAdmins(admins) {
    $("admins-list").innerHTML = admins.length
      ? admins.map((a) => `<span class="badge badge-admin" style="margin:0 6px 6px 0;">${esc(a)}</span>`).join("")
      : '<p class="user-meta">No admins found in .env.</p>';
  }

  function renderUsers(users) {
    const box = $("users-table");
    if (users.length === 0) {
      box.innerHTML = '<div class="empty"><div class="big">👤</div>No users yet — add one on the right.</div>';
      return;
    }
    box.innerHTML = users.map((u) => {
      const depts = u.departments || [];
      const access = u.can_download ? "Can download" : "View only";
      const accessClass = u.can_download ? "badge-admin" : "badge-muted";
      return `
        <div class="user-row" data-user="${esc(u.username)}">
          <div class="user-row-head">
            <strong>${esc(u.username)}</strong>
            <span class="badge ${accessClass}">${access}</span>
            <span class="spacer"></span>
            <span class="user-meta">${esc(u.created_at || "—")}${u.created_by ? " · by " + esc(u.created_by) : ""}</span>
          </div>
          <div class="user-access">
            <div class="dept-checks user-depts"></div>
            <div class="host-restrict user-hosts"></div>
            <details class="mg-box user-meetings"${(u.meetings || []).length ? " open" : ""}>
              <summary>Shared meetings — <span class="host-sum">${
                (u.meetings || []).length
                  ? `${u.meetings.length} meeting${u.meetings.length === 1 ? "" : "s"}`
                  : "none"}</span></summary>
              <p class="user-meta host-hint">Give access to one meeting on its own, from any department — even one this user cannot otherwise browse.</p>
              ${MEETING_PICKER_HTML}
            </details>
            <div class="user-access-actions">
              <select class="input perm-select">
                <option value="view"${u.can_download ? "" : " selected"}>View only</option>
                <option value="download"${u.can_download ? " selected" : ""}>Can download</option>
              </select>
              <button class="btn btn-ghost btn-sm" data-save>Save</button>
              <button class="btn btn-danger btn-sm" data-del>Delete</button>
            </div>
          </div>
        </div>`;
    }).join("");

    // Fill each row's checkbox grid with that user's current departments + host
    // restriction, then wire buttons.
    box.querySelectorAll(".user-row").forEach((row, i) => {
      const deptBox = row.querySelector(".user-depts");
      const hostBox = row.querySelector(".user-hosts");
      renderDeptChecks(deptBox, users[i].departments || []);
      renderHostBoxes(hostBox, users[i].departments || [], users[i].hosts || {});
      wireHostSync(deptBox, hostBox);
      wireMeetingPicker(row.querySelector(".user-meetings"), users[i].meetings || []);
      row.querySelector("[data-save]").addEventListener("click", (e) => saveAccess(row, e.currentTarget));
      row.querySelector("[data-del]").addEventListener("click", (e) =>
        deleteUser(row.dataset.user, e.currentTarget));
    });
  }

  async function saveAccess(row, btn) {
    const username = row.dataset.user;
    const departments = readDeptChecks(row.querySelector(".user-depts"));
    const hosts = readHostChecks(row.querySelector(".user-hosts"));
    const meetings = readMeetingGrants(row.querySelector(".user-meetings"));
    const can_download = row.querySelector(".perm-select").value === "download";
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const resp = await fetch("/api/admin/users/" + encodeURIComponent(username), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ departments, hosts, meetings, can_download }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Could not save access.", "error"); return; }
      showNotice(`Saved access for "${username}".`, "ok");
      load();
    } catch (e) {
      showNotice("Network error saving access.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Save";
    }
  }

  async function deleteUser(username, btn) {
    if (!confirm(`Delete user "${username}"? They will no longer be able to sign in.`)) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const resp = await fetch("/api/admin/users/" + encodeURIComponent(username), { method: "DELETE" });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Delete failed.", "error"); btn.disabled = false; btn.textContent = "Delete"; return; }
      showNotice(`Deleted user "${username}".`, "ok");
      load();
    } catch (e) {
      showNotice("Network error during delete.", "error");
      btn.disabled = false; btn.textContent = "Delete";
    }
  }

  async function createUser(e) {
    e.preventDefault();
    const btn = $("btn-create");
    const username = $("new-username").value;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Creating…';
    try {
      const resp = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          password: $("new-password").value,
          departments: readDeptChecks($("new-depts")),
          hosts: readHostChecks($("new-hosts")),
          meetings: readMeetingGrants($("new-meetings")),
          can_download: $("new-perm").value === "download",
        }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Could not create user.", "error"); return; }
      showNotice(`Created user "${username}".`, "ok");
      $("create-form").reset();
      renderDeptChecks($("new-depts"), []);
      renderHostBoxes($("new-hosts"), [], {});
      resetMeetingPicker();      // form.reset() cannot clear chips it never owned
      load();
    } catch (e2) {
      showNotice("Network error creating user.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Create user";
    }
  }

  // The create form's picker is built from the same markup as the per-user ones,
  // so the two can never drift apart.
  function resetMeetingPicker() {
    $("new-meetings").innerHTML = MEETING_PICKER_HTML;
    wireMeetingPicker($("new-meetings"), []);
  }

  $("create-form").addEventListener("submit", createUser);
  wireHostSync($("new-depts"), $("new-hosts"));
  resetMeetingPicker();
  load();
})();
