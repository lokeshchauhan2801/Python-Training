// src/lib/canvas.store.svelte.ts — NEWLY AUTHORED (not vendored).
//
// Canvas <-> backend wiring state for the hosted graph editor (design 4.2/5.2/6).
// Owns the iframe querystring contract (?csrf&company&company_name), the active
// graph id, the last-loaded snapshot (so adapter.save can diff), the company's
// Tool rows (surfaced as STEP-node checkboxes), and the Save/reload/first-visit
// flow. Persistence is via lib/api.ts + lib/adapter.ts — the upstream FastAPI
// FileTransmit/graphs-io path is NOT used.

import { writable, get } from 'svelte/store';
import { api, type AgentsApi } from './api';
import { currentNodes } from './flow/graphs.store.svelte';
import type { FlowNode } from './flow/node-schema';
import {
	loadToFlow,
	save as adapterSave,
	type Snapshot,
	type LoadPayload
} from './adapter';

/** A company Tool row as returned in the /graph/<id>/load/ payload. */
export interface ToolRow {
	id: string;
	slug?: string;
	name?: string;
	is_enabled?: boolean;
}

// --- iframe querystring contract (design 4.3): ?csrf&company&company_name ----

function readQuery(name: string): string {
	if (typeof window === 'undefined' || !window.location) return '';
	return new URLSearchParams(window.location.search).get(name) || '';
}

export const activeCompanyId = writable<string>('');
export const activeCompanyName = writable<string>('');

// --- backend-wiring state ----------------------------------------------------

/** Active graph ULID (null until the first-visit flow resolves one). */
export const graphId = writable<string | null>(null);
/** graph.company FK id from the current load payload (design 4.3 step 2 guard). */
export const loadedGraphCompany = writable<string>('');
/** graph.name / is_active banner state. */
export const graphName = writable<string>('');
export const graphIsActive = writable<boolean>(false);
/** The company's Tool rows (STEP-node checkbox source). */
export const tools = writable<ToolRow[]>([]);
/** Last-loaded snapshot; adapter.save diffs the canvas against it. */
export const snapshot = writable<Snapshot | null>(null);
/**
 * Per-canvas-node desired tool selection (ULID[] keyed by canvas node id).
 * Seeded from the load payload's node.tools and edited via the STEP-node
 * checkboxes; stored as the Tool M2M on save (NOT free text).
 */
export const nodeTools = writable<Map<string, string[]>>(new Map());

/** Error/status banner copy. */
export const banner = writable<string>('');
export const loading = writable<boolean>(false);

/** True after the canvas state diverges from the last snapshot (best-effort). */
export const dirty = writable<boolean>(false);

/** Mark the canvas dirty (called on any node/edge/tool edit). */
export function markDirty(): void {
	dirty.set(true);
}

// --- load / first-visit ------------------------------------------------------

function toToolRows(payload: LoadPayload): ToolRow[] {
	const raw = Array.isArray(payload.tools) ? payload.tools : [];
	return raw.filter((t): t is ToolRow => !!t && typeof t === 'object');
}

/** Apply a /graph/<id>/load/ payload to the canvas + snapshot + stores. */
function applyLoad(payload: LoadPayload): void {
	const result = loadToFlow(payload);
	currentNodes.set(result.nodes);
	snapshot.set(result.snapshot);
	loadedGraphCompany.set(payload.graph?.company ?? '');
	graphName.set(payload.graph?.name ?? '');
	graphIsActive.set(Boolean(payload.graph?.is_active));
	tools.set(toToolRows(payload));
	// Seed per-node tool selection from the backend node.tools.
	const seeded = new Map<string, string[]>();
	for (const n of payload.nodes ?? []) {
		seeded.set(n.id, Array.isArray(n.tools) ? n.tools.map(String) : []);
	}
	nodeTools.set(seeded);
	dirty.set(false);
}

/** Load a specific graph by id and apply it to the canvas. */
export async function loadInto(client: AgentsApi, id: string): Promise<void> {
	const payload = (await client.loadGraph(id)) as LoadPayload;
	applyLoad(payload);
}

/** Reload the active graph (resync after a failed save). */
export async function reload(client: AgentsApi = api): Promise<void> {
	const id = get(graphId);
	if (id) await loadInto(client, id);
}

/**
 * First-visit flow (design 6 "Empty / first visit"): GET /graph/ first; only
 * create a 'Default agent' if none exist. Then list/pick/activate rather than
 * assuming a singleton. Picks the active graph if one exists, else the first.
 */
export async function bootstrap(client: AgentsApi = api): Promise<void> {
	loading.set(true);
	banner.set('');
	try {
		const company = get(activeCompanyId);
		const listed = await client.listGraphs();
		const list = normalizeList(listed);
		let chosen = list.find((g) => g && g.is_active) ?? list[0];
		if (!chosen) {
			if (!company) {
				banner.set("This company isn't available for editing — switch to a company you own.");
				return;
			}
			chosen = (await client.createGraph({ name: 'Default agent', company })) as {
				id: string;
				is_active?: boolean;
			};
		}
		graphId.set(chosen.id);
		await loadInto(client, chosen.id);
	} catch (err) {
		banner.set(bootstrapError(err));
	} finally {
		loading.set(false);
	}
}

function normalizeList(listed: unknown): { id: string; is_active?: boolean }[] {
	if (Array.isArray(listed)) return listed as { id: string; is_active?: boolean }[];
	if (listed && typeof listed === 'object' && 'results' in (listed as Record<string, unknown>)) {
		const r = (listed as Record<string, unknown>).results;
		if (Array.isArray(r)) return r as { id: string; is_active?: boolean }[];
	}
	return [];
}

function bootstrapError(err: unknown): string {
	// A member-not-owner active company fails the first read/write (design 4.3
	// multi-tenant scope boundary): listGraphs is empty (handled above) or
	// createGraph 400/403/404s.
	const status = (err as { status?: number })?.status;
	if (status === 400 || status === 403 || status === 404) {
		return "This company isn't available for editing — switch to a company you own.";
	}
	if (err instanceof Error) return err.message;
	return String(err);
}

// --- save --------------------------------------------------------------------

/**
 * Save the canvas to the backend via the adapter snapshot-diff. Resolves on a
 * clean save (clears dirty) and REJECTS on any non-2xx (adapter has already
 * re-loaded to resync). Used both by the explicit Save button and by run.ts's
 * pre-run flush.
 */
export async function save(client: AgentsApi = api): Promise<void> {
	const id = get(graphId);
	const snap = get(snapshot);
	if (!id || !snap) throw new Error('No graph loaded.');
	const flow: FlowNode[] = get(currentNodes);
	await adapterSave(client, id, flow, snap, get(nodeTools), () => reload(client));
	dirty.set(false);
	// Re-sync the snapshot after a clean save so the next diff is minimal.
	await loadInto(client, id);
}

/** Flush unsaved edits only when dirty (run.ts pre-run step 1). */
export async function saveIfDirty(client: AgentsApi = api): Promise<void> {
	if (get(dirty)) await save(client);
}

// --- init --------------------------------------------------------------------

/** Read the iframe querystring contract into the stores. Call once on mount. */
export function initFromQuery(): void {
	activeCompanyId.set(readQuery('company'));
	activeCompanyName.set(readQuery('company_name'));
}
