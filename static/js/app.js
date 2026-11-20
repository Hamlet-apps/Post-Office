/* Post Office: small behaviours shared by every screen. No build step. */
(function () {
  "use strict";

  // Dialogs: any <dialog> swapped into #po-dialog-slot (or anywhere by HTMX) opens itself.
  function openDialogs(root) {
    (root || document).querySelectorAll("dialog[data-auto-open]").forEach(function (d) {
      if (!d.open) d.showModal();
    });
  }
  document.addEventListener("DOMContentLoaded", function () { openDialogs(document); dismissToasts(); });
  document.addEventListener("htmx:afterSwap", function (e) { openDialogs(e.target); });

  // <button data-open-dialog="id"> and <button data-close-dialog>
  document.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-open-dialog]");
    if (opener) {
      var d = document.getElementById(opener.getAttribute("data-open-dialog"));
      if (d && d.showModal) d.showModal();
      return;
    }
    var closer = e.target.closest("[data-close-dialog]");
    if (closer) {
      var dlg = closer.closest("dialog");
      if (dlg) { dlg.close(); if (dlg.hasAttribute("data-auto-open")) dlg.remove(); }
      return;
    }
    // <button data-copy="text"> or data-copy-from="#selector"
    var copier = e.target.closest("[data-copy],[data-copy-from]");
    if (copier) {
      var text = copier.getAttribute("data-copy");
      if (text === null) {
        var src = document.querySelector(copier.getAttribute("data-copy-from"));
        text = src ? (src.value !== undefined ? src.value : src.textContent) : "";
      }
      if (navigator.clipboard) navigator.clipboard.writeText(text.trim());
      toast(copier.getAttribute("data-copied") || "Copied");
    }
  });
  // Click on the backdrop closes a dialog.
  document.addEventListener("mousedown", function (e) {
    if (e.target.tagName === "DIALOG" && e.target.open) {
      var r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) {
        e.target.close();
        if (e.target.hasAttribute("data-auto-open")) e.target.remove();
      }
    }
  });

  // <form data-confirm="Are you sure?"> and <button data-confirm="...">
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm") || (e.submitter && e.submitter.getAttribute("data-confirm"));
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // Toasts
  function toast(text, isError) {
    var box = document.getElementById("po-toasts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "po-toast" + (isError ? " po-toast--error" : "");
    el.textContent = text;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }
  function dismissToasts() {
    document.querySelectorAll("[data-toast]").forEach(function (el) {
      setTimeout(function () { el.remove(); }, 5000);
    });
  }
  window.poToast = toast;

  // A server response can raise a toast with the HX-Trigger header: {"po-toast": "Saved"}
  document.addEventListener("po-toast", function (e) {
    var d = e.detail || {};
    toast(d.value || d.message || String(d), d.level === "error");
  });
  document.addEventListener("htmx:responseError", function () {
    toast("That did not work. Please try again.", true);
  });
})();
