// Thin client for the DRF api/v1/agents/ endpoints.
//
// The canvas runs inside a same-origin <iframe> on the Django /agent-builder/
// page, so requests carry the session cookie automatically. Write methods need
// the CSRF token, which the Django page hands to the iframe via its src
// querystring (?csrf=<token>); we read it back from location.search.

const API_ROOT = "/api/v1/agents";

function csrfToken() {
  const params = new URLSearchParams(window.location.search);
  return params.get("csrf") || "";
}

async function request(method, path, body) {
  const headers = { Accept: "application/json" };
  const opts = { method, headers, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRFToken"] = csrfToken();
  }
  const res = await fetch(`${API_ROOT}${path}`, opts);
  if (!res.ok) {
    let detail = "";
    try {
      detail = JSON.stringify(await res.json());
    } catch (_e) {
      detail = res.statusText;
    }
    const err = new Error(`${method} ${path} failed (${res.status}): ${detail}`);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  listGraphs: () => request("GET", "/graph/"),
  createGraph: (data) => request("POST", "/graph/", data),
  loadGraph: (id) => request("GET", `/graph/${id}/load/`),
  activateGraph: (id) => request("POST", `/graph/${id}/activate/`, {}),

  createNode: (data) => request("POST", "/node/", data),
  updateNode: (id, data) => request("PATCH", `/node/${id}/`, data),
  deleteNode: (id) => request("DELETE", `/node/${id}/`),

  createEdge: (data) => request("POST", "/edge/", data),
  deleteEdge: (id) => request("DELETE", `/edge/${id}/`),

  listTools: () => request("GET", "/tool/"),
  createTool: (data) => request("POST", "/tool/", data),
  updateTool: (id, data) => request("PATCH", `/tool/${id}/`, data),
};
