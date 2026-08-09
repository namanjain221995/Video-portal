/* multiselect.js — a checkbox dropdown for filters that accept several values.
 *
 * A native <select multiple> renders as an always-open scrolling box and needs
 * ctrl-click to add a second value, which people discover by accident at best.
 * This is a plain button + popup of checkboxes: click to add, click to remove,
 * with "Select all" / "Clear" shortcuts and a summary on the closed control.
 *
 * Exposes window.MultiSelect(options) -> { get, set, clear, element }.
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /**
   * opts:
   *   mount     element the control is rendered into (required)
   *   options   [{ value, label }] (required)
   *   id        id given to the toggle button, so a <label for> can target it
   *   allLabel  summary shown when nothing is picked ("All types")
   *   noun      plural noun used once too many are picked to list ("types")
   *   onChange  called with the current value array after every change
   */
  window.MultiSelect = function (opts) {
    var options = opts.options || [];
    var allLabel = opts.allLabel || "All";
    var noun = opts.noun || "selected";
    var onChange = opts.onChange || function () {};

    var wrap = document.createElement("div");
    wrap.className = "ms";
    wrap.innerHTML =
      '<button type="button" class="input ms-toggle"' + (opts.id ? ' id="' + esc(opts.id) + '"' : "") +
      ' aria-haspopup="true" aria-expanded="false">' +
      '<span class="ms-summary"></span></button>' +
      '<div class="ms-menu" hidden>' +
      '<div class="ms-menu-head">' +
      '<button type="button" class="ms-link" data-ms="all">Select all</button>' +
      '<button type="button" class="ms-link" data-ms="none">Clear</button>' +
      "</div>" +
      '<div class="ms-menu-body">' +
      options.map(function (o) {
        return '<label class="ms-option"><input type="checkbox" value="' + esc(o.value) + '">' +
          "<span>" + esc(o.label) + "</span></label>";
      }).join("") +
      "</div></div>";
    opts.mount.appendChild(wrap);

    var toggle = wrap.querySelector(".ms-toggle");
    var summary = wrap.querySelector(".ms-summary");
    var menu = wrap.querySelector(".ms-menu");
    var boxes = Array.prototype.slice.call(wrap.querySelectorAll(".ms-option input"));

    function values() {
      return boxes.filter(function (b) { return b.checked; })
        .map(function (b) { return b.value; });
    }

    function labelFor(value) {
      for (var i = 0; i < options.length; i++) {
        if (options[i].value === value) return options[i].label;
      }
      return value;
    }

    // Two picks still fit on the closed control; beyond that a count reads better
    // than a truncated list that hides which ones are actually active.
    function renderSummary() {
      var picked = values();
      if (picked.length === 0) {
        summary.textContent = allLabel;
        wrap.classList.remove("has-value");
        return;
      }
      wrap.classList.add("has-value");
      summary.textContent = picked.length <= 2
        ? picked.map(labelFor).join(", ")
        : picked.length + " " + noun;
    }

    function open() {
      menu.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      wrap.classList.add("open");
    }

    function close() {
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      wrap.classList.remove("open");
    }

    function changed() {
      renderSummary();
      onChange(values());
    }

    toggle.addEventListener("click", function () {
      if (menu.hidden) open(); else close();
    });
    boxes.forEach(function (b) { b.addEventListener("change", changed); });
    wrap.querySelector('[data-ms="all"]').addEventListener("click", function () {
      boxes.forEach(function (b) { b.checked = true; });
      changed();
    });
    wrap.querySelector('[data-ms="none"]').addEventListener("click", function () {
      boxes.forEach(function (b) { b.checked = false; });
      changed();
    });
    document.addEventListener("click", function (e) {
      if (!menu.hidden && !wrap.contains(e.target)) close();
    });
    wrap.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !menu.hidden) {
        close();
        toggle.focus();
      }
    });

    renderSummary();

    return {
      element: wrap,
      get: values,
      set: function (list) {
        var wanted = list || [];
        boxes.forEach(function (b) { b.checked = wanted.indexOf(b.value) !== -1; });
        renderSummary();
      },
      clear: function () {
        boxes.forEach(function (b) { b.checked = false; });
        renderSummary();
      },
    };
  };
})();
