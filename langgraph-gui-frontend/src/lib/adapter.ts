// src/lib/adapter.ts — NEWLY AUTHORED (not vendored).
//
// Bidirectional mapping between the vendored SvelteFlow node model
// (src/lib/flow/node-schema.ts) and the backend AgentGraph/AgentNode/AgentEdge
// rows, plus a snapshot-diff save (design 3.3 / 3.4 / 5.2 / 5.3).
//
// This module is a dependency-injected PURE module: it takes an AgentsApi (so
// tests inject a fake and assert the exact call sequence / zero-network guards)
// and owns the START/END uniqueness guard and the duplicate (source,target)
// edge-pair guard as the REAL enforcement — the backend enforces NEITHER on the
// API write path (a 2nd START/END returns 201; a dup edge pair returns an
// unhandled 500), so these guards must run BEFORE any POST/PATCH.

import type { AgentsApi } from './api';
import { ApiError } from './api';
import { NodeType, type FlowNode, type FlowNodeData } from './flow/node-schema';

// --- END sentinel (adapter-level; NOT a vendored NodeType member) -----------
//
// The vendored upstream NodeType enum (src/lib/flow/node-schema.ts) has exactly
// six values — START, STEP, TOOL, CONDITION, INFO, SUBGRAPH — and NO END (this
// matches FEAT-001's recorded node-schema diff: "NO DELTA" against those six).
// The backend, however, has kind='end' terminal nodes that MUST round-trip. Per
// the approved design decision we represent an END node as an ADAPTER-LEVEL
// SENTINEL rather than editing the vendored enum: the FlowNode carries
// data.type === END_TYPE and config.ui_type === 'END', exactly the same
// config.ui_type mechanism already used for TOOL/INFO/SUBGRAPH. This keeps the
// vendored node-schema.ts byte-for-byte verbatim (clean re-vendoring) and the
// backend unchanged, while load->edit->save->reload stays lossless. The END
// sentinel is kind='end' on the backend (semantically terminal), distinct from
// the TOOL/INFO/SUBGRAPH steps which are kind='step'.
export const END_TYPE = 'END' as const;

/** Adapter's node-type space = the vendored six PLUS the END sentinel. */
export type AdapterNodeType = NodeType | typeof END_TYPE;

/** True when a FlowNode's type is the adapter-level END sentinel. */
export function isEndType(type: AdapterNodeType): boolean {
	return (type as string) === END_TYPE;
}

/** True when a FlowNode is an END-sentinel node. */
export function isEndNode(node: FlowNode): boolean {
	return isEndType(node.data.type as AdapterNodeType);
}

// --- backend payload shapes (as returned by /graph/<id>/load/) --------------

export interface BackendNode {
	id: string;
	graph: string;
	slug: string;
	kind: string; // 'start' | 'step' | 'end'
	label: string;
	tools: string[];
	prompt_name?: string | null;
	position: { x?: number; y?: number } | null;
	config: Record<string, unknown> | null;
}

export interface BackendEdge {
	id: string;
	graph: string;
	source: string;
	target: string;
	condition: string; // '' | 'true' | 'false'
}

export interface BackendGraph {
	id: string;
	name: string;
	is_active?: boolean;
	company: string;
}

export interface LoadPayload {
	graph: BackendGraph;
	tools?: unknown[];
	nodes?: BackendNode[];
	edges?: BackendEdge[];
}

// --- config whitelist (the ONLY keys that count as a user edit, design 5.2) --

export const CONFIG_WHITELIST = [
	'ui_type',
	'ui_width',
	'ui_height',
	'description',
	'ui_tool_text',
	'subgraph_name',
	'non_executable'
] as const;

// Bookkeeping keys the adapter writes into config but which never by themselves
// trigger a PATCH — they are deterministic derivations, not user edits, and are
// intentionally ABSENT from CONFIG_WHITELIST so the "changed?" test ignores them.
export const BOOKKEEPING_KEYS = ['backend_id', 'canvas_id'] as const;

export class GuardError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'GuardError';
	}
}

// --- kind <-> ui_type mapping (design 3.3) ----------------------------------

/** Backend kind for an adapter node type (START/END are kinds; else 'step'). */
export function kindForType(type: AdapterNodeType): string {
	if (type === NodeType.START) return 'start';
	if (isEndType(type)) return 'end';
	return 'step';
}

