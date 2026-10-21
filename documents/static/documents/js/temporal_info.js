/*
 * Per-row "Temporal workflow info" modal for the Documents changelist.
 *
 * Each row renders an "Info" pill (class "info-action", data-doc-id) via the
 * DocumentAdmin.info_action @action. This script delegates clicks on that pill
 * (preventDefault so it never navigates), fetches the scoped temporal-info
 * endpoint, and opens a modal showing the workflow id/status/run id/started/
 * closed/result plus a Stages table (Stage | Status | Detail).
 *
 * All DOM is built with createElement/textContent/appendChild (never raw HTML
 * assignment) so workflow ids, stage names, and error details cannot be
 * interpreted as markup. GET only with credentials:"same-origin" +
 * X-Requested-With (the CSRF
 * cookie rides along; no token needed for a read). While the workflow status is
 * "RUNNING" the modal re-fetches every ~2000ms; it stops on a terminal/unknown
 * status or when the modal closes. Close button (×), Escape, and a backdrop
 * click all close the modal and restore focus to the triggering pill.
 */
(function () {
  "use strict";

  var root = document.getElementById("documents-temporal-info-root");
  if (!root) {
    return;
  }
  var urlTemplate = root.getAttribute("data-info-url-template");
  if (!urlTemplate) {
    return;
  }

  var POLL_MS = 2000;
  // Statuses that keep polling. Anything else (COMPLETED/FAILED/TERMINATED/
  // CANCELED/TIMED_OUT/CONTINUED_AS_NEW/unknown/n/a/...) stops the single timer.
  var POLLING_STATUSES = { RUNNING: true };

  var STATUS_COLORS = {
    completed: "#16a34a",
    running: "#6b7280",
    pending: "#6b7280",
    failed: "#dc2626",
  };

  // Module-level singletons: only one modal + one poll timer at a time.
  var overlayEl = null;
  var pollTimer = null;
  var currentDocId = null;
  var triggerEl = null;
  var bodyEl = null; // the scrollable card body we rebuild on each fetch

  function infoUrl(docId) {
    return urlTemplate.replace("__PK__", encodeURIComponent(docId));
  }

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function closeModal() {
    stopPolling();
    if (overlayEl && overlayEl.parentNode) {
      overlayEl.parentNode.removeChild(overlayEl);
    }
    overlayEl = null;
    bodyEl = null;
    currentDocId = null;
    document.removeEventListener("keydown", onKeydown);
    if (triggerEl && typeof triggerEl.focus === "function") {
      triggerEl.focus();
    }
    triggerEl = null;
  }

  function onKeydown(event) {
    if (event.key === "Escape" || event.key === "Esc") {
      closeModal();
    }
  }

  // Build the fixed overlay + dialog card shell once; returns the card body
  // element (cleared/rebuilt on every fetch) so poll refreshes are cheap.
  function buildShell() {
    var overlay = document.createElement("div");
    overlay.style.position = "fixed";
    overlay.style.top = "0";
    overlay.style.left = "0";
    overlay.style.right = "0";
    overlay.style.bottom = "0";
    overlay.style.background = "rgba(0,0,0,0.5)";
    overlay.style.zIndex = "9999";
    overlay.style.display = "flex";
    overlay.style.alignItems = "flex-start";
    overlay.style.justifyContent = "center";
    overlay.style.padding = "48px 16px";
    overlay.style.overflow = "auto";
    // Backdrop click (on the overlay itself, not the card) closes the modal.
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) {
        closeModal();
      }
    });

    var card = document.createElement("div");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-label", "Temporal workflow info");
    card.style.position = "relative";
    card.style.background = "#fff";
    card.style.color = "#111827";
    card.style.borderRadius = "8px";
    card.style.boxShadow = "0 10px 30px rgba(0,0,0,0.3)";
    card.style.maxWidth = "640px";
    card.style.width = "100%";
    card.style.padding = "20px 22px";
    card.style.boxSizing = "border-box";

    var heading = document.createElement("div");
    heading.textContent = "Temporal workflow info";
    heading.style.fontSize = "16px";
    heading.style.fontWeight = "700";
    heading.style.marginBottom = "12px";
    heading.style.paddingRight = "24px";
    card.appendChild(heading);

    var close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.textContent = "×";
    close.style.position = "absolute";
    close.style.top = "12px";
    close.style.right = "14px";
    close.style.background = "transparent";
    close.style.border = "none";
    close.style.color = "#111827";
    close.style.cursor = "pointer";
    close.style.fontSize = "22px";
    close.style.lineHeight = "1";
    close.addEventListener("click", closeModal);
    card.appendChild(close);

    var body = document.createElement("div");
    body.style.fontSize = "13px";
    card.appendChild(body);

    overlay.appendChild(card);
    document.body.appendChild(overlay);
    overlayEl = overlay;
    bodyEl = body;

    // Move focus into the dialog for keyboard users.
    close.focus();
    return body;
  }

  // One "Label: value" row.
  function labeledRow(label, value) {
    var row = document.createElement("div");
    row.style.display = "flex";
    row.style.gap = "8px";
    row.style.padding = "3px 0";

    var key = document.createElement("span");
    key.textContent = label;
    key.style.fontWeight = "600";
    key.style.minWidth = "100px";
    row.appendChild(key);

    var val = document.createElement("span");
    val.textContent = value === null || value === undefined || value === ""
      ? "—"
      : String(value);
    val.style.wordBreak = "break-all";
    row.appendChild(val);

    return row;
  }

  function buildStagesTable(stages) {
    var wrap = document.createElement("div");
    wrap.style.marginTop = "14px";

    var caption = document.createElement("div");
    caption.textContent = "Stages";
    caption.style.fontWeight = "600";
    caption.style.marginBottom = "6px";
    wrap.appendChild(caption);

    var table = document.createElement("table");
    table.style.width = "100%";
    table.style.borderCollapse = "collapse";
    table.style.fontSize = "12px";

    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Stage", "Status", "Detail"].forEach(function (text) {
      var th = document.createElement("th");
      th.textContent = text;
      th.style.textAlign = "left";
      th.style.borderBottom = "1px solid #e5e7eb";
      th.style.padding = "4px 6px";
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    (stages || []).forEach(function (stage) {
      var tr = document.createElement("tr");

      var nameTd = document.createElement("td");
      nameTd.textContent = stage.name || "—";
      nameTd.style.padding = "4px 6px";
      nameTd.style.borderBottom = "1px solid #f3f4f6";
      tr.appendChild(nameTd);

      var statusTd = document.createElement("td");
      statusTd.textContent = stage.status || "—";
      statusTd.style.padding = "4px 6px";
      statusTd.style.borderBottom = "1px solid #f3f4f6";
      statusTd.style.fontWeight = "600";
      statusTd.style.color = STATUS_COLORS[stage.status] || "#6b7280";
      tr.appendChild(statusTd);

      var detailTd = document.createElement("td");
      detailTd.textContent = stage.detail || "";
      detailTd.style.padding = "4px 6px";
      detailTd.style.borderBottom = "1px solid #f3f4f6";
      detailTd.style.wordBreak = "break-word";
      tr.appendChild(detailTd);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // Rebuild the card body from a fresh payload.
  function render(data) {
    if (!bodyEl) {
      return;
    }
    bodyEl.textContent = "";

    if (data.note) {
      var note = document.createElement("div");
      note.textContent = data.note;
      note.style.marginBottom = "10px";
      note.style.color = "#6b7280";
      bodyEl.appendChild(note);
    }
    if (data.degraded) {
      var degraded = document.createElement("div");
      degraded.textContent =
        "Temporal is unreachable; showing the last known stage data.";
      degraded.style.marginBottom = "10px";
      degraded.style.color = "#d97706";
      bodyEl.appendChild(degraded);
    }

    bodyEl.appendChild(labeledRow("Workflow id:", data.workflow_id));
    bodyEl.appendChild(labeledRow("Status:", data.status));
    bodyEl.appendChild(labeledRow("Run ID:", data.run_id));
    bodyEl.appendChild(labeledRow("Started:", data.started));
    bodyEl.appendChild(labeledRow("Closed:", data.closed));
    bodyEl.appendChild(labeledRow("Result:", data.result));
    bodyEl.appendChild(buildStagesTable(data.stages));
  }

  function renderError(message) {
    if (!bodyEl) {
      return;
    }
    bodyEl.textContent = "";
    var err = document.createElement("div");
    err.textContent = message || "Could not load workflow info.";
    err.style.color = "#dc2626";
    bodyEl.appendChild(err);
  }

  function fetchInfo() {
    var docId = currentDocId;
    if (!docId) {
      return;
    }
    fetch(infoUrl(docId), {
      method: "GET",
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(data.detail || "status " + response.status);
          }
          return data;
        });
      })
      .then(function (data) {
        // The modal may have been closed (or switched to another row) while
        // the request was in flight — ignore a stale response.
        if (!bodyEl || currentDocId !== docId) {
          return;
        }
        render(data);
        // Keep polling only while the workflow is RUNNING; otherwise stop.
        if (!POLLING_STATUSES[data.status]) {
          stopPolling();
        }
      })
      .catch(function (error) {
        if (!bodyEl || currentDocId !== docId) {
          return;
        }
        renderError("Could not load workflow info: " + error.message);
        stopPolling();
      });
  }

  function openModal(docId, trigger) {
    // Replace any modal already open (e.g. a second row's pill).
    if (overlayEl) {
      closeModal();
    }
    currentDocId = docId;
    triggerEl = trigger || null;
    buildShell();
    document.addEventListener("keydown", onKeydown);

    var loading = document.createElement("div");
    loading.textContent = "Loading…";
    loading.style.color = "#6b7280";
    bodyEl.appendChild(loading);

    fetchInfo();
    // Start the single poll timer; fetchInfo() stops it once non-RUNNING.
    stopPolling();
    pollTimer = window.setInterval(fetchInfo, POLL_MS);
  }

  // Delegate clicks so dynamically re-rendered rows keep working.
  document.addEventListener("click", function (event) {
    var pill = event.target.closest(".info-action");
    if (!pill) {
      return;
    }
    event.preventDefault();
    var docId = pill.getAttribute("data-doc-id");
    if (docId) {
      openModal(docId, pill);
    }
  });
})();
