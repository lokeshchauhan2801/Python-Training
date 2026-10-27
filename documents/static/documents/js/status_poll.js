
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

  var lastStatus = {};
  var firstPass = true;
  var pollRetries = 0;
  var MAX_POLL_RETRIES = 10;

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
      if (doc.status) {
        statusCell.setAttribute("data-status", doc.status);
      }
      statusCell.style.color = doc.status === "ready"
        ? "#16a34a"
        : doc.status === "failed"
          ? "#dc2626"
          : doc.status === "processing" || doc.status === "queued"
            ? "#d97706"
            : "#6b7280";
    }

    // Flip the Retry/Stop controls live as the row changes state.
    if (window.refreshDocumentControls) {
      window.refreshDocumentControls(doc.id);
    }

    // Update backend column if included in response
    if (doc.processing_backend && row) {
      var backendCell = row.querySelector(".field-processing_backend");
      if (backendCell) {
        var BACKEND_LABELS = {
          temporal: "Temporal",
          celery: "Celery",
          "local-background": "Celery (inline)",
          django: "Django (process now)",
          kafka: "Kafka",
        };
        var label = BACKEND_LABELS[doc.processing_backend] || doc.processing_backend || "processing";
        backendCell.textContent = label;
      }
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
        pollRetries = 0;  // Reset on successful poll
        var docs = (data && data.documents) || [];
        docs.forEach(function (doc) {
          updateProgress(doc);
          var prev = lastStatus[doc.id];
          lastStatus[doc.id] = doc.status;

          // Re-enable Action button when document becomes terminal
          if (TERMINAL[doc.status] && prev && !TERMINAL[prev]) {
            if (window.enableDocumentScheduleButton) {
              window.enableDocumentScheduleButton(doc.id);
            }
          }

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
        pollRetries++;
        if (pollRetries >= MAX_POLL_RETRIES) {
          // Polling failed too many times - re-enable all Action buttons as fallback
          if (window.enableDocumentScheduleButton) {
            var toggles = document.querySelectorAll(".schedule-toggle[disabled]");
            for (var i = 0; i < toggles.length; i++) {
              var docId = toggles[i].getAttribute("data-doc-id");
              if (docId) {
                window.enableDocumentScheduleButton(docId);
              }
            }
          }
        }
      });
  }

  poll();
  window.setInterval(poll, POLL_MS);
})();