/** Build the backend config for a canvas node (design 3.3 field mapping). */
function configForNode(node: FlowNode, canvasId: string, backendId?: string): Record<string, unknown> {
	const data = node.data;
	const type = data.type as AdapterNodeType;
	const config: Record<string, unknown> = {};
	// ui_type preserves the richer upstream type losslessly. START has no
	// ui_type (its kind IS 'start') and a plain STEP has no richer type, so
	// neither carries a ui_type — this keeps a plain-step round-trip
	// (load config {} -> save) from spuriously introducing a ui_type key. The
	// END sentinel DOES carry config.ui_type='END' (kind='end' alone cannot be
	// distinguished from a plain terminal on reload without it, and it is the
	// mechanism that makes the sentinel survive the round-trip).
	if (type !== NodeType.START && type !== NodeType.STEP) {
		config.ui_type = isEndType(type) ? END_TYPE : (type as string);
	}
	// Size is written only when it deviates from the canvas default (200), so an
	// un-resized node round-trips without introducing ui_width/ui_height keys
	// that the load payload did not carry (matches the load default of 200).
	if (typeof node.width === 'number' && node.width !== 200) config.ui_width = node.width;
	if (typeof node.height === 'number' && node.height !== 200) config.ui_height = node.height;
	if (data.description) config.description = data.description;
	if (data.type === NodeType.INFO) config.non_executable = true;
	if (data.type === NodeType.SUBGRAPH) config.subgraph_name = data.name;
	// bookkeeping (excluded from the change test, see nodeChangedFields)
	config.canvas_id = canvasId;
	if (backendId) config.backend_id = backendId;
	return config;
}

// --- LOAD: backend payload -> FlowNodes + snapshot --------------------------

export interface Snapshot {
	graph: BackendGraph;
	nodesByUlid: Map<string, BackendNode>;
	edgesByUlid: Map<string, BackendEdge>;
}

export interface LoadResult {
	nodes: FlowNode[];
	snapshot: Snapshot;
	/** canvas id (== ULID here) -> ULID; rebuilt on every load. */
	idMap: Map<string, string>;
}

function uiTypeToNodeType(kind: string, config: Record<string, unknown>): AdapterNodeType {
	if (kind === 'start') return NodeType.START;
	// backend kind='end' -> the adapter-level END sentinel (config.ui_type is
	// 'END' on round-tripped nodes, but kind='end' is the authoritative signal).
	if (kind === 'end') return END_TYPE;
	const ui = config.ui_type;
	if (typeof ui === 'string' && (Object.values(NodeType) as string[]).includes(ui)) {
		return ui as NodeType;
	}
	return NodeType.STEP;
}

/**
 * Build FlowNodes from a /graph/<id>/load/ payload. The canvas id is the ULID
 * (durable identity), so the id-map is identity on load. Edges are folded back
 * into each source node's nexts / true_next / false_next.
 */
export function loadToFlow(payload: LoadPayload): LoadResult {
	const nodes = payload.nodes ?? [];
	const edges = payload.edges ?? [];

	const nodesByUlid = new Map<string, BackendNode>();
	const edgesByUlid = new Map<string, BackendEdge>();
	const idMap = new Map<string, string>();

	const flowByUlid = new Map<string, FlowNode>();

	for (const n of nodes) {
		nodesByUlid.set(n.id, n);
		idMap.set(n.id, n.id);
		const config = (n.config ?? {}) as Record<string, unknown>;
		const type = uiTypeToNodeType(n.kind, config);
		const pos = n.position ?? {};
		const data: FlowNodeData = {
			name: n.label || n.slug,
			description: typeof config.description === 'string' ? config.description : '',
			// `type` is the adapter node-type space (vendored six + END sentinel);
			// the END sentinel is a plain string the vendored components treat as
			// an unknown type (rendered via the END-distinct styling in the UI).
			type: type as NodeType,
			nexts: new Set<string>(),
			true_next: null,
			false_next: null
		};
		const flow: FlowNode = {
			id: n.id,
			type: 'textNode',
			position: { x: typeof pos.x === 'number' ? pos.x : 0, y: typeof pos.y === 'number' ? pos.y : 0 },
			width: typeof config.ui_width === 'number' ? config.ui_width : 200,
			height: typeof config.ui_height === 'number' ? config.ui_height : 200,
			data
		};
		flowByUlid.set(n.id, flow);
	}

	// Fold first-class edges onto the source node (design 3.3 edges / load).
	for (const e of edges) {
		edgesByUlid.set(e.id, e);
		const src = flowByUlid.get(e.source);
		if (!src) continue;
		if (e.condition === 'true') {
			src.data.true_next = e.target;
		} else if (e.condition === 'false') {
			src.data.false_next = e.target;
		} else {
			src.data.nexts.add(e.target);
		}
	}

	return {
		nodes: Array.from(flowByUlid.values()),
		snapshot: {
			graph: payload.graph,
			nodesByUlid,
			edgesByUlid
		},
		idMap
	};
}

