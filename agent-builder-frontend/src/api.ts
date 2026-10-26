// TypeScript API client for LangGraph agent builder backend

const API_ROOT = "/api/v1/agents";

export interface Position {
  x: number;
  y: number;
}

export interface Graph {
  id: number;
  name: string;
  company: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Node {
  id: number;
  graph: number;
  slug: string;
  kind: "start" | "step" | "end" | "tool" | "condition";
  label: string;
  position: Position;
  tools: number[];
  created_at: string;
  updated_at: string;
}

export interface Edge {
  id: number;
  graph: number;
  source: string;
  target: string;
  created_at: string;
  updated_at: string;
}

export interface Tool {
  id: number;
  slug: string;
  name: string;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface LoadGraphResponse {
  graph: Graph;
  nodes: Node[];
  edges: Edge[];
  tools: Tool[];
}

export interface ChatSession {
  id: number;
  title: string;
  company_id: number;
}

export interface SendResponse {
  assistant: {
    content: string;
    citations?: any[];
    chart_data?: any;
  };
}

function csrfToken(): string {
  const params = new URLSearchParams(window.location.search);
  return params.get("csrf") || "";
}

async function request<T>(method: string, path: string, body?: any): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const opts: RequestInit = { method, headers, credentials: "same-origin" };
  
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
    const err: any = new Error(`${method} ${path} failed (${res.status}): ${detail}`);
    err.status = res.status;
    throw err;
  }
  
  if (res.status === 204) return null as T;
  return res.json();
}

async function formRequest<T>(url: string, body: Record<string, string>): Promise<T> {
  const formData = new URLSearchParams();
  Object.entries(body).forEach(([key, value]) => formData.append(key, value));
  
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRFToken": csrfToken(),
    },
    credentials: "same-origin",
    body: formData,
  });
  
  if (!res.ok) {
    let detail = "";
    try {
      const json = await res.json();
      detail = json.detail || JSON.stringify(json);
    } catch (_e) {
      detail = res.statusText;
    }
    const err: any = new Error(`POST ${url} failed (${res.status}): ${detail}`);
    err.status = res.status;
    throw err;
  }
  
  return res.json();
}

export const api = {
  // Graph operations
  listGraphs: () => request<{ results: Graph[] }>("GET", "/graph/"),
  createGraph: (data: { name: string }) => request<Graph>("POST", "/graph/", data),
  loadGraph: (id: number) => request<LoadGraphResponse>("GET", `/graph/${id}/load/`),
  activateGraph: (id: number) => request<Graph>("POST", `/graph/${id}/activate/`, {}),

  // Node operations
  createNode: (data: Partial<Node> & { graph: number }) => request<Node>("POST", "/node/", data),
  updateNode: (id: number, data: Partial<Node>) => request<Node>("PATCH", `/node/${id}/`, data),
  deleteNode: (id: number) => request<void>("DELETE", `/node/${id}/`),

  // Edge operations
  createEdge: (data: { graph: number; source: string; target: string }) => 
    request<Edge>("POST", "/edge/", data),
  deleteEdge: (id: number) => request<void>("DELETE", `/edge/${id}/`),

  // Tool operations
  listTools: () => request<Tool[]>("GET", "/tool/"),
  createTool: (data: Partial<Tool>) => request<Tool>("POST", "/tool/", data),
  updateTool: (id: number, data: Partial<Tool>) => request<Tool>("PATCH", `/tool/${id}/`, data),

  // Chat operations (form-urlencoded, not JSON)
  createChatSession: (companyId: number) => 
    formRequest<ChatSession>("/chat/new/", { company: String(companyId) }),
  sendChatMessage: (sessionId: number, question: string) => 
    formRequest<SendResponse>(`/chat/${sessionId}/send/`, { question }),
};
