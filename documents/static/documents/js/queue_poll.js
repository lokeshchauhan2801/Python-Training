
(function () {
  "use strict";

  var root = document.getElementById("documents-queue");
  if (!root) {
    return;
  }
  var queueUrl = root.getAttribute("data-queue-url");
  if (!queueUrl) {
    return;
  }

  function renderDocument(doc) {
    var item = root.querySelector(
      '.document-queue-item[data-document-id="' +
        String(doc.id).replace(/["\\]/g, "\\$&") +
        '"]'
    );
    if (!item) {
      window.location.reload();
      return;
    }
    var status = item.querySelector("[data-queue-status]");
    var stage = item.querySelector("[data-queue-stage]");
    var backend = item.querySelector("[data-queue-backend]");
    var task = item.querySelector("[data-queue-task]");
    var progressText = item.querySelector("[data-queue-progress-text]");
    var progressBar = item.querySelector("[data-queue-progress-bar]");
    var progressTrack = item.querySelector('[role="progressbar"]');
    var error = item.querySelector("[data-queue-error]");
    var progress = Math.max(
      0,
      Math.min(100, Number(doc.processing_progress) || 0)
    );
    if (doc.status === "ready") { progress = 100; }

    if (status) { status.textContent = doc.status_display || doc.status; }
    if (stage) {
      stage.textContent = (doc.processing_stage || doc.status || "")
        .replace(/_/g, " ")
        .replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
    }
    if (backend) { backend.textContent = doc.processing_backend || ""; }
    if (task) { task.textContent = doc.processing_task_id || "—"; }
    if (progressText) {
      progressText.textContent =
        progress + "% processed · " + (100 - progress) + "% left";
    }
    if (progressBar) { progressBar.style.width = progress + "%"; }
    if (progressTrack) {
      progressTrack.setAttribute("aria-valuenow", String(progress));
    }
    if (error) {
      error.textContent = doc.processing_error || "";
      error.classList.toggle("hidden", !doc.processing_error);
    }
  }

  function poll() {
    fetch(queueUrl, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Queue refresh failed (" + response.status + ").");
        }
        return response.json();
      })
      .then(function (data) {
        var documents = (data && data.documents) || [];
        if (!documents.length && root.querySelector(".document-queue-item")) {
          window.location.reload();
          return;
        }
        documents.forEach(renderDocument);
      })
      .catch(function (error) {
        console.error(error);
      });
  }

  window.setInterval(poll, 3000);
})();