// --- SAVE: desired edge set derived from node nexts/true_next/false_next -----

export interface DesiredEdge {
	source: string; // ULID
	target: string; // ULID
	condition: string; // '' | 'true' | 'false'
}

/** edge map key is the (source,target) PAIR ONLY — condition is NOT part of identity. */
export function edgeKey(source: string, target: string): string {
	return `${source}\u0000${target}`;
}

/**
 * Flatten each canvas node's nexts/true_next/false_next into a desired edge map
 * keyed by (sourceUlid,targetUlid). Throws GuardError on a duplicate pair —
 * including a CONDITION node whose true_next === false_next (design 5.2).
 * canvasToUlid resolves the canvas id to its backend ULID (undefined for a
 * brand-new node that has no ULID yet; such edges cannot be created until the
 * node exists, so they are keyed by canvas id and resolved at apply time).
 */
export function desiredEdges(
	flowNodes: FlowNode[],
	resolve: (canvasId: string) => string
): Map<string, DesiredEdge> {
	const desired = new Map<string, DesiredEdge>();
	const add = (sourceCanvas: string, targetCanvas: string, condition: string) => {
		const source = resolve(sourceCanvas);
		const target = resolve(targetCanvas);
		const key = edgeKey(source, target);
		if (desired.has(key)) {
			throw new GuardError('A node can have only one edge to a given target');
		}
		desired.set(key, { source, target, condition });
	};
	for (const node of flowNodes) {
		const d = node.data;
		for (const t of d.nexts) add(node.id, t, '');
		if (d.true_next != null) add(node.id, d.true_next, 'true');
		if (d.false_next != null) add(node.id, d.false_next, 'false');
	}
	return desired;
}

/**
 * START/END uniqueness guard (adapter-owned, design 3.4 / 5.2). Throws
 * GuardError if more than one start (or more than one end) is present. Used both
 * interactively (on add / kind-change) and before a diff.
 */
export function assertSingleStartEnd(flowNodes: FlowNode[]): void {
	let starts = 0;
	let ends = 0;
	for (const n of flowNodes) {
		if (n.data.type === NodeType.START) starts++;
		if (isEndNode(n)) ends++; // END-sentinel, not a NodeType enum member
	}
	if (starts > 1) throw new GuardError('Only one START node is allowed.');
	if (ends > 1) throw new GuardError('Only one END node is allowed.');
}

/**
 * Interactive guard for a single add / kind-change: would adding a node of
 * `type` (or changing a node to `type`) exceed one START / one END, given the
 * types already present (excluding the node being changed)? Returns the guard
 * message to surface, or null if allowed. The caller reverts WITHOUT any POST.
 */
export function guardAddOrChange(
	existingTypes: AdapterNodeType[],
	type: AdapterNodeType
): string | null {
	if (type === NodeType.START && existingTypes.includes(NodeType.START)) {
		return 'Only one START node is allowed.';
	}
	if (isEndType(type) && existingTypes.some((t) => isEndType(t))) {
		return 'Only one END node is allowed.';
	}
	return null;
}

// --- per-field "changed?" test (design 5.2) ---------------------------------

function positionEqual(a: { x?: number; y?: number } | null, b: { x?: number; y?: number } | null): boolean {
	const ax = a?.x ?? 0;
	const ay = a?.y ?? 0;
	const bx = b?.x ?? 0;
	const by = b?.y ?? 0;
	return ax === bx && ay === by;
}

function toolsEqual(a: string[], b: string[]): boolean {
	const sa = new Set(a);
	const sb = new Set(b);
	if (sa.size !== sb.size) return false;
	for (const x of sa) if (!sb.has(x)) return false;
	return true;
}

/** Compare config on the whitelist ONLY (bookkeeping keys excluded). */
function configWhitelistEqual(
	a: Record<string, unknown>,
	b: Record<string, unknown>
): boolean {
	for (const key of CONFIG_WHITELIST) {
		const av = a[key];
		const bv = b[key];
		if (JSON.stringify(av ?? null) !== JSON.stringify(bv ?? null)) return false;
	}
	return true;
}

