/* datepicker.js — an interactive calendar with two ways to choose days.
 *
 * RANGE (the default, the one people know from booking sites): the first click
 * sets the start, the second sets the end, and a third starts over. Apply with
 * only a start picked means that ONE day — so a single date needs no mode
 * switch, it is just a range you stopped short of extending.
 *
 * PICK DAYS: every click toggles one day on or off, so separate days that a
 * range cannot express — the 5th, the 12th and the 20th, with nothing in
 * between — can be asked for without dragging in the fortnight they span.
 *
 * The field stays typeable: "2026-06-10", "2026-06-10 to 2026-06-20", the
 * comma-separated "2026-06-10, 2026-06-20" (two separate days) and the partial
 * "2026-06" (a whole month) all work, and anything the calendar cannot express
 * is handed back verbatim as free text for the server's substring date filter.
 *
 * Exposes window.DateRangePicker(options) -> { get, set, clear, close }.
 */
(function () {
  "use strict";

  var ISO_RE = /^\d{4}-\d{2}-\d{2}$/;
  // Two full ISO dates with almost any separator between them. Both sides are
  // anchored, so the "-" separator can never be confused with a date's own hyphens.
  // A COMMA is deliberately not in this list any more: it now means "these
  // separate days", which is what a comma means everywhere else in this app (file
  // types, meeting ids) and what someone typing one almost always intends.
  var RANGE_RE =
    /^(\d{4}-\d{2}-\d{2})\s*(?:to|\.\.+|→|–|—|~|\/|-)\s*(\d{4}-\d{2}-\d{2})$/i;
  // A typed list of separate days: every piece must be a full ISO date, or the
  // text is not a list at all and goes to the server as free text untouched.
  var LIST_SPLIT_RE = /[,;]+/;
  var MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];
  var WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
  // Recordings are filed in S3 under their IST date, so every preset ("Today",
  // "Last 7 days") is anchored to today IN IST. Anchoring to the device clock
  // would ask a US-based user for yesterday's folder for half of their day.
  var FILING_ZONE = "Asia/Kolkata";

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function iso(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function fromIso(s) {
    var p = String(s).split("-");
    return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  }
  function shift(isoDate, days) {
    var d = fromIso(isoDate);
    d.setDate(d.getDate() + days);
    return iso(d);
  }

  function todayIso() {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: FILING_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
      }).format(new Date());
    } catch (e) {
      return iso(new Date());          // no Intl/tz data — the device date will do
    }
  }

  function monthStart(isoDate) { return isoDate.slice(0, 8) + "01"; }
  function monthEnd(isoDate) {
    var d = fromIso(isoDate);
    return iso(new Date(d.getFullYear(), d.getMonth() + 1, 0));
  }

  // A list of separate days, oldest first, or null when the text is not one.
  function parseList(text) {
    var pieces = String(text || "").split(LIST_SPLIT_RE);
    if (pieces.length < 2) return null;
    var out = [];
    for (var i = 0; i < pieces.length; i++) {
      var piece = pieces[i].trim();
      if (!piece) continue;
      if (!ISO_RE.test(piece)) return null;     // one bad piece -> not a list
      if (out.indexOf(piece) === -1) out.push(piece);
    }
    return out.length > 1 ? out.sort() : null;
  }

  function label(from, to, dates) {
    // The full list, not "3 days": the field has to round-trip through
    // parseTyped(), and a summary could not be read back into a selection.
    if (dates && dates.length) return dates.join(", ");
    if (!from && !to) return "";
    if (from && to) return from === to ? from : from + " → " + to;
    return from ? "from " + from : "until " + to;
  }

  /**
   * opts:
   *   input     the text input the picker is attached to (required)
   *   onApply   called with { from, to, raw } when the user applies or clears
   */
  window.DateRangePicker = function (opts) {
    var input = opts.input;
    var onApply = opts.onApply || function () {};
    var state = { from: "", to: "", dates: [], raw: input.value || "" };
    var pending = "";                 // start of a range whose end is not chosen yet
    var hover = "";
    var mode = "range";               // "range" | "multi"
    var view = fromIso(todayIso());   // month currently on screen

    var pop = document.createElement("div");
    pop.className = "dp-pop";
    pop.hidden = true;
    pop.innerHTML =
      '<div class="dp-presets">' +
      '<button type="button" class="dp-preset" data-preset="today">Today</button>' +
      '<button type="button" class="dp-preset" data-preset="yesterday">Yesterday</button>' +
      '<button type="button" class="dp-preset" data-preset="7">Last 7 days</button>' +
      '<button type="button" class="dp-preset" data-preset="30">Last 30 days</button>' +
      '<button type="button" class="dp-preset" data-preset="month">This month</button>' +
      '<button type="button" class="dp-preset" data-preset="lastmonth">Last month</button>' +
      '<button type="button" class="dp-preset" data-preset="year">This year</button>' +
      "</div>" +
      '<div class="dp-main">' +
      '<div class="dp-modes" role="group" aria-label="How to choose days">' +
      '<button type="button" class="dp-mode is-on" data-mode="range">Range</button>' +
      '<button type="button" class="dp-mode" data-mode="multi">Pick days</button>' +
      "</div>" +
      '<div class="dp-head">' +
      '<button type="button" class="dp-nav" data-nav="-1" aria-label="Previous month">‹</button>' +
      '<select class="dp-month" aria-label="Month"></select>' +
      '<select class="dp-year" aria-label="Year"></select>' +
      '<button type="button" class="dp-nav" data-nav="1" aria-label="Next month">›</button>' +
      "</div>" +
      '<div class="dp-week">' + WEEKDAYS.map(function (w) {
        return "<span>" + w + "</span>";
      }).join("") + "</div>" +
      '<div class="dp-days"></div>' +
      '<div class="dp-foot">' +
      '<span class="dp-sel"></span><span class="dp-spacer"></span>' +
      '<button type="button" class="btn btn-ghost btn-sm" data-act="clear">Clear</button>' +
      '<button type="button" class="btn btn-primary btn-sm" data-act="apply">Apply</button>' +
      "</div></div>";

    var host = input.parentNode;
    host.classList.add("dp-field");
    host.appendChild(pop);

    var monthSel = pop.querySelector(".dp-month");
    var yearSel = pop.querySelector(".dp-year");
    var daysBox = pop.querySelector(".dp-days");
    var selText = pop.querySelector(".dp-sel");

    monthSel.innerHTML = MONTHS.map(function (m, i) {
      return '<option value="' + i + '">' + m + "</option>";
    }).join("");
    var thisYear = fromIso(todayIso()).getFullYear();
    var years = [];
    for (var y = thisYear + 1; y >= thisYear - 8; y--) years.push(y);
    yearSel.innerHTML = years.map(function (v) {
      return '<option value="' + v + '">' + v + "</option>";
    }).join("");

    // ── what the calendar is currently proposing ────────────────────────────
    function draftRange() {
      if (pending) {
        var other = hover || pending;
        return pending <= other ? { from: pending, to: other } : { from: other, to: pending };
      }
      return { from: state.from, to: state.to };
    }

    function renderDays() {
      var today = todayIso();
      var draft = draftRange();
      var first = new Date(view.getFullYear(), view.getMonth(), 1);
      var cursor = new Date(first);
      cursor.setDate(1 - first.getDay());          // back up to the Sunday of week 1
      var html = "";
      for (var i = 0; i < 42; i++) {
        var value = iso(cursor);
        var cls = ["dp-day"];
        if (cursor.getMonth() !== view.getMonth()) cls.push("is-out");
        if (value === today) cls.push("is-today");
        if (mode === "multi") {
          // Each picked day stands alone — nothing between them is selected,
          // which is the entire difference from a range.
          if (state.dates.indexOf(value) !== -1) cls.push("is-picked");
        } else {
          if (draft.from && draft.to && value > draft.from && value < draft.to) cls.push("is-in");
          if (value === draft.from) cls.push("is-start");
          if (value === draft.to) cls.push("is-end");
          if (pending && !hover && value === pending) cls.push("is-pending");
        }
        html += '<button type="button" class="' + cls.join(" ") + '" data-date="' + value +
          '">' + cursor.getDate() + "</button>";
        cursor.setDate(cursor.getDate() + 1);
      }
      daysBox.innerHTML = html;
    }

    function renderFoot() {
      if (mode === "multi") {
        var n = state.dates.length;
        selText.textContent = n
          ? n + (n === 1 ? " day: " : " days: ") + state.dates.join(", ")
          : "Click the days you want — they do not have to be next to each other";
        return;
      }
      if (pending && !state.to) {
        selText.textContent = pending + " → pick an end date, or Apply for that single day";
        return;
      }
      var text = label(state.from, state.to);
      selText.textContent = text || "No date selected";
    }

    function renderModes() {
      pop.querySelectorAll(".dp-mode").forEach(function (b) {
        b.classList.toggle("is-on", b.dataset.mode === mode);
        b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
      });
    }

    function render() {
      renderModes();
      // A year outside the fixed dropdown range (an old recording) is added on the
      // fly, so navigating there never silently snaps the selection somewhere else.
      if (!yearSel.querySelector('option[value="' + view.getFullYear() + '"]')) {
        var extra = document.createElement("option");
        extra.value = String(view.getFullYear());
        extra.textContent = String(view.getFullYear());
        yearSel.appendChild(extra);
      }
      monthSel.value = String(view.getMonth());
      yearSel.value = String(view.getFullYear());
      renderDays();
      renderFoot();
    }

    function setSelection(from, to, dates) {
      state.from = from || "";
      state.to = to || "";
      state.dates = (dates || []).slice().sort();
      mode = state.dates.length ? "multi" : "range";
      pending = "";
      hover = "";
      var anchor = state.dates[0] || state.from;
      if (anchor) view = fromIso(anchor);
      render();
    }

    function commit(from, to, dates) {
      state.from = from || "";
      state.to = to || "";
      state.dates = (dates || []).slice().sort();
      state.raw = label(state.from, state.to, state.dates);
      input.value = state.raw;
      input.classList.toggle("has-value", !!state.raw);
      pending = "";
      hover = "";
      close();
      onApply({ from: state.from, to: state.to, dates: state.dates, raw: state.raw });
    }

    // ── typed text ──────────────────────────────────────────────────────────
    function parseTyped() {
      var text = (input.value || "").trim();
      state.raw = text;
      var m = text.match(RANGE_RE);
      var list = m ? null : parseList(text);
      if (m) {
        var a = m[1], b = m[2];
        state.from = a <= b ? a : b;
        state.to = a <= b ? b : a;
        state.dates = [];
        mode = "range";
      } else if (list) {
        // "2026-06-10, 2026-06-20" is two separate days, not the fortnight
        // between them — so typing it puts the calendar into Pick days.
        state.dates = list;
        state.from = state.to = "";
        mode = "multi";
      } else if (ISO_RE.test(text)) {
        state.from = state.to = text;
        state.dates = [];
        mode = "range";
      } else {
        // "2026-06", "June", anything else: not a selection the calendar can
        // show — it goes to the server as a free-text date filter, untouched.
        state.from = state.to = "";
        state.dates = [];
        mode = "range";
      }
      input.classList.toggle("has-value", !!text);
      var anchor = state.dates[0] || state.from;
      if (anchor) view = fromIso(anchor);
      pending = "";
      hover = "";
      if (!pop.hidden) render();
    }

    // ── open / close ────────────────────────────────────────────────────────
    function open() {
      if (!pop.hidden) return;
      pending = "";
      hover = "";
      var anchor = state.dates[0] || state.from;
      if (anchor) view = fromIso(anchor);
      pop.hidden = false;
      host.classList.add("dp-open");
      render();
      // The calendar is wider than its field, so a field near the right edge would
      // push it off screen. Measure once it is laid out, and flip it if needed.
      pop.style.left = "0";
      pop.style.right = "auto";
      if (pop.getBoundingClientRect().right > document.documentElement.clientWidth - 8) {
        pop.style.left = "auto";
        pop.style.right = "0";
      }
    }
    function close() {
      pop.hidden = true;
      host.classList.remove("dp-open");
    }

    // ── events ──────────────────────────────────────────────────────────────
    input.addEventListener("focus", open);
    input.addEventListener("click", open);
    input.addEventListener("input", parseTyped);
    input.addEventListener("change", parseTyped);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !pop.hidden) { e.stopPropagation(); close(); }
      // Enter inside an open calendar means "use what I picked", not "submit the
      // form with the half-finished range still on screen".
      if (e.key === "Enter" && !pop.hidden &&
          (pending || state.from || state.dates.length)) {
        e.preventDefault();
        applyDraft();
      }
    });

    daysBox.addEventListener("click", function (e) {
      var cell = e.target.closest(".dp-day");
      if (!cell) return;
      var value = cell.dataset.date;
      if (mode === "multi") {
        var at = state.dates.indexOf(value);
        if (at === -1) state.dates.push(value); else state.dates.splice(at, 1);
        state.dates.sort();
        state.from = state.to = "";
        render();
        return;
      }
      // `pending` is set only between the two clicks of a range, so its absence
      // means this click starts a fresh selection (first click, or a third one).
      if (!pending) {
        pending = value;
        state.from = value;
        state.to = "";
      } else {
        var start = pending;
        state.from = start <= value ? start : value;
        state.to = start <= value ? value : start;
        pending = "";
      }
      hover = "";
      render();
    });
    pop.querySelectorAll(".dp-mode").forEach(function (b) {
      b.addEventListener("click", function () {
        if (mode === b.dataset.mode) return;
        mode = b.dataset.mode;
        // Carry the selection across rather than dropping it: a range collapses
        // to its two ends as pickable days, and a set of days collapses to the
        // span it covers. Switching mode by accident then costs nothing.
        if (mode === "multi") {
          var carried = [];
          if (state.from) carried.push(state.from);
          if (state.to && state.to !== state.from) carried.push(state.to);
          state.dates = carried;
          state.from = state.to = "";
        } else {
          state.from = state.dates[0] || "";
          state.to = state.dates[state.dates.length - 1] || "";
          state.dates = [];
        }
        pending = "";
        hover = "";
        render();
      });
    });

    daysBox.addEventListener("mouseover", function (e) {
      var cell = e.target.closest(".dp-day");
      if (mode === "multi") return;            // nothing is being dragged out
      if (!cell || !pending || state.to) return;
      hover = cell.dataset.date;
      renderDays();
    });
    daysBox.addEventListener("mouseleave", function () {
      if (!hover) return;
      hover = "";
      renderDays();
    });

    pop.querySelectorAll(".dp-nav").forEach(function (b) {
      b.addEventListener("click", function () {
        view = new Date(view.getFullYear(), view.getMonth() + Number(b.dataset.nav), 1);
        render();
      });
    });
    monthSel.addEventListener("change", function () {
      view = new Date(view.getFullYear(), Number(monthSel.value), 1);
      render();
    });
    yearSel.addEventListener("change", function () {
      view = new Date(Number(yearSel.value), view.getMonth(), 1);
      render();
    });

    pop.querySelectorAll(".dp-preset").forEach(function (b) {
      b.addEventListener("click", function () {
        var today = todayIso();
        var p = b.dataset.preset;
        // Every preset is a span of consecutive days, so it commits a RANGE
        // whichever mode the calendar happens to be in.
        mode = "range";
        if (p === "today") return commit(today, today, []);
        if (p === "yesterday") return commit(shift(today, -1), shift(today, -1), []);
        if (p === "7") return commit(shift(today, -6), today, []);
        if (p === "30") return commit(shift(today, -29), today, []);
        if (p === "month") return commit(monthStart(today), monthEnd(today), []);
        if (p === "lastmonth") {
          var prev = shift(monthStart(today), -1);
          return commit(monthStart(prev), monthEnd(prev), []);
        }
        if (p === "year") return commit(today.slice(0, 4) + "-01-01", today, []);
      });
    });

    function applyDraft() {
      if (mode === "multi") return commit("", "", state.dates);
      // A start with no end is a single day — that is what "Apply" means here.
      var from = state.from || pending;
      var to = state.to || from;
      if (!from) return commit("", "", []);
      commit(from, to, []);
    }

    pop.querySelector('[data-act="apply"]').addEventListener("click", applyDraft);
    pop.querySelector('[data-act="clear"]').addEventListener("click", function () {
      setSelection("", "", []);
      commit("", "", []);
    });

    // Capture phase, deliberately — and this is load-bearing, not a style choice.
    // Clicking a day re-renders the grid, and renderDays() replaces daysBox's
    // innerHTML, which destroys the very button that was clicked. A bubble-phase
    // listener runs AFTER that, by which point e.target is a detached node and
    // host.contains(e.target) answers false for a click that happened INSIDE the
    // calendar — so the picker closed on the first click of a range. That made a
    // range impossible to finish and left the field looking as though it could
    // only ever hold a single date. Capture runs before the grid is rebuilt,
    // while the clicked button is still in the tree, so the test is honest.
    document.addEventListener("click", function (e) {
      if (!pop.hidden && !host.contains(e.target)) close();
    }, true);

    parseTyped();

    return {
      element: pop,
      get: function () {
        return { from: state.from, to: state.to,
                 dates: state.dates.slice(), raw: state.raw };
      },
      set: function (from, to, dates) {
        setSelection(from, to, dates);
        state.raw = label(state.from, state.to, state.dates);
        input.value = state.raw;
        input.classList.toggle("has-value", !!state.raw);
      },
      clear: function () {
        setSelection("", "", []);
        state.raw = "";
        input.value = "";
        input.classList.remove("has-value");
        close();
      },
      close: close,
    };
  };
})();
