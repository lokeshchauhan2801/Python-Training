(function () {
  "use strict";

  var root = document.getElementById("documents-temporal-info-root");
  if (!root) { return; }
  var urlTemplate = root.getAttribute("data-info-url-template");
  if (!urlTemplate) { return; }

  var POLL_MS = 2000;
  var STATUS_COLORS = {
    completed: "#16a34a", running: "#d97706", pending: "#6b7280", failed: "#dc2626",
    paused: "#7c3aed", scheduled: "#2563eb", timed_out: "#dc2626", canceled: "#6b7280"
  };
  var BACKEND_LABELS = {
    temporal: "Temporal", celery: "Celery", "local-background": "Celery (inline)",
    django: "Django (process now)", kafka: "Kafka"
  };
  var overlayEl = null;
  var pollTimer = null;
  var currentDocId = null;
  var triggerEl = null;
  var bodyEl = null;
  var headingEl = null;

  function backendLabel(backend) {
    return BACKEND_LABELS[backend] || backend || "processing";
  }
  function infoUrl(docId) {
    return urlTemplate.replace("__PK__", encodeURIComponent(docId));
  }
  function stopPolling() {
    if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null; }
  }
  function closeModal() {
    stopPolling();
    if (overlayEl && overlayEl.parentNode) { overlayEl.parentNode.removeChild(overlayEl); }
    overlayEl = null; bodyEl = null; headingEl = null; currentDocId = null;
    document.removeEventListener("keydown", onKeydown);
    if (triggerEl && typeof triggerEl.focus === "function") { triggerEl.focus(); }
    triggerEl = null;
  }
  function onKeydown(event) {
    if (event.key === "Escape" || event.key === "Esc") { closeModal(); }
  }
  function buildShell() {
    var overlay = document.createElement("div");
    Object.assign(overlay.style, {
      position: "fixed", top: "0", left: "0", right: "0", bottom: "0", background: "rgba(0,0,0,0.5)",
      zIndex: "9999", display: "flex", alignItems: "flex-start", justifyContent: "center", padding: "48px 16px", overflow: "auto"
    });
    overlay.addEventListener("click", function (event) { if (event.target === overlay) { closeModal(); } });
    var card = document.createElement("div");
    card.setAttribute("role", "dialog"); card.setAttribute("aria-modal", "true"); card.setAttribute("aria-label", "Processing info");
    Object.assign(card.style, { position: "relative", background: "#fff", color: "#111827", borderRadius: "8px", boxShadow: "0 10px 30px rgba(0,0,0,0.3)", maxWidth: "900px", width: "100%", padding: "16px 18px", boxSizing: "border-box" });
    var heading = document.createElement("div");
    heading.style.cssText = "font-size:16px;font-weight:700;margin-bottom:12px;padding-right:24px";
    card.appendChild(heading); headingEl = heading;
    var close = document.createElement("button");
    close.type = "button"; close.setAttribute("aria-label", "Close"); close.textContent = "×";
    close.style.cssText = "position:absolute;top:12px;right:14px;background:transparent;border:none;color:#111827;cursor:pointer;font-size:22px;line-height:1";
    close.addEventListener("click", closeModal); card.appendChild(close);
    var body = document.createElement("div"); body.style.fontSize = "13px"; card.appendChild(body);
    overlay.appendChild(card); document.body.appendChild(overlay);
    overlayEl = overlay; bodyEl = body; close.focus();
  }
  var detailsOpen = false;
  function show(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
  }
  function elapsed(value) {
    return value === null || value === undefined ? "—" : value < 1000 ? value + " ms" : (value / 1000).toFixed(1) + " s";
  }
  function summaryItem(label, value, color) {
    var item = document.createElement("div"); item.style.cssText = "flex:1;min-width:90px";
    var key = document.createElement("div"); key.textContent = label; key.style.cssText = "font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.03em"; item.appendChild(key);
    var val = document.createElement("div"); val.textContent = show(value); val.style.cssText = "font-weight:600;margin-top:2px;word-break:break-word" + (color ? ";color:" + color : ""); item.appendChild(val);
    return item;
  }
  function buildStagesTable(stages) {
    var table = document.createElement("table"); table.style.cssText = "width:100%;border-collapse:collapse;font-size:12px;margin-top:12px";
    (stages || []).forEach(function (stage) {
      var tr = document.createElement("tr");
      [stage.name || "—", stage.status || "—", elapsed(stage.elapsed_ms)].forEach(function (value, index) {
        var td = document.createElement("td"); td.textContent = value; td.style.cssText = "padding:4px 6px;border-bottom:1px solid #f3f4f6" + (index === 2 ? ";text-align:right;color:#6b7280" : "");
        if (index === 1) { td.style.fontWeight = "600"; td.style.color = STATUS_COLORS[stage.status] || "#6b7280"; }
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    return table;
  }
  function buildDetails(data, backend) {
    var db = data.db || {}; var temporal = data.temporal || {}; var celery = data.celery || {};
    var rows = [["Task ID", db.task_id], ["Last update", db.updated_at], ["Attempts", db.attempts]];
    if (backend === "temporal") {
      rows = rows.concat([["Workflow ID", data.workflow_id], ["Workflow status", temporal.status], ["Current stage", temporal.current_stage], ["Started", temporal.started], ["Closed", temporal.closed]]);
    } else if (backend === "celery") {
      rows = rows.concat([["Run ID", celery.run_id], ["Run status", celery.found ? celery.status : null], ["Workers", (celery.workers || []).join(", ") || null]]);
    }
    var details = document.createElement("details"); details.open = detailsOpen; details.style.cssText = "margin-top:10px;color:#4b5563";
    details.addEventListener("toggle", function () { detailsOpen = details.open; });
    var summary = document.createElement("summary"); summary.textContent = "More details"; summary.style.cssText = "cursor:pointer;font-size:12px"; details.appendChild(summary);
    rows.forEach(function (row) {
      var line = document.createElement("div"); line.style.cssText = "display:flex;gap:8px;padding:2px 0;font-size:12px";
      var key = document.createElement("span"); key.textContent = row[0]; key.style.cssText = "min-width:110px;color:#6b7280"; line.appendChild(key);
      var val = document.createElement("span"); val.textContent = show(row[1]); val.style.wordBreak = "break-all"; line.appendChild(val);
      details.appendChild(line);
    });
    return details;
  }
  function render(data) {
    if (!bodyEl) { return; }
    bodyEl.textContent = "";
    var db = data.db || {}; var backend = data.processing_backend || db.backend || "";
    if (headingEl) { headingEl.textContent = backendLabel(backend) + " processing info"; }
    (data.warnings || []).forEach(function (warning) {
      var callout = document.createElement("div"); callout.textContent = warning; callout.style.cssText = "margin:0 0 8px;padding:6px 8px;border-radius:4px;background:#fffbeb;color:#92400e;font-size:12px"; bodyEl.appendChild(callout);
    });
    var summaryRow = document.createElement("div"); summaryRow.style.cssText = "display:flex;gap:12px;flex-wrap:wrap;padding:10px;border:1px solid #e5e7eb;border-radius:6px";
    summaryRow.appendChild(summaryItem("Status", db.status, STATUS_COLORS[db.status === "ready" ? "completed" : db.status]));
    summaryRow.appendChild(summaryItem("Progress", db.progress === undefined || db.progress === null ? null : db.progress + "%"));
    summaryRow.appendChild(summaryItem("Chunks", db.chunk_count));
    summaryRow.appendChild(summaryItem("Backend", backendLabel(db.backend || backend)));
    bodyEl.appendChild(summaryRow);
    if (db.error) {
      var err = document.createElement("div"); err.textContent = db.error; err.style.cssText = "margin-top:8px;color:#dc2626;font-size:12px;word-break:break-word"; bodyEl.appendChild(err);
    }
    bodyEl.appendChild(buildStagesTable(data.stages));
    bodyEl.appendChild(buildDetails(data, backend));
  }
  function renderError(message) {
    if (!bodyEl) { return; }
    bodyEl.textContent = ""; var err = document.createElement("div"); err.textContent = message || "Could not load workflow info."; err.style.color = "#dc2626"; bodyEl.appendChild(err);
  }
  function fetchInfo() {
    var docId = currentDocId; if (!docId) { return; }
    fetch(infoUrl(docId), { method: "GET", credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (response) { return response.json().then(function (data) { if (!response.ok) { throw new Error(data.detail || "status " + response.status); } return data; }); })
      .then(function (data) { if (!bodyEl || currentDocId !== docId) { return; } render(data); if (!data.polling) { stopPolling(); } })
      .catch(function (error) { if (!bodyEl || currentDocId !== docId) { return; } renderError("Could not load workflow info: " + error.message); stopPolling(); });
  }
  function openModal(docId, trigger) {
    if (overlayEl) { closeModal(); }
    detailsOpen = false; currentDocId = docId; triggerEl = trigger || null; buildShell(); document.addEventListener("keydown", onKeydown);
    var loading = document.createElement("div"); loading.textContent = "Loading…"; loading.style.color = "#6b7280"; bodyEl.appendChild(loading);
    fetchInfo(); stopPolling(); pollTimer = window.setInterval(fetchInfo, POLL_MS);
  }
  document.addEventListener("click", function (event) {
    var pill = event.target.closest(".info-action"); if (!pill) { return; }
    event.preventDefault(); var docId = pill.getAttribute("data-doc-id"); if (docId) { openModal(docId, pill); }
  });
})();
