// TypeScript API client for the agents backend (two-level Agent -> Steps model).

const API_ROOT = "/api/v1/agents";

export type ToolType =
  | "general_assistant"
  | "document_search"
  | "chat_summarise"
  | "web_search"
  | "add_to_knowledge_base"
  | "document_processing_status"
  | "langfuse";

export interface Tool {
  id: string; // ULID string
  slug: string;
  name: string;
  type: ToolType;
  description: string;
  is_enabled: boolean;
  company: string | number | null;
  created_at: string;
  updated_at: string;
  created_by: string | number | null;
  updated_by: string | number | null;
  is_deleted: boolean;
  deleted_at: string | null;
}

/** A flow CONTAINER. Its ordered Steps are the canvas nodes. */
export interface Agent {
  id: string; // ULID string
  slug: string;
  name: string;
  order: number;
  is_active: boolean; // READ-ONLY: set only via the activate action
  company: string | number | null;
  created_at: string;
  updated_at: string;
  created_by: string | number | null;
  updated_by: string | number | null;
  is_deleted: boolean;
  deleted_at: string | null;
}

/** One node under an Agent. Holds the tools; chained via next_step. */
export interface Step {
  id: string; // ULID string
  agent: string; // parent Agent ULID string
  slug: string;
  name: string;
  tools: string[]; // array of Tool ULID strings (M2M)
  connected_from_start: boolean;
  connected_to_end: boolean;
  next_step: string | null; // nullable Step ULID string (same agent only)
  position_x: number;
  position_y: number;
  order: number;
  company: string | number | null;
  created_at: string;
  updated_at: string;
  created_by: string | number | null;
  updated_by: string | number | null;
  is_deleted: boolean;
  deleted_at: string | null;
}

export interface LoadFlowResponse {
  active_agent_id: string | null;
  agents: Agent[];
  selected_agent_id: string | null;
  steps: Step[];
  tools: Tool[];
}

/** A registered handler function the canvas can attach as a tool. */
export interface AvailableFunction {
  slug: string;
  name: string;
  type: ToolType;
  description: string;
  tool_id: string | null; // set when a backing Tool row already exists
}

function queryParam(name: string): string {
  const params = new URLSearchParams(window.location.search);
  return params.get(name) || "";
}

/** CSRF token passed by the Django iframe via ?csrf=<token>. */
function csrfToken(): string {
  return queryParam("csrf");
}

/** Active company id passed by the Django iframe via ?company=<id>. */
export function companyId(): string {
  return queryParam("company");
}

/** Active company name passed by the Django iframe via ?company_name=<name>. */
export function companyName(): string {
  return queryParam("company_name");
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

/** DRF list endpoints may return a plain array or a paginated {results:[]}. */
function asList<T>(payload: T[] | { results: T[] }): T[] {
  return Array.isArray(payload) ? payload : payload?.results || [];
}

export const api = {
  // Tool operations
  listTools: async (): Promise<Tool[]> =>
    asList(await request<Tool[] | { results: Tool[] }>("GET", "/tool/")),
  createTool: (data: Partial<Tool>) => request<Tool>("POST", "/tool/", data),
  updateTool: (id: string, data: Partial<Tool>) =>
    request<Tool>("PATCH", `/tool/${id}/`, data),
  deleteTool: (id: string) => request<void>("DELETE", `/tool/${id}/`),

  // Tools-from-functions: list the registered handler functions and
  // create/attach a backing Tool row to a STEP from one.
  availableFunctions: (company?: string) => {
    const qs = company ? `?company=${encodeURIComponent(company)}` : "";
    return request<AvailableFunction[]>("GET", `/tool/available/${qs}`);
  },
  attachFunction: (data: { slug: string; step?: string; company?: string }) =>
    request<Tool>("POST", "/tool/attach/", data),

  // Agent (container) operations
  listAgents: async (): Promise<Agent[]> =>
    asList(await request<Agent[] | { results: Agent[] }>("GET", "/agent/")),
  createAgent: (data: Partial<Agent>) => request<Agent>("POST", "/agent/", data),
  updateAgent: (id: string, data: Partial<Agent>) =>
    request<Agent>("PATCH", `/agent/${id}/`, data),
  deleteAgent: (id: string) => request<void>("DELETE", `/agent/${id}/`),
  activateAgent: (id: string) => request<Agent>("POST", `/agent/${id}/activate/`, {}),

  // Step (node) operations
  listSteps: async (): Promise<Step[]> =>
    asList(await request<Step[] | { results: Step[] }>("GET", "/step/")),
  createStep: (data: Partial<Step>) => request<Step>("POST", "/step/", data),
  updateStep: (id: string, data: Partial<Step>) =>
    request<Step>("PATCH", `/step/${id}/`, data),
  deleteStep: (id: string) => request<void>("DELETE", `/step/${id}/`),

  // Flow bootstrap: all the company's agents + a selected agent's steps + tools.
  loadFlow: (company?: string, agent?: string) => {
    const params = new URLSearchParams();
    if (company) params.set("company", company);
    if (agent) params.set("agent", agent);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return request<LoadFlowResponse>("GET", `/agent/load/${qs}`);
  },
};
