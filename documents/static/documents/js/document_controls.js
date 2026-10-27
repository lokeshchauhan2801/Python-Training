
(function () {
  "use strict";

  var root = document.getElementById("documents-controls-root");
  if (!root) {
    return;
  }
  var retryTemplate = root.getAttribute("data-retry-url-template");
  var cancelTemplate = root.getAttribute("data-cancel-url-template");

  function retryUrl(docId) {
    return retryTemplate
      ? retryTemplate.replace("__PK__", encodeURIComponent(docId))
      : "";
  }

  function cancelUrl(docId) {
    return cancelTemplate
      ? cancelTemplate.replace("__PK__", encodeURIComponent(docId))
      : "";
  }

  // --- helpers copied from schedule_dropdown.js so this script is standalone ---

  function getCookie(name) {
    var cookies = (document.cookie || "").split(";");
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf(name + "=") === 0) {
        return decodeURIComponent(c.substring(name.length + 1));
      }
    }
    return "";
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  var NOTICE_Z_INDEX = "9999";

  function showNotice(message, isError) {
    var existing = document.getElementById("documents-controls-notice");
    if (existing) {
      existing.remove();
    }
    var notice = document.createElement("div");
    notice.id = "documents-controls-notice";
    notice.setAttribute("role", isError ? "alert" : "status");
    notice.setAttribute("aria-live", isError ? "assertive" : "polite");
    notice.textContent = message;
    notice.style.position = "fixed";
    notice.style.right = "16px";
    notice.style.top = "16px";
    notice.style.zIndex = NOTICE_Z_INDEX;
    notice.style.maxWidth = "420px";
    notice.style.padding = "12px 16px";
    notice.style.borderRadius = "6px";
    notice.style.color = "#fff";
    notice.style.background = isError ? "#dc2626" : "#16a34a";
    notice.style.boxShadow = "0 2px 8px rgba(0,0,0,0.2)";
    document.body.appendChild(notice);
    window.setTimeout(function () {
      notice.remove();
    }, 6000);
  }

  // --- row lookup + status-driven visibility ---

  function rowForDoc(docId) {
    var btn = document.querySelector(
      '.retry-action[data-doc-id="' + cssEscape(docId) + '"]'
    ) ||
      document.querySelector(
        '.stop-action[data-doc-id="' + cssEscape(docId) + '"]'
      );
    return btn ? btn.closest("tr") : null;
  }

  function rowStatus(row) {
    var span = row && row.querySelector(
      ".field-status_badge span[data-status]"
    );
    return span ? span.getAttribute("data-status") : null;
  }

  function applyVisibility(row) {
    if (!row) {
      return;
    }
    var status = rowStatus(row);
    var retry = row.querySelector(".retry-action");
    var stop = row.querySelector(".stop-action");
    if (retry) {
      retry.style.display = status === "failed" ? "" : "none";
    }
    if (stop) {
      stop.style.display =
        status === "queued" || status === "processing" ? "" : "none";
    }
  }

  function refreshControlVisibility(docId) {
    if (docId) {
      applyVisibility(rowForDoc(docId));
      return;
    }
    var rows = document.querySelectorAll("table tbody tr");
    for (var i = 0; i < rows.length; i++) {
      applyVisibility(rows[i]);
    }
  }

  // Keep the status cell + its data-status in sync after an optimistic update,
  // reusing the same cell structure status_poll.js / schedule_dropdown.js use.
  function setRowStatus(docId, statusDisplay, status) {
    var row = rowForDoc(docId);
    if (!row) {
      return;
    }
    var cell = row.querySelector(".field-status_badge");
    if (!cell) {
      return;
    }
    var span = cell.querySelector("span");
    if (!span) {
      span = document.createElement("span");
      cell.textContent = "";
      cell.appendChild(span);
    }
    if (statusDisplay) {
      span.textContent = statusDisplay;
    }
    if (status) {
      span.setAttribute("data-status", status);
    }
    applyVisibility(row);
  }

  // --- AJAX actions (shared with the Info modal via window.* exposure) ---

  function postControl(url, docId) {
    var body = new URLSearchParams();
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-CSRFToken": getCookie("csrftoken"),
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: body.toString(),
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error(data.detail || "The request was rejected.");
        }
        return data;
      });
    });
  }

  function retryDocument(docId) {
    var url = retryUrl(docId);
    if (!docId || !url) {
      return Promise.reject(new Error("Missing retry URL."));
    }
    return postControl(url, docId)
      .then(function (data) {
        if (data && data.status_display) {
          setRowStatus(docId, data.status_display, data.status);
        }
        showNotice((data && data.message) || "Resuming processing.", false);
        return data;
      })
      .catch(function (error) {
        showNotice("Could not resume document: " + error.message, true);
        throw error;
      });
  }

  function stopDocument(docId) {
    var url = cancelUrl(docId);
    if (!docId || !url) {
      return Promise.reject(new Error("Missing cancel URL."));
    }
    // No confirm() popup — the action runs immediately on click.
    return postControl(url, docId)
      .then(function (data) {
        if (data && data.status_display) {
          setRowStatus(docId, data.status_display, data.status);
        }
        showNotice((data && data.message) || "Processing stopped.", false);
        return data;
      })
      .catch(function (error) {
        if (error && error.message === "cancelled") {
          throw error;
        }
        showNotice("Could not stop document: " + error.message, true);
        throw error;
      });
  }

  // Exposed for status_poll.js (and any cross-script callers). The Info modal
  // is information-only and no longer calls these; the row Retry/Stop buttons do.
  window.retryDocument = retryDocument;
  window.stopDocument = stopDocument;
  window.refreshDocumentControls = refreshControlVisibility;

  document.addEventListener("click", function (event) {
    var retry = event.target.closest(".retry-action");
    if (retry) {
      event.preventDefault();
      var retryId = retry.getAttribute("data-doc-id");
      if (retryId) {
        retryDocument(retryId).catch(function () {});
      }
      return;
    }
    var stop = event.target.closest(".stop-action");
    if (stop) {
      event.preventDefault();
      var stopId = stop.getAttribute("data-doc-id");
      if (stopId) {
        stopDocument(stopId).catch(function () {});
      }
      return;
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      refreshControlVisibility();
    });
  } else {
    refreshControlVisibility();
  }
})();
