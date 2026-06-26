/* login.js — posts credentials to /api/login, then redirects to ?next or /search */
(function () {
  const form = document.getElementById("login-form");
  const errBox = document.getElementById("error");
  const btn = document.getElementById("submit");

  function showError(msg) {
    errBox.textContent = msg;
    errBox.classList.add("show");
  }

  function nextTarget() {
    const next = new URLSearchParams(location.search).get("next");
    // only allow same-site relative paths
    return next && next.startsWith("/") ? next : "/search";
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errBox.classList.remove("show");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Signing in…';

    try {
      const resp = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("username").value,
          password: document.getElementById("password").value,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        showError(data.error || "Sign in failed. Please try again.");
        return;
      }
      window.location.assign(nextTarget());
    } catch (err) {
      showError("Network error — is the server running?");
    } finally {
      btn.disabled = false;
      btn.textContent = "Sign in";
    }
  });
})();
