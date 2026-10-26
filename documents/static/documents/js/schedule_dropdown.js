
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
        // Check if processing was skipped (already-processed document)
        var isSkipped = data && data.message && data.message.indexOf("Already processed") === 0;
        
        if (isSkipped) {
          // Revert optimistic updates for already-processed documents
          if (currentBackend) {
            updateProcessingBackend(docId, currentBackend);
          }
          enableScheduleButton(docId);
        } else {
          // Normal processing: confirm optimistic updates
          if (data && data.status_display) {
            updateRowStatus(docId, data.status_display);
          }
          if (data && data.backend) {
            updateProcessingBackend(docId, data.backend);
          }
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
        dispatch(docId, backend, parentMenu);
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
