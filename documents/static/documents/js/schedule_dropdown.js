/*
 * In-row "Schedule" dropdown for the Documents changelist.
 *
 * Each row's Schedule column renders an "Action" button and a hidden dropdown
 * menu (Process now / Celery / Kafka / Temporal — rendered server-side by the
 * schedule_column display method). This script:
 *   - toggles a single dropdown open/closed below its button (only one open at
 *     a time; clicking elsewhere closes it);
 *   - on an item click, POSTs {backend} to the scoped AJAX dispatch endpoint
 *     with the CSRF token (no page navigation, no full reload);
 *   - updates that row's status cell from the JSON response.
 *
 * All DOM updates use textContent/createElement (never innerHTML) so server
 * data can never be interpreted as markup. The dispatch URL is a reverse()d
 * template with a placeholder pk the JS swaps per row.
 */
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

  // Read the CSRF cookie (Django's default "csrftoken").
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

  function closeAllMenus(except) {
    var menus = document.querySelectorAll(".schedule-menu");
    for (var i = 0; i < menus.length; i++) {
      if (menus[i] !== except) {
        menus[i].classList.add("hidden");
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
    // Find the row holding this document's Action button, then its status cell
    // (Django renders the status_badge column as td.field-status_badge).
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

  // Minimal CSS.escape fallback for the ids we embed (ULIDs/UUIDs are safe,
  // but guard anyway for attribute-selector correctness).
  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  function dispatch(docId, backend, menu) {
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
          return { ok: response.ok, data: data };
        });
      })
      .then(function (result) {
        if (result.ok && result.data && result.data.status_display) {
          updateRowStatus(docId, result.data.status_display);
        }
        // The completion toast is handled by status_poll.js once the backend
        // reaches a terminal state; this just reflects the queued transition.
      })
      .catch(function () {
        /* transient error — the user can retry via the dropdown */
      });
    if (menu) {
      menu.classList.add("hidden");
    }
    closeAllMenus();
  }

  // Delegate all clicks so dynamically re-rendered rows keep working.
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
        toggle.setAttribute("aria-expanded", "true");
      } else {
        menu.classList.add("hidden");
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

    // Click anywhere else closes any open menu.
    closeAllMenus();
  });
})();