export interface NodePatch {
	label?: string;
	kind?: string;
	position?: { x: number; y: number };
	config?: Record<string, unknown>;
	tools?: string[];
}

/**
 * Compute the PATCH body for a canvas node against its snapshot row — only the
 * changed fields. Returns null when nothing a user could edit changed (so an
 * unchanged node issues ZERO PATCHes). Bookkeeping config keys never by
 * themselves trigger a PATCH, but when any whitelisted key changed the FULL
 * merged config (incl. bookkeeping) is written.
 */
export function nodeChangedFields(
	flow: FlowNode,
	snapshot: BackendNode,
	desiredTools: string[]
): NodePatch | null {
	const patch: NodePatch = {};
	const snapConfig = (snapshot.config ?? {}) as Record<string, unknown>;
	const desiredConfig = configForNode(flow, snapshot.id, snapshot.id);

	const label = flow.data.name;
	if (label !== snapshot.label) patch.label = label;

	const kind = kindForType(flow.data.type);
	if (kind !== snapshot.kind) patch.kind = kind;

	const desiredPos = { x: flow.position.x, y: flow.position.y };
	if (!positionEqual(desiredPos, snapshot.position)) patch.position = desiredPos;

	if (!toolsEqual(desiredTools, snapshot.tools ?? [])) patch.tools = desiredTools;

	if (!configWhitelistEqual(desiredConfig, snapConfig)) {
		// send full merged config (whitelist changed), preserving bookkeeping
		patch.config = desiredConfig;
	}

	return Object.keys(patch).length === 0 ? null : patch;
}

// --- error mapping (design 5.3 (A), keyed by FIELD NAME, never message text) -

export function mapSerializerError(body: unknown): string | null {
	if (!body || typeof body !== 'object') return null;
	const b = body as Record<string, unknown>;
	if ('slug' in b) return 'A node with this name already exists; rename it.';
	if ('target' in b) {
		const msg = Array.isArray(b.target) ? String(b.target[0]) : String(b.target);
		if (msg.includes('itself')) return "A node can't connect to itself.";
		return 'Both ends must be in the same graph.';
	}
	if ('graph' in b) return 'Both ends must be in the same graph.';
	if ('company' in b) return "This graph isn't available for the active company.";
	return null;
}

// --- the diff + apply (design 5.2 ordering) ---------------------------------

export interface SaveResult {
	createdNodes: number;
	patchedNodes: number;
	deletedNodes: number;
	createdEdges: number;
	deletedEdges: number;
}

export interface DiffPlan {
	nodeCreates: FlowNode[];
	nodePatches: { ulid: string; patch: NodePatch }[];
	nodeDeletes: string[]; // ULIDs
	edgeCreates: DesiredEdge[];
	edgeDeletes: string[]; // edge ULIDs
}

/**
 * Compute the diff plan between the canvas (flowNodes + per-node tool selection)
 * and the loaded snapshot. Runs the START/END and duplicate-edge-pair guards
 * FIRST — a guard violation throws GuardError and NO plan (hence no network
 * call) is produced. toolsByCanvasId supplies the desired tool ULIDs per node.
 */
export function diff(
	flowNodes: FlowNode[],
	snapshot: Snapshot,
	toolsByCanvasId: Map<string, string[]>
): DiffPlan {
	// Guards first — before any request is contemplated.
	assertSingleStartEnd(flowNodes);

	const hasUlid = (canvasId: string) => snapshot.nodesByUlid.has(canvasId);
	// For the edge guard/diff we only consider edges whose endpoints both have a
	// ULID in the snapshot (brand-new nodes have no ULID yet; their edges are
	// created on the next save after the node exists).
	const resolve = (canvasId: string) => canvasId; // canvas id == ULID in v1

	// desiredEdges throws GuardError on a duplicate (source,target) pair.
	const desired = desiredEdges(flowNodes, resolve);

	// Node diff.
	const nodeCreates: FlowNode[] = [];
	const nodePatches: { ulid: string; patch: NodePatch }[] = [];
	const seenUlids = new Set<string>();
	for (const node of flowNodes) {
		if (!hasUlid(node.id)) {
			nodeCreates.push(node);
			continue;
		}
		seenUlids.add(node.id);
		const snap = snapshot.nodesByUlid.get(node.id)!;
		const tools = toolsByCanvasId.get(node.id) ?? snap.tools ?? [];
		const patch = nodeChangedFields(node, snap, tools);
		if (patch) nodePatches.push({ ulid: node.id, patch });
	}
	const nodeDeletes: string[] = [];
	for (const ulid of snapshot.nodesByUlid.keys()) {
		if (!seenUlids.has(ulid)) nodeDeletes.push(ulid);
	}

	// Edge diff — identity is the (source,target) PAIR only. A condition-only
	// change is a delete-of-old-row + create-of-new for that pair.
	const snapByPair = new Map<string, BackendEdge>();
	for (const e of snapshot.edgesByUlid.values()) {
		snapByPair.set(edgeKey(e.source, e.target), e);
	}
	const edgeCreates: DesiredEdge[] = [];
	const edgeDeletes: string[] = [];
	// only diff edges whose endpoints exist in the snapshot (both ULIDs known)
	for (const [key, de] of desired) {
		const existsNow = hasUlid(de.source) && hasUlid(de.target);
		if (!existsNow) {
			// endpoint is a brand-new node; defer to the next save
			continue;
		}
		const snap = snapByPair.get(key);
		if (!snap) {
			edgeCreates.push(de);
		} else if (snap.condition !== de.condition) {
			edgeDeletes.push(snap.id);
			edgeCreates.push(de);
		}
	}
	for (const [key, snap] of snapByPair) {
		if (!desired.has(key)) edgeDeletes.push(snap.id);
	}

	return { nodeCreates, nodePatches, nodeDeletes, edgeCreates, edgeDeletes };
}

