/* Admin helpers: confirm-on-submit, submit-once forms, copy buttons and tick-all boxes (same conventions as Hello New Home HQ). */
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
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-target]");
    if (!btn) return;
    var el = document.querySelector(btn.getAttribute("data-copy-target"));
    if (!el) return;
    var text = el.value !== undefined ? el.value : el.textContent;
    var done = function () { btn.classList.add("is-done"); btn.textContent = "Copied"; setTimeout(function () { btn.classList.remove("is-done"); btn.textContent = "Copy"; }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { el.select && el.select(); });
    else if (el.select) el.select();
  });
})();
