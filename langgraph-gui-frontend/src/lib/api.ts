// src/lib/api.ts — NEWLY AUTHORED (not vendored).
//
// Two distinct request shapes live side by side (design 4.3 / 5.1):
//   (a) the DRF agents client talks JSON to /api/v1/agents/*;
//   (b) the chat run helpers talk application/x-www-form-urlencoded to
//       /chat/new/ and /chat/<id>/send/ (those views read request.POST).
//
// CSRF: the token is read once from the iframe querystring (?csrf=...), exactly
// like the React canvas (agent-builder-frontend/src/api.js). All writes send
// X-CSRFToken and credentials:"same-origin".

export const API_ROOT = '/api/v1/agents';

/** An Error that also carries the HTTP status and the parsed response body. */
export class ApiError extends Error {
	status: number;
	body: unknown;
	constructor(message: string, status: number, body: unknown) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.body = body;
	}
}

/** Read the CSRF token from the iframe querystring (?csrf=...). */
export function csrfToken(): string {
	if (typeof window === 'undefined' || !window.location) return '';
	const params = new URLSearchParams(window.location.search);
	return params.get('csrf') || '';
}

// --- fetch injection (so tests can supply a fake without a DOM) -------------

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

let _fetch: FetchLike = (input, init) => fetch(input, init);

/** Override the fetch implementation (used by unit tests). */
export function __setFetch(fn: FetchLike | null): void {
	_fetch = fn ?? ((input, init) => fetch(input, init));
}

// --- (a) DRF agents client (JSON) -------------------------------------------

async function request<T = unknown>(
	method: string,
	path: string,
	body?: unknown
): Promise<T> {
	const headers: Record<string, string> = { Accept: 'application/json' };
	const opts: RequestInit = { method, headers, credentials: 'same-origin' };
	if (body !== undefined) {
		headers['Content-Type'] = 'application/json';
		opts.body = JSON.stringify(body);
	}
	if (method !== 'GET' && method !== 'HEAD') {
		headers['X-CSRFToken'] = csrfToken();
	}
	const res = await _fetch(`${API_ROOT}${path}`, opts);
	if (!res.ok) {
		let parsed: unknown = null;
		let detail = '';
		try {
			parsed = await res.json();
			detail = JSON.stringify(parsed);
		} catch {
			detail = res.statusText;
		}
		throw new ApiError(
			`${method} ${path} failed (${res.status}): ${detail}`,
			res.status,
			parsed
		);
	}
	if (res.status === 204) return null as T;
	return (await res.json()) as T;
}

export interface AgentsApi {
	listGraphs(): Promise<unknown>;
	createGraph(data: { name: string; company: string; description?: string }): Promise<unknown>;
	loadGraph(id: string): Promise<unknown>;
	activateGraph(id: string): Promise<unknown>;

	createNode(data: unknown): Promise<unknown>;
	updateNode(id: string, data: unknown): Promise<unknown>;
	deleteNode(id: string): Promise<unknown>;

	createEdge(data: unknown): Promise<unknown>;
	deleteEdge(id: string): Promise<unknown>;

	listTools(): Promise<unknown>;
	createTool(data: unknown): Promise<unknown>;
	updateTool(id: string, data: unknown): Promise<unknown>;
}

export const api: AgentsApi = {
	listGraphs: () => request('GET', '/graph/'),
	// createGraph MUST pass company explicitly or the serializer 400s
	// {"company": "ambiguous; specify a company"} for a multi-company user
	// (design 5.1 (a) / finding 5).
	createGraph: (data) => request('POST', '/graph/', data),
	loadGraph: (id) => request('GET', `/graph/${id}/load/`),
	activateGraph: (id) => request('POST', `/graph/${id}/activate/`, {}),

	createNode: (data) => request('POST', '/node/', data),
	updateNode: (id, data) => request('PATCH', `/node/${id}/`, data),
	deleteNode: (id) => request('DELETE', `/node/${id}/`),

	createEdge: (data) => request('POST', '/edge/', data),
	deleteEdge: (id) => request('DELETE', `/edge/${id}/`),

	listTools: () => request('GET', '/tool/'),
	createTool: (data) => request('POST', '/tool/', data),
	updateTool: (id, data) => request('PATCH', `/tool/${id}/`, data)
};

// --- (b) chat run client (FORM-ENCODED) -------------------------------------
//
// /chat/new/ and /chat/<id>/send/ are plain Django views that read
// request.POST, so these post application/x-www-form-urlencoded (via
// URLSearchParams) and MUST NOT set Content-Type: application/json.

export interface ChatResponse<T> {
	status: number;
	ok: boolean;
	body: T;
}

async function formPost<T = unknown>(
	path: string,
	fields: Record<string, string>
): Promise<ChatResponse<T>> {
	const params = new URLSearchParams();
	for (const [k, v] of Object.entries(fields)) params.append(k, v);
	const res = await _fetch(path, {
		method: 'POST',
		credentials: 'same-origin',
		headers: {
			Accept: 'application/json',
			'X-CSRFToken': csrfToken()
			// deliberately NO 'Content-Type: application/json'; the browser sets
			// application/x-www-form-urlencoded for a URLSearchParams body.
		},
		body: params
	});
	let body: unknown = null;
	try {
		body = await res.json();
	} catch {
		body = null;
	}
	return { status: res.status, ok: res.ok, body: body as T };
}

export interface NewSessionResponse {
	id: string;
	session_id: string;
	title: string;
	company_id: string;
}

export interface SendAssistant {
	content: string;
	citations?: string[] | null;
	chart_data?: unknown;
}

export interface SendResponse {
	user?: unknown;
	assistant?: SendAssistant;
	session?: unknown;
	error?: string;
}

/** POST /chat/new/ with form field company=<id>. */
export function newSession(companyId: string): Promise<ChatResponse<NewSessionResponse>> {
	return formPost<NewSessionResponse>('/chat/new/', { company: companyId });
}

/** POST /chat/<sessionId>/send/ with form field question=<...>. */
export function send(
	sessionId: string,
	question: string
): Promise<ChatResponse<SendResponse>> {
	return formPost<SendResponse>(`/chat/${sessionId}/send/`, { question });
}
