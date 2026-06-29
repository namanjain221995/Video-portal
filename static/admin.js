/* admin.js — lists/creates/deletes users and manages per-user department +
   download access via /api/admin/users. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");

  // All departments the bucket exposes (from the server). Used to render the
  // checkbox grids both on the create form and on every user row.
  let allDepartments = [];

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

  async function load() {
    try {
      const resp = await fetch("/api/admin/users");
      if (resp.status === 401) { location.href = "/login"; return; }
      if (resp.status === 403) { location.href = "/search"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Could not load users.", "error"); return; }
      allDepartments = data.departments || [];
      renderDeptChecks($("new-depts"), []);     // create-form checkboxes
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

    // Fill each row's checkbox grid with that user's current departments, then wire buttons.
    box.querySelectorAll(".user-row").forEach((row, i) => {
      renderDeptChecks(row.querySelector(".user-depts"), users[i].departments || []);
      row.querySelector("[data-save]").addEventListener("click", (e) => saveAccess(row, e.currentTarget));
      row.querySelector("[data-del]").addEventListener("click", (e) =>
        deleteUser(row.dataset.user, e.currentTarget));
    });
  }

  async function saveAccess(row, btn) {
    const username = row.dataset.user;
    const departments = readDeptChecks(row.querySelector(".user-depts"));
    const can_download = row.querySelector(".perm-select").value === "download";
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const resp = await fetch("/api/admin/users/" + encodeURIComponent(username), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ departments, can_download }),
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
          can_download: $("new-perm").value === "download",
        }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Could not create user.", "error"); return; }
      showNotice(`Created user "${username}".`, "ok");
      $("create-form").reset();
      renderDeptChecks($("new-depts"), []);
      load();
    } catch (e2) {
      showNotice("Network error creating user.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Create user";
    }
  }

  $("create-form").addEventListener("submit", createUser);
  load();
})();
