/*
 * Documents completion-notification poller.
 *
 * Polls the scoped status endpoint every few seconds and shows a dismissible
 * toast when a document flips to "ready" or "failed", then stops tracking that
 * id. GET only (no CSRF needed). All DOM is built with createElement/textContent
 * (never innerHTML) so document titles/errors can never be interpreted as
 * markup. The endpoint URL is read from a data attribute set by the template.
 */
(function () {
  "use strict";

  var root = document.getElementById("documents-status-poll");
  if (!root) {
    return;
  }
  var statusUrl = root.getAttribute("data-status-url");
  if (!statusUrl) {
    return;
  }

  var POLL_MS = 4000;
  var TERMINAL = { ready: true, failed: true };
  // Last status we saw per document id, so we only toast on a transition.
  var lastStatus = {};
  var firstPass = true;

  function toastContainer() {
    var existing = document.getElementById("documents-toast-container");
    if (existing) {
      return existing;
    }
    var box = document.createElement("div");
    box.id = "documents-toast-container";
    box.style.position = "fixed";
    box.style.right = "16px";
    box.style.bottom = "16px";
    box.style.zIndex = "9999";
    box.style.display = "flex";
    box.style.flexDirection = "column";
    box.style.gap = "8px";
    document.body.appendChild(box);
    return box;
  }

  function showToast(doc) {
    var card = document.createElement("div");
    card.style.minWidth = "260px";
    card.style.maxWidth = "360px";
    card.style.padding = "10px 12px";
    card.style.borderRadius = "6px";
    card.style.color = "#fff";
    card.style.fontSize = "13px";
    card.style.boxShadow = "0 2px 8px rgba(0,0,0,0.2)";
    card.style.background = doc.status === "ready" ? "#16a34a" : "#dc2626";

    var title = document.createElement("div");
    title.style.fontWeight = "600";
    title.style.marginBottom = "2px";
    title.textContent =
      doc.status === "ready"
        ? "Processed: " + doc.title
        : "Failed: " + doc.title;
    card.appendChild(title);

    var detail = document.createElement("div");
    if (doc.status === "ready") {
      detail.textContent = doc.chunk_count + " chunk(s) ready.";
    } else {
      detail.textContent = doc.processing_error || "Processing failed.";
    }
    card.appendChild(detail);

    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.style.position = "absolute";
    close.style.top = "4px";
    close.style.right = "8px";
    close.style.background = "transparent";
    close.style.border = "none";
    close.style.color = "#fff";
    close.style.cursor = "pointer";
    close.style.fontSize = "16px";
    close.addEventListener("click", function () {
      card.remove();
    });
    card.style.position = "relative";
    card.appendChild(close);

    toastContainer().appendChild(card);
    window.setTimeout(function () {
      card.remove();
    }, 10000);
  }

  function updateProgress(doc) {
    var container = document.querySelector(
      '.document-progress[data-document-id="' + String(doc.id).replace(/["\\]/g, "\\$&") + '"]'
    );
    if (!container) {
      return;
    }
    var progress = doc.status === "ready"
      ? 100
      : Math.max(0, Math.min(100, Number(doc.processing_progress) || 0));
    var remaining = 100 - progress;
    var percent = container.querySelector("[data-progress-percent]");
    var left = container.querySelector("[data-progress-remaining]");
    var stage = container.querySelector("[data-progress-stage]");
    var bar = container.querySelector("[data-progress-bar]");
    var track = container.querySelector('[role="progressbar"]');
    if (percent) { percent.textContent = progress + "% processed"; }
    if (left) { left.textContent = remaining + "% left"; }
    if (stage) {
      stage.textContent = (doc.processing_stage || doc.status || "")
        .replace(/_/g, " ")
        .replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
    }
    if (bar) { bar.style.width = progress + "%"; }
    if (track) { track.setAttribute("aria-valuenow", String(progress)); }

    var row = container.closest("tr");
    var statusCell = row && row.querySelector(".field-status_badge span");
    if (statusCell) {
      statusCell.textContent = doc.status_display || doc.status || "";
      statusCell.style.color = doc.status === "ready"
        ? "#16a34a"
        : doc.status === "failed"
          ? "#dc2626"
          : doc.status === "processing" || doc.status === "queued"
            ? "#d97706"
            : "#6b7280";
    }
  }

  function poll() {
    fetch(statusUrl, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("status " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        var docs = (data && data.documents) || [];
        docs.forEach(function (doc) {
          updateProgress(doc);
          var prev = lastStatus[doc.id];
          lastStatus[doc.id] = doc.status;
          // On the first pass we only seed the baseline so we don't toast for
          // documents that were already terminal before the page loaded.
          if (firstPass) {
            return;
          }
          if (TERMINAL[doc.status] && prev && prev !== doc.status) {
            showToast(doc);
          }
        });
        firstPass = false;
      })
      .catch(function () {
        /* transient network error — the next tick retries */
      });
  }

  poll();
  window.setInterval(poll, POLL_MS);
})();
