(function () {
  "use strict";

  var root = document.getElementById("documents-temporal-info-root");
  if (!root) { return; }
  var urlTemplate = root.getAttribute("data-info-url-template");
  if (!urlTemplate) { return; }

  var POLL_MS = 2000;
  var STATUS_COLORS = {
    completed: "#16a34a", running: "#d97706", pending: "#6b7280", failed: "#dc2626"
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
    Object.assign(card.style, { position: "relative", background: "#fff", color: "#111827", borderRadius: "8px", boxShadow: "0 10px 30px rgba(0,0,0,0.3)", maxWidth: "900px", width: "100%", padding: "20px 22px", boxSizing: "border-box" });
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
  function labeledRow(label, value) {
    var row = document.createElement("div"); row.style.cssText = "display:flex;gap:8px;padding:3px 0";
    var key = document.createElement("span"); key.textContent = label; key.style.cssText = "font-weight:600;min-width:112px"; row.appendChild(key);
    var val = document.createElement("span"); val.textContent = value === null || value === undefined || value === "" ? "—" : String(value); val.style.wordBreak = "break-all"; row.appendChild(val);
    return row;
  }
  function section(title, rows) {
    var wrap = document.createElement("section"); wrap.style.cssText = "flex:1;min-width:250px;border:1px solid #e5e7eb;border-radius:6px;padding:10px";
    var caption = document.createElement("div"); caption.textContent = title; caption.style.cssText = "font-weight:700;margin-bottom:5px"; wrap.appendChild(caption);
    rows.forEach(function (row) { wrap.appendChild(labeledRow(row[0], row[1])); });
    return wrap;
  }
  function elapsed(value) {
    return value === null || value === undefined ? "—" : value < 1000 ? value + " ms" : (value / 1000).toFixed(1) + " s";
  }
  function buildStagesTable(stages) {
    var wrap = document.createElement("section"); wrap.style.marginTop = "14px";
    var caption = document.createElement("div"); caption.textContent = "Database stage timings"; caption.style.cssText = "font-weight:700;margin-bottom:6px"; wrap.appendChild(caption);
    var table = document.createElement("table"); table.style.cssText = "width:100%;border-collapse:collapse;font-size:12px";
    var thead = document.createElement("thead"); var headRow = document.createElement("tr");
    ["Stage", "Status", "Detail", "Started", "Finished", "Elapsed"].forEach(function (text) {
      var th = document.createElement("th"); th.textContent = text; th.style.cssText = "text-align:left;border-bottom:1px solid #e5e7eb;padding:4px 6px"; headRow.appendChild(th);
    });
    thead.appendChild(headRow); table.appendChild(thead);
    var tbody = document.createElement("tbody");
    (stages || []).forEach(function (stage) {
      var tr = document.createElement("tr");
      [stage.name || "—", stage.status || "—", stage.detail || "", stage.started_at || "—", stage.finished_at || "—", elapsed(stage.elapsed_ms)].forEach(function (value, index) {
        var td = document.createElement("td"); td.textContent = value; td.style.cssText = "padding:4px 6px;border-bottom:1px solid #f3f4f6;word-break:break-word";
        if (index === 1) { td.style.fontWeight = "600"; td.style.color = STATUS_COLORS[stage.status] || "#6b7280"; }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); wrap.appendChild(table); return wrap;
  }
  function render(data) {
    if (!bodyEl) { return; }
    bodyEl.textContent = "";
    var db = data.db || {}; var temporal = data.temporal || {}; var backend = data.processing_backend || db.backend || "";
    if (headingEl) { headingEl.textContent = backendLabel(backend) + " processing info"; }
    (data.warnings || []).forEach(function (warning) {
      var callout = document.createElement("div"); callout.textContent = warning; callout.style.cssText = "margin:0 0 8px;padding:8px;border-radius:4px;background:#fffbeb;color:#92400e"; bodyEl.appendChild(callout);
    });
    var columns = document.createElement("div"); columns.style.cssText = "display:flex;gap:12px;flex-wrap:wrap";
    columns.appendChild(section("Document database", [
      ["Status:", db.status], ["Current stage:", db.stage], ["Progress:", db.progress === undefined ? null : db.progress + "%"],
      ["Chunks:", db.chunk_count], ["Backend:", backendLabel(db.backend)], ["Attempts:", db.attempts],
      ["Task ID:", db.task_id], ["Last update:", db.updated_at], ["Update age:", db.updated_age_seconds === undefined ? null : db.updated_age_seconds + " s"], ["Error:", db.error]
    ]));
    if (backend === "temporal") {
      columns.appendChild(section("Temporal workflow", [
        ["Status:", temporal.status], ["Workflow ID:", data.workflow_id], ["Run ID:", temporal.run_id],
        ["Started:", temporal.started], ["Closed:", temporal.closed], ["Result:", temporal.result], ["Degraded:", temporal.degraded ? "Yes" : "No"]
      ]));
    }
    bodyEl.appendChild(columns);
    if (backend === "temporal" && data.temporal_url) {
      var link = document.createElement("a"); link.href = data.temporal_url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Open workflow"; link.style.cssText = "display:inline-block;margin-top:10px;color:#2563eb;text-decoration:underline"; bodyEl.appendChild(link);
    }
    bodyEl.appendChild(buildStagesTable(data.stages));
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
    currentDocId = docId; triggerEl = trigger || null; buildShell(); document.addEventListener("keydown", onKeydown);
    var loading = document.createElement("div"); loading.textContent = "Loading…"; loading.style.color = "#6b7280"; bodyEl.appendChild(loading);
    fetchInfo(); stopPolling(); pollTimer = window.setInterval(fetchInfo, POLL_MS);
  }
  document.addEventListener("click", function (event) {
    var pill = event.target.closest(".info-action"); if (!pill) { return; }
    event.preventDefault(); var docId = pill.getAttribute("data-doc-id"); if (docId) { openModal(docId, pill); }
  });
})();