/** Build the POST /node/ body for a freshly-created canvas node. */
function createNodeBody(graphId: string, node: FlowNode, tools: string[]): Record<string, unknown> {
	const kind = kindForType(node.data.type);
	const shortid = Math.random().toString(36).slice(2, 8);
	const base = (node.data.name || kind)
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/(^-|-$)/g, '') || kind;
	return {
		graph: graphId,
		slug: `${base}-${shortid}`,
		kind,
		label: node.data.name || kind,
		position: { x: node.position.x, y: node.position.y },
		config: configForNode(node, node.id),
		tools
	};
}

/**
 * Apply a diff plan via the injected api, in the constraint-safe order
 * (design 5.2): (1) create/patch nodes, (2) delete edges, (3) create edges,
 * (4) delete nodes. On ANY non-2xx the plan stops, reload() is invoked to
 * resync, and the error is re-thrown (never silently swallowed). Per-call
 * serializer 400s are mapped to friendly copy by FIELD NAME.
 */
export async function applyDiff(
	api: AgentsApi,
	graphId: string,
	plan: DiffPlan,
	reload: () => Promise<void>
): Promise<SaveResult> {
	const result: SaveResult = {
		createdNodes: 0,
		patchedNodes: 0,
		deletedNodes: 0,
		createdEdges: 0,
		deletedEdges: 0
	};
	try {
		// (1) create + patch nodes
		for (const node of plan.nodeCreates) {
			await api.createNode(createNodeBody(graphId, node, []));
			result.createdNodes++;
		}
		for (const { ulid, patch } of plan.nodePatches) {
			await api.updateNode(ulid, patch);
			result.patchedNodes++;
		}
		// (2) delete edges
		for (const id of plan.edgeDeletes) {
			await api.deleteEdge(id);
			result.deletedEdges++;
		}
		// (3) create edges
		for (const de of plan.edgeCreates) {
			await api.createEdge({
				graph: graphId,
				source: de.source,
				target: de.target,
				condition: de.condition
			});
			result.createdEdges++;
		}
		// (4) delete nodes last
		for (const id of plan.nodeDeletes) {
			await api.deleteNode(id);
			result.deletedNodes++;
		}
	} catch (err) {
		// Re-load to resync on ANY non-2xx, then re-throw with mapped copy.
		await reload();
		if (err instanceof ApiError) {
			const friendly = mapSerializerError(err.body);
			if (friendly) {
				const mapped = new ApiError(friendly, err.status, err.body);
				throw mapped;
			}
			if (err.status >= 500) {
				throw new ApiError('That connection already exists.', err.status, err.body);
			}
		}
		throw err;
	}
	return result;
}

/**
 * Full save: compute the diff (running guards) and apply it. A GuardError from
 * the diff is thrown BEFORE any network call is made (zero requests).
 */
export async function save(
	api: AgentsApi,
	graphId: string,
	flowNodes: FlowNode[],
	snapshot: Snapshot,
	toolsByCanvasId: Map<string, string[]>,
	reload: () => Promise<void>
): Promise<SaveResult> {
	const plan = diff(flowNodes, snapshot, toolsByCanvasId);
	return applyDiff(api, graphId, plan, reload);
}
