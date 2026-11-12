// Progressive enhancement only – every rule is also enforced on the server.
(function () {
  // Confirmation prompts for consequential actions.
  document.addEventListener("submit", function (e) {
    var submitter = e.submitter;
    var msg = (submitter && submitter.getAttribute("data-confirm")) || e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Leave application: live working-day count from the server's own calculation.
  var form = document.getElementById("apply-form");
  if (form) {
    var type = form.querySelector("[name=leave_type_id]");
    var from = form.querySelector("[name=from_date]");
    var to = form.querySelector("[name=to_date]");
    var half = form.querySelector("[name=half_day]");
    var out = document.getElementById("day-count");
    var hint = document.getElementById("type-hint");
    var refresh = function () {
      var opt = type.options[type.selectedIndex];
      if (hint) hint.textContent = opt ? (opt.getAttribute("data-hint") || "") : "";
      if (from.value && !to.value) to.value = from.value;
      if (half && half.value && from.value) to.value = from.value;  // a half day is a single date
      if (!type.value || !from.value || !to.value) { out.textContent = "—"; return; }
      var url = form.getAttribute("data-preview") + "?leave_type_id=" + encodeURIComponent(type.value) +
        "&from_date=" + encodeURIComponent(from.value) + "&to_date=" + encodeURIComponent(to.value) +
        "&half_day=" + encodeURIComponent(half ? half.value : "");
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok) { out.textContent = d.error; return; }
        var text = d.days + (d.days === 1 ? " day" : " days");
        var avail = opt ? opt.getAttribute("data-available") : null;
        if (avail !== null && avail !== "") {
          var left = parseFloat(avail) - d.days;
          text += ". Balance: " + parseFloat(avail) + " available, " + left + " after this request";
          if (left < 0) text += " (not enough balance)";
        }
        out.textContent = text;
      }).catch(function () { out.textContent = "—"; });
    };
    [type, from, to, half].forEach(function (el) { if (el) el.addEventListener("change", refresh); });
    refresh();
  }

  // Show / hide a password field. The button stays hidden when scripts do not run.
  document.querySelectorAll("[data-reveal]").forEach(function (btn) {
    var field = document.getElementById(btn.getAttribute("data-reveal"));
    if (!field) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var show = field.type === "password";
      field.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      field.focus();
    });
    btn.setAttribute("aria-label", "Show password");
    // never leave a password readable when the form is sent
    if (field.form) field.form.addEventListener("submit", function () { field.type = "password"; });
  });

  // Utilisation bars (widths set here because the CSP forbids inline style attributes).
  document.querySelectorAll("[data-pct]").forEach(function (el) {
    var pct = Math.max(0, Math.min(100, parseFloat(el.getAttribute("data-pct")) || 0));
    el.style.width = pct + "%";
  });

  // Phone menu: the sidebar is hidden on small screens until the Menu button is pressed.
  var toggle = document.querySelector(".menu-toggle");
  var closeNav = function () {
    document.body.classList.remove("nav-open");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  };
  if (toggle) {
    toggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  var backdrop = document.querySelector(".nav-backdrop");
  if (backdrop) backdrop.addEventListener("click", closeNav);

  // Avatar menu: close it when clicking elsewhere; Escape closes menus.
  var userMenu = document.querySelector(".usermenu");
  document.addEventListener("click", function (e) {
    if (userMenu && userMenu.open && !userMenu.contains(e.target)) userMenu.open = false;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (userMenu && userMenu.open) { userMenu.open = false; userMenu.querySelector("summary").focus(); }
    closeNav();
  });

  // Click a column heading to sort the table (lists with one body and plain rows only).
  var cellValue = function (row, i) {
    var cell = row.children[i];
    if (!cell) return "";
    var t = (cell.getAttribute("data-sort") || cell.textContent).trim();
    var n = t.replace(/[,₹\s]/g, "");
    if (/^-?\d+(\.\d+)?$/.test(n)) return parseFloat(n);
    var d = Date.parse(t.split("\n")[0]);
    if (!isNaN(d) && /\d{4}/.test(t)) return d;
    return t.toLowerCase();
  };
  document.querySelectorAll(".table-wrap > table").forEach(function (table) {
    var bodies = table.tBodies;
    if (!table.tHead || bodies.length !== 1 || table.classList.contains("ts")) return;
    var rows = Array.prototype.slice.call(bodies[0].rows);
    if (rows.length < 3 || rows.some(function (r) { return r.querySelector("td[colspan], form"); })) return;
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, i) {
      if (!th.textContent.trim()) return;
      th.classList.add("sortable");
      th.setAttribute("tabindex", "0");
      var sort = function () {
        var asc = th.getAttribute("aria-sort") !== "ascending";
        Array.prototype.forEach.call(table.tHead.rows[0].cells, function (c) { c.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        var sorted = Array.prototype.slice.call(bodies[0].rows).sort(function (a, b) {
          var x = cellValue(a, i), y = cellValue(b, i);
          if (typeof x !== typeof y) { x = String(x); y = String(y); }
          return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
        });
        sorted.forEach(function (r) { bodies[0].appendChild(r); });
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  });

  // Print buttons (inline handlers are not allowed by the content security policy).
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  // Password rules: tick them off while typing (show / hide is handled by [data-reveal] above).
  var pwForm = document.getElementById("pw-form");
  if (pwForm) {
    var np = document.getElementById("np");
    var cf = document.getElementById("cf");
    var match = document.getElementById("pw-match");
    var user = (pwForm.getAttribute("data-username") || "").toLowerCase();
    var min = parseInt(pwForm.getAttribute("data-min"), 10) || 10;
    var checks = {
      length: function (v) { return v.length >= min; },
      upper: function (v) { return /[A-Z]/.test(v); },
      lower: function (v) { return /[a-z]/.test(v); },
      digit: function (v) { return /\d/.test(v); },
      special: function (v) { return /[^A-Za-z0-9]/.test(v); },
      username: function (v) { return v.length > 0 && (!user || v.toLowerCase().indexOf(user) === -1); }
    };
    var update = function () {
      var v = np.value;
      document.querySelectorAll("#pw-rules [data-rule]").forEach(function (li) {
        var ok = checks[li.getAttribute("data-rule")](v);
        li.className = v ? (ok ? "ok" : "bad") : "";
      });
      if (!cf.value) { match.textContent = ""; match.className = "hint"; }
      else if (cf.value === v) { match.textContent = "Passwords match."; match.className = "hint ok"; }
      else { match.textContent = "Passwords do not match yet."; match.className = "hint bad"; }
    };
    np.addEventListener("input", update);
    cf.addEventListener("input", update);
  }

  // Workflow editor: show only the approver field relevant to the chosen type.
  document.querySelectorAll("[data-stage]").forEach(function (row) {
    var sel = row.querySelector(".stage-type");
    var sync = function () {
      row.querySelectorAll("[data-for]").forEach(function (el) {
        el.style.display = el.getAttribute("data-for") === sel.value ? "" : "none";
      });
    };
    sel.addEventListener("change", sync);
    sync();
  });
})();
