/* admin.js — lists/creates/deletes users via /api/admin/users */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showNotice(msg, kind) {
    notice.textContent = msg;
    notice.className = "notice show " + (kind === "ok" ? "notice-ok" : "notice-error");
  }

  async function load() {
    try {
      const resp = await fetch("/api/admin/users");
      if (resp.status === 401) { location.href = "/login"; return; }
      if (resp.status === 403) { location.href = "/search"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Could not load users.", "error"); return; }
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
    const rows = users.map((u) => `
      <tr>
        <td><strong>${esc(u.username)}</strong></td>
        <td class="user-meta">${esc(u.created_at || "—")}${u.created_by ? " · by " + esc(u.created_by) : ""}</td>
        <td style="text-align:right;">
          <button class="btn btn-danger btn-sm" data-user="${esc(u.username)}">Delete</button>
        </td>
      </tr>`).join("");
    box.innerHTML = `
      <table class="simple">
        <thead><tr><th>Username</th><th>Created</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    box.querySelectorAll("button[data-user]").forEach((btn) => {
      btn.addEventListener("click", () => deleteUser(btn.dataset.user, btn));
    });
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
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Creating…';
    try {
      const resp = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: $("new-username").value,
          password: $("new-password").value,
        }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Could not create user.", "error"); return; }
      showNotice(`Created user "${$("new-username").value}".`, "ok");
      $("create-form").reset();
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
