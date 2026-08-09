/* jobwatch UI behaviour. Vendored, no build step, no dependencies but htmx.
 *
 * Two jobs: keep the elapsed-time gutter honest, and make the review queue
 * clearable without touching the mouse.
 */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── elapsed time ─────────────────────────────────────────────────────
   * Rendered server-side too, so the column is correct before JS runs and
   * stays correct without it. Under prefers-reduced-motion this is a
   * poll-on-refresh rather than a live ticker (§10 quality floor).
   */
  function age(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return "—";
    var s = Math.max(0, (Date.now() - then) / 1000);
    if (s < 60) return Math.floor(s) + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h";
    return Math.floor(s / 86400) + "d";
  }

  function tick() {
    var nodes = document.querySelectorAll("[data-ts]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var text = age(el.getAttribute("data-ts"));
      if (el.textContent !== text) el.textContent = text;
      el.classList.toggle("stale", text.indexOf("s") === -1 && text.indexOf("m") === -1);
    }
  }

  tick();
  if (!reduced) setInterval(tick, 1000);
  document.body.addEventListener("htmx:afterSwap", tick);

  /* ── toast ────────────────────────────────────────────────────────────── */

  var toastEl = null;
  var toastTimer = null;
  function toast(message) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.id = "toast";
      toastEl.setAttribute("role", "status");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = message;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 1800);
  }
  window.jobwatchToast = toast;

  document.body.addEventListener("htmx:responseError", function (e) {
    toast("Request failed (" + e.detail.xhr.status + ")");
  });
  document.body.addEventListener("htmx:sendError", function () {
    toast("Server unreachable — is jobwatch still running?");
  });
  document.body.addEventListener("jobwatch:toast", function (e) {
    if (e.detail && e.detail.message) toast(e.detail.message);
  });

  /* ── review queue keyboard bindings ───────────────────────────────────
   * J/K move, M match, R reject, 1–4 tag a category, U undo.
   * Thirty items in under a minute is the target, so nothing here may wait
   * on a round trip before accepting the next key.
   */

  function typing(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  function click(selector) {
    var card = document.getElementById("review-card");
    if (!card) return false;
    var btn = card.querySelector(selector);
    if (!btn) return false;
    btn.click();
    return true;
  }

  function selectCategory(index) {
    var card = document.getElementById("review-card");
    if (!card) return;
    var buttons = card.querySelectorAll("[data-category]");
    if (index >= buttons.length) return;
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("aria-pressed", i === index ? "true" : "false");
    }
    var field = card.querySelector("input[name='category']");
    if (field) field.value = buttons[index].getAttribute("data-category");
    toast("Category: " + buttons[index].getAttribute("data-category"));
  }

  document.addEventListener("keydown", function (e) {
    if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (!document.getElementById("review-card")) return;

    var key = e.key.toLowerCase();
    var handled = true;

    if (key === "m") handled = click("[data-act='match']");
    else if (key === "r") handled = click("[data-act='reject']");
    else if (key === "j") handled = click("[data-act='next']");
    else if (key === "k") handled = click("[data-act='prev']");
    else if (key === "u") handled = click("[data-act='undo']");
    else if (key >= "1" && key <= "4") selectCategory(parseInt(key, 10) - 1);
    else if (key === "o") handled = click("[data-act='open']");
    else handled = false;

    if (handled) e.preventDefault();
  });

  /* Category buttons are also clickable, for the times you do have a mouse. */
  document.body.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-category]") : null;
    if (!btn) return;
    var buttons = btn.parentNode.querySelectorAll("[data-category]");
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i] === btn) selectCategory(i);
    }
  });
})();
