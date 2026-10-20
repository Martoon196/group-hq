/* Admin helpers: confirm-on-submit and copy buttons (same conventions as Hello New Home HQ). */
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) e.preventDefault();
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
