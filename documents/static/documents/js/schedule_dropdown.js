
(function () {
  "use strict";

  var root = document.getElementById("documents-schedule-root");
  if (!root) {
    return;
  }
  var urlTemplate = root.getAttribute("data-dispatch-url-template");
  if (!urlTemplate) {
    return;
  }

  function dispatchUrl(docId) {
    return urlTemplate.replace("__PK__", encodeURIComponent(docId));
  }

  var BACKEND_LABELS = {
    temporal: "Temporal",
    celery: "Celery",
    "local-background": "Celery (inline)",
    django: "Django (process now)",
    kafka: "Kafka",
  };

  function backendLabel(backend) {
    return BACKEND_LABELS[backend] || backend || "processing";
  }


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

  var MENU_Z_INDEX = "9999";


  function positionMenu(toggle, menu) {
    var rect = toggle.getBoundingClientRect();
    menu.style.position = "fixed";
    menu.style.top = rect.bottom + 4 + "px";
    menu.style.left = rect.left + "px";
    menu.style.zIndex = MENU_Z_INDEX;
    menu.style.margin = "0";
  }


  function hideMenu(menu) {
    menu.classList.add("hidden");
    menu.style.position = "";
    menu.style.top = "";
    menu.style.left = "";
    menu.style.zIndex = "";
    menu.style.margin = "";
  }

  function closeAllMenus(except) {
    var menus = document.querySelectorAll(".schedule-menu");
    for (var i = 0; i < menus.length; i++) {
      if (menus[i] !== except) {
        hideMenu(menus[i]);
        var cell = menus[i].closest(".schedule-cell");
        if (cell) {
          var toggle = cell.querySelector(".schedule-toggle");
          if (toggle) {
            toggle.setAttribute("aria-expanded", "false");
          }
        }
      }
    }
  }

  function updateRowStatus(docId, statusDisplay) {


    var toggle = document.querySelector(
      '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
    );
    if (!toggle) {
      return;
    }
    var row = toggle.closest("tr");
    if (!row) {
      return;
    }
    var cell = row.querySelector(".field-status_badge");
    if (!cell) {
      return;
    }
    var span = cell.querySelector("span") || document.createElement("span");
    span.textContent = statusDisplay;
    if (!span.parentNode) {
      cell.textContent = "";
      cell.appendChild(span);
    }
  }

  function updateProcessingBackend(docId, backend) {
    var toggle = document.querySelector(
      '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
    );
    if (!toggle) {
      return;
    }
    var row = toggle.closest("tr");
    if (!row) {
      return;
    }
    var cell = row.querySelector(".field-processing_backend");
    if (!cell) {
      return;
    }
    cell.textContent = backendLabel(backend);

    // The "See workflow" link is only meaningful for a Temporal-backed
    // document. When a reprocess switches the backend to anything else
    // (celery/kafka/django), drop the now-stale link so it doesn't linger
    // until a page reload.
    if (backend !== "temporal") {
      var staleLink = row.querySelector(".workflow-link");
      if (staleLink) {
        staleLink.remove();
      }
    }
  }

  function disableScheduleButton(docId) {
    var toggle = document.querySelector(
      '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
    );
    if (!toggle) {
      return;
    }
    toggle.disabled = true;
    toggle.style.cursor = "not-allowed";
    toggle.style.opacity = "0.5";
    toggle.setAttribute("aria-disabled", "true");
  }

  function enableScheduleButton(docId) {
    var toggle = document.querySelector(
      '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
    );
    if (!toggle) {
      return;
    }
    toggle.disabled = false;
    toggle.style.cursor = "pointer";
    toggle.style.opacity = "1";
    toggle.setAttribute("aria-disabled", "false");
  }

  // Expose for cross-script use (status_poll.js re-enables button on completion)
  window.enableDocumentScheduleButton = enableScheduleButton;
  window.disableDocumentScheduleButton = disableScheduleButton;

  function showNotice(message, isError) {
    var existing = document.getElementById("documents-dispatch-notice");
    if (existing) {
      existing.remove();
    }
    var notice = document.createElement("div");
    notice.id = "documents-dispatch-notice";
    notice.setAttribute("role", isError ? "alert" : "status");
    notice.setAttribute("aria-live", isError ? "assertive" : "polite");
    notice.textContent = message;
    notice.style.position = "fixed";
    notice.style.right = "16px";
    notice.style.top = "16px";
    notice.style.zIndex = MENU_Z_INDEX;
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


  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  // True when the row for docId is in the terminal "ready" (Processed) state,
  // i.e. it already has embedded chunks. Reprocessing it is allowed but wastes
  // work, so the caller asks for confirmation first.
  function isDocumentReady(docId) {
    var toggle = document.querySelector(
      '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
    );
    if (!toggle) {
      return false;
    }
    var row = toggle.closest("tr");
    if (!row) {
      return false;
    }
    var badge = row.querySelector(".field-status_badge span[data-status]");
    return !!badge && badge.getAttribute("data-status") === "ready";
  }

  // Small confirm popup centered on the main screen. Resolves true if the user
  // confirms the reprocess, false if they cancel or dismiss. Keyboard-friendly:
  // Escape cancels, the confirm button is autofocused.
  function confirmReprocess(docId) {
    return new Promise(function (resolve) {
      var titleText = "Reprocess this document?";
      var toggle = document.querySelector(
        '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
      );
      if (toggle) {
        var row = toggle.closest("tr");
        var titleCell = row && row.querySelector(".field-title");
        if (titleCell && titleCell.textContent.trim()) {
          titleText =
            'Reprocess "' + titleCell.textContent.trim() + '"?';
        }
      }

      var overlay = document.createElement("div");
      overlay.id = "documents-reprocess-confirm";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.setAttribute("aria-labelledby", "documents-reprocess-title");
      overlay.style.position = "fixed";
      overlay.style.inset = "0";
      overlay.style.zIndex = "10000";
      overlay.style.display = "flex";
      overlay.style.alignItems = "center";
      overlay.style.justifyContent = "center";
      overlay.style.background = "rgba(0,0,0,0.45)";

      var dialog = document.createElement("div");
      dialog.style.maxWidth = "360px";
      dialog.style.width = "calc(100% - 32px)";
      dialog.style.background = "#ffffff";
      dialog.style.color = "#111827";
      dialog.style.borderRadius = "8px";
      dialog.style.boxShadow = "0 10px 30px rgba(0,0,0,0.25)";
      dialog.style.padding = "20px";
      // Respect dark admin themes.
      if (document.documentElement.classList.contains("dark")) {
        dialog.style.background = "#1f2937";
        dialog.style.color = "#f9fafb";
      }

      var title = document.createElement("div");
      title.id = "documents-reprocess-title";
      title.textContent = titleText;
      title.style.fontWeight = "600";
      title.style.fontSize = "15px";
      title.style.marginBottom = "8px";

      var body = document.createElement("div");
      body.textContent =
        "This document is already processed. Reprocessing clears its " +
        "existing chunks and runs the whole pipeline again.";
      body.style.fontSize = "13px";
      body.style.lineHeight = "1.4";
      body.style.marginBottom = "16px";
      body.style.opacity = "0.85";

      var actions = document.createElement("div");
      actions.style.display = "flex";
      actions.style.justifyContent = "flex-end";
      actions.style.gap = "8px";

      var cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.textContent = "Cancel";
      cancelBtn.style.padding = "6px 14px";
      cancelBtn.style.fontSize = "13px";
      cancelBtn.style.borderRadius = "6px";
      cancelBtn.style.border = "1px solid rgba(148,163,184,0.5)";
      cancelBtn.style.background = "transparent";
      cancelBtn.style.color = "inherit";
      cancelBtn.style.cursor = "pointer";

      var confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.textContent = "Reprocess";
      confirmBtn.style.padding = "6px 14px";
      confirmBtn.style.fontSize = "13px";
      confirmBtn.style.borderRadius = "6px";
      confirmBtn.style.border = "1px solid transparent";
      confirmBtn.style.background = "#7c3aed";
      confirmBtn.style.color = "#ffffff";
      confirmBtn.style.cursor = "pointer";

      function cleanup(result) {
        document.removeEventListener("keydown", onKeydown, true);
        overlay.remove();
        resolve(result);
      }
      function onKeydown(event) {
        if (event.key === "Escape") {
          event.preventDefault();
          cleanup(false);
        }
      }

      cancelBtn.addEventListener("click", function () {
        cleanup(false);
      });
      confirmBtn.addEventListener("click", function () {
        cleanup(true);
      });
      overlay.addEventListener("click", function (event) {
        if (event.target === overlay) {
          cleanup(false);
        }
      });
      document.addEventListener("keydown", onKeydown, true);

      actions.appendChild(cancelBtn);
      actions.appendChild(confirmBtn);
      dialog.appendChild(title);
      dialog.appendChild(body);
      dialog.appendChild(actions);
      overlay.appendChild(dialog);
      document.body.appendChild(overlay);
      confirmBtn.focus();
    });
  }

  function dispatch(docId, backend, menu) {
    // Store current backend for potential revert on error
    var toggle = document.querySelector(
      '.schedule-toggle[data-doc-id="' + cssEscape(docId) + '"]'
    );
    var currentBackend = "";
    if (toggle) {
      var row = toggle.closest("tr");
      if (row) {
        var backendCell = row.querySelector(".field-processing_backend");
        if (backendCell) {
          currentBackend = backendCell.textContent;
        }
      }
    }

    // Optimistic updates: immediately update UI before API call
    updateProcessingBackend(docId, backend === "now" ? "django" : backend);
    disableScheduleButton(docId);

    var body = new URLSearchParams();
    body.set("backend", backend);
    fetch(dispatchUrl(docId), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-CSRFToken": getCookie("csrftoken"),
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: body.toString(),
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(data.detail || "The request was rejected.");
          }
          return data;
        });
      })
      .then(function (data) {
        // The backend no longer skips already-processed documents (the user
        // confirms the reprocess up front), so always confirm the optimistic
        // UI updates with the server's reported status/backend.
        if (data && data.status_display) {
          updateRowStatus(docId, data.status_display);
        }
        if (data && data.backend) {
          updateProcessingBackend(docId, data.backend);
        }

        showNotice(
          (data && data.message) || "Task queued for processing.",
          false
        );
      })
      .catch(function (error) {
        // Revert optimistic updates on error
        if (currentBackend) {
          updateProcessingBackend(docId, currentBackend);
        }
        enableScheduleButton(docId);
        showNotice(
          "Could not queue document: " + error.message,
          true
        );
      });
    if (menu) {
      hideMenu(menu);
    }
    closeAllMenus();
  }


  document.addEventListener("click", function (event) {
    var toggle = event.target.closest(".schedule-toggle");
    if (toggle) {
      event.preventDefault();
      var cell = toggle.closest(".schedule-cell");
      var menu = cell ? cell.querySelector(".schedule-menu") : null;
      if (!menu) {
        return;
      }
      var isHidden = menu.classList.contains("hidden");
      closeAllMenus(isHidden ? menu : null);
      if (isHidden) {
        menu.classList.remove("hidden");
        positionMenu(toggle, menu);
        toggle.setAttribute("aria-expanded", "true");
      } else {
        hideMenu(menu);
        toggle.setAttribute("aria-expanded", "false");
      }
      return;
    }

    var item = event.target.closest(".schedule-menu-item");
    if (item) {
      event.preventDefault();
      var docId = item.getAttribute("data-doc-id");
      var backend = item.getAttribute("data-backend");
      var parentMenu = item.closest(".schedule-menu");
      if (docId && backend) {
        // Close the menu right away; the confirm popup (if any) takes over.
        if (parentMenu) {
          hideMenu(parentMenu);
        }
        closeAllMenus();
        if (isDocumentReady(docId)) {
          confirmReprocess(docId).then(function (confirmed) {
            if (confirmed) {
              dispatch(docId, backend, null);
            }
          });
        } else {
          dispatch(docId, backend, parentMenu);
        }
      }
      return;
    }


    closeAllMenus();
  });


  window.addEventListener("scroll", function () {
    closeAllMenus();
  }, true);
  window.addEventListener("resize", function () {
    closeAllMenus();
  });

  // On page load, disable Action buttons for documents in non-terminal states
  (function initializeButtonStates() {
    var rows = document.querySelectorAll("table tbody tr");
    for (var i = 0; i < rows.length; i++) {
      var statusBadge = rows[i].querySelector(".field-status_badge span[data-status]");
      if (!statusBadge) {
        continue;
      }
      var status = statusBadge.getAttribute("data-status");
      // Disable button if status is "queued" or "processing"
      if (status === "queued" || status === "processing") {
        var toggle = rows[i].querySelector(".schedule-toggle");
        if (toggle) {
          var docId = toggle.getAttribute("data-doc-id");
          if (docId) {
            disableScheduleButton(docId);
          }
        }
      }
    }
  })();
})();
