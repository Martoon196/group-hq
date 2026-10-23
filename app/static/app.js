/* Admin helpers: confirm-on-submit, submit-once forms, copy buttons, tick-all boxes (same conventions as Hello New Home HQ)
   and panels loaded after the page (data-fragment-src), so a slow product never holds a page. */
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) e.preventDefault();
  });
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f.hasAttribute("data-submit-once")) return;
    if (f.dataset.submitted) { e.preventDefault(); return; }
    f.dataset.submitted = "1";
    f.querySelectorAll("button[type=submit]").forEach(function (b) { b.dataset.label = b.textContent; b.disabled = true; b.textContent = "Importing..."; });
  });
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;          // back to a page restored from the cache: let its form be sent again
    document.querySelectorAll("form[data-submit-once][data-submitted]").forEach(function (f) {
      delete f.dataset.submitted;
      f.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = false; if (b.dataset.label) b.textContent = b.dataset.label; });
    });
  });
  document.addEventListener("change", function (e) {
    var box = e.target;
    if (!box.matches || !box.matches("[data-show-password]")) return;
    box.getAttribute("data-show-password").split(",").forEach(function (sel) {
      var field = document.querySelector(sel.trim());
      if (field) field.type = box.checked ? "text" : "password";
    });
  });
  document.addEventListener("change", function (e) {
    var all = e.target;
    if (!all.matches || !all.matches("[data-check-all]")) return;
    document.querySelectorAll(all.getAttribute("data-check-all")).forEach(function (box) {
      if (!box.disabled) box.checked = all.checked;
    });
  });
  // Copy: the clipboard API where the browser allows it, else a temporary textarea and execCommand("copy"), else the
  // text is selected so a person can copy it by hand.
  function copyByTextarea(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed"; area.style.top = "0"; area.style.left = "0"; area.style.opacity = "0";
    document.body.appendChild(area);
    area.focus(); area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    document.body.removeChild(area);
    return ok;
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-target]");
    if (!btn) return;
    var el = document.querySelector(btn.getAttribute("data-copy-target"));
    if (!el) return;
    var text = el.value !== undefined ? el.value : el.textContent;
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    var done = function () {
      btn.classList.add("is-done"); btn.textContent = btn.getAttribute("data-copied") || "Copied";
      setTimeout(function () { btn.classList.remove("is-done"); btn.textContent = btn.dataset.label; }, 1800);
    };
    var fallback = function () {
      if (copyByTextarea(text)) { done(); return; }
      var details = el.closest && el.closest("details");
      if (details) details.open = true;
      if (el.select) el.select();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  });
  // Panels loaded after the page: the server renders them with each product asked at once, within a short timeout.
  // A redirect (a session that has ended) or any failure leaves a plain message instead of half a page.
  document.querySelectorAll("[data-fragment-src]").forEach(function (box) {
    var src = box.getAttribute("data-fragment-src");
    var failed = box.getAttribute("data-fragment-fail") || "Couldn't load this panel. Reload the page to try again.";
    fetch(src, { credentials: "same-origin", redirect: "manual", headers: { "Accept": "text/html" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); })
      .then(function (html) { box.innerHTML = html; box.removeAttribute("aria-busy"); })
      .catch(function () {
        var p = document.createElement("p");
        p.className = "bad-text";
        p.textContent = failed;
        box.innerHTML = "";
        box.appendChild(p);
        box.removeAttribute("aria-busy");
      });
  });
})();
