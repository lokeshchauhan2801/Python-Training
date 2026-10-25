// src/lib/adapter.test.ts — Vitest unit tests for the pure adapter module.
import { describe, it, expect } from 'vitest';
import type { AgentsApi } from './api';
import { ApiError } from './api';
import { NodeType, type FlowNode, type FlowNodeData } from './flow/node-schema';
import {
	loadToFlow,
	desiredEdges,
	diff,
	applyDiff,
	save,
	nodeChangedFields,
	guardAddOrChange,
	edgeKey,
	kindForType,
	isEndNode,
	GuardError,
	mapSerializerError,
	END_TYPE,
	type AdapterNodeType,
	type BackendNode,
	type LoadPayload,
	type Snapshot
} from './adapter';

// --- a recording fake AgentsApi ---------------------------------------------

interface Call {
	method: string;
	args: unknown[];
}

function makeFakeApi(overrides: Partial<Record<keyof AgentsApi, (...a: unknown[]) => unknown>> = {}): {
	api: AgentsApi;
	calls: Call[];
} {
	const calls: Call[] = [];
	const rec =
		(method: keyof AgentsApi) =>
		async (...args: unknown[]) => {
			calls.push({ method, args });
			if (overrides[method]) return overrides[method]!(...args);
			// createNode returns a row with an id so downstream code could map it
			if (method === 'createNode') return { id: `ulid-new-${calls.length}` };
			return {};
		};
	const api = {
		listGraphs: rec('listGraphs'),
		createGraph: rec('createGraph'),
		loadGraph: rec('loadGraph'),
		activateGraph: rec('activateGraph'),
		createNode: rec('createNode'),
		updateNode: rec('updateNode'),
		deleteNode: rec('deleteNode'),
		createEdge: rec('createEdge'),
		deleteEdge: rec('deleteEdge'),
		listTools: rec('listTools'),
		createTool: rec('createTool'),
		updateTool: rec('updateTool')
	} as unknown as AgentsApi;
	return { api, calls };
}

// --- fixtures ---------------------------------------------------------------

function backendNode(partial: Partial<BackendNode> & { id: string; kind: string }): BackendNode {
	return {
		graph: 'G1',
		slug: partial.id.toLowerCase(),
		label: partial.id,
		tools: [],
		position: { x: 0, y: 0 },
		config: {},
		...partial
	};
}

function flowNode(id: string, type: AdapterNodeType, data: Partial<FlowNodeData> = {}): FlowNode {
	return {
		id,
		type: 'textNode',
		position: { x: 0, y: 0 },
		width: 200,
		height: 200,
		data: {
			name: id,
			description: '',
			// END is an adapter-level sentinel string, cast into the vendored type slot
			type: type as NodeType,
			nexts: new Set<string>(),
			true_next: null,
			false_next: null,
			...data
		}
	};
}

// ----------------------------------------------------------------------------

describe('loadToFlow (backend -> canvas)', () => {
	it('folds plain / true / false edges into nexts / true_next / false_next', () => {
		const payload: LoadPayload = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodes: [
				backendNode({ id: 'A', kind: 'start' }),
				backendNode({ id: 'B', kind: 'step', config: { ui_type: 'CONDITION' } }),
				backendNode({ id: 'C', kind: 'step' }),
				backendNode({ id: 'D', kind: 'end' })
			],
			edges: [
				{ id: 'e1', graph: 'G1', source: 'A', target: 'B', condition: '' },
				{ id: 'e2', graph: 'G1', source: 'B', target: 'C', condition: 'true' },
				{ id: 'e3', graph: 'G1', source: 'B', target: 'D', condition: 'false' }
			]
		};
		const { nodes, snapshot } = loadToFlow(payload);
		const byId = new Map(nodes.map((n) => [n.id, n]));
		expect(byId.get('A')!.data.type).toBe(NodeType.START);
		// backend kind='end' loads as the adapter-level END sentinel
		expect(isEndNode(byId.get('D')!)).toBe(true);
		expect(byId.get('B')!.data.type).toBe(NodeType.CONDITION);
		expect([...byId.get('A')!.data.nexts]).toEqual(['B']);
		expect(byId.get('B')!.data.true_next).toBe('C');
		expect(byId.get('B')!.data.false_next).toBe('D');
		expect(snapshot.nodesByUlid.size).toBe(4);
		expect(snapshot.edgesByUlid.size).toBe(3);
	});

	it('restores TOOL/INFO/SUBGRAPH from config.ui_type and size/description from config', () => {
		const payload: LoadPayload = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodes: [
				backendNode({ id: 'T', kind: 'step', config: { ui_type: 'TOOL', ui_width: 320, ui_height: 140 } }),
				backendNode({ id: 'I', kind: 'step', config: { ui_type: 'INFO', non_executable: true, description: 'note' } }),
				backendNode({ id: 'S', kind: 'step', config: { ui_type: 'SUBGRAPH', subgraph_name: 'child' } })
			]
		};
		const { nodes } = loadToFlow(payload);
		const byId = new Map(nodes.map((n) => [n.id, n]));
		expect(byId.get('T')!.data.type).toBe(NodeType.TOOL);
		expect(byId.get('T')!.width).toBe(320);
		expect(byId.get('T')!.height).toBe(140);
		expect(byId.get('I')!.data.type).toBe(NodeType.INFO);
		expect(byId.get('I')!.data.description).toBe('note');
		expect(byId.get('S')!.data.type).toBe(NodeType.SUBGRAPH);
	});
});

describe('END sentinel round-trip (adapter-level, no vendored NodeType.END)', () => {
	it('maps the END sentinel to backend kind="end" (START->start, STEP->step)', () => {
		expect(kindForType(NodeType.START)).toBe('start');
		expect(kindForType(END_TYPE)).toBe('end');
		expect(kindForType(NodeType.STEP)).toBe('step');
		expect(kindForType(NodeType.TOOL)).toBe('step');
	});

	it('save-side: a newly created END node POSTs kind="end" with config.ui_type="END"', async () => {
		const { api, calls } = makeFakeApi();
		const snapshot: Snapshot = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodesByUlid: new Map(),
			edgesByUlid: new Map()
		};
		const nodes = [flowNode('end-canvas', END_TYPE, { name: 'Finish' })];
		await save(api, 'G1', nodes, snapshot, new Map(), async () => {});
		const create = calls.find((c) => c.method === 'createNode')!;
		const body = create.args[0] as Record<string, unknown>;
		expect(body.kind).toBe('end');
		expect((body.config as Record<string, unknown>).ui_type).toBe('END');
	});

	it('load-then-save round-trip: kind="end" survives as kind="end" (no spurious patch)', () => {
		const payload: LoadPayload = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodes: [backendNode({ id: 'E', kind: 'end', label: 'E', config: { ui_type: 'END' } })]
		};
		const { nodes, snapshot } = loadToFlow(payload);
		expect(isEndNode(nodes[0])).toBe(true);
		// no edits -> zero patches (END round-trips cleanly)
		const plan = diff(nodes, snapshot, new Map());
		expect(plan.nodePatches).toHaveLength(0);
		expect(plan.nodeCreates).toHaveLength(0);
		expect(plan.nodeDeletes).toHaveLength(0);
	});
});

describe('desiredEdges (canvas -> backend) + duplicate-pair guard', () => {
	it('flattens nexts/true_next/false_next with the matching condition', () => {
		const nodes = [
			flowNode('A', NodeType.START, { nexts: new Set(['B']) }),
			flowNode('B', NodeType.CONDITION, { true_next: 'C', false_next: 'D' }),
			flowNode('C', NodeType.STEP),
			flowNode('D', END_TYPE)
		];
		const desired = desiredEdges(nodes, (id) => id);
		expect(desired.get(edgeKey('A', 'B'))!.condition).toBe('');
		expect(desired.get(edgeKey('B', 'C'))!.condition).toBe('true');
		expect(desired.get(edgeKey('B', 'D'))!.condition).toBe('false');
	});

	it('throws GuardError when a CONDITION node has true_next === false_next', () => {
		const nodes = [flowNode('B', NodeType.CONDITION, { true_next: 'C', false_next: 'C' })];
		expect(() => desiredEdges(nodes, (id) => id)).toThrow(GuardError);
	});

	it('throws GuardError on two edges sharing the same (source,target) pair', () => {
		const nodes = [flowNode('A', NodeType.STEP, { nexts: new Set(['B']), true_next: 'B' })];
		expect(() => desiredEdges(nodes, (id) => id)).toThrow(GuardError);
	});
});

describe('guardAddOrChange (interactive START/END guard)', () => {
	it('rejects a second START / second END, allows the first', () => {
		expect(guardAddOrChange([NodeType.START], NodeType.START)).toMatch(/one START/);
		expect(guardAddOrChange([END_TYPE], END_TYPE)).toMatch(/one END/);
		expect(guardAddOrChange([NodeType.STEP], NodeType.START)).toBeNull();
		expect(guardAddOrChange([], END_TYPE)).toBeNull();
	});
});

describe('guards issue ZERO network calls', () => {
	const snapshot: Snapshot = {
		graph: { id: 'G1', name: 'g', company: 'C1' },
		nodesByUlid: new Map(),
		edgesByUlid: new Map()
	};

	it('a second START produces zero calls (GuardError before any request)', async () => {
		const { api, calls } = makeFakeApi();
		const nodes = [flowNode('x', NodeType.START), flowNode('y', NodeType.START)];
		await expect(
			save(api, 'G1', nodes, snapshot, new Map(), async () => {})
		).rejects.toThrow(GuardError);
		expect(calls).toHaveLength(0);
	});

	it('a second END (sentinel) produces zero calls', async () => {
		const { api, calls } = makeFakeApi();
		const nodes = [flowNode('x', END_TYPE), flowNode('y', END_TYPE)];
		await expect(
			save(api, 'G1', nodes, snapshot, new Map(), async () => {})
		).rejects.toThrow(GuardError);
		expect(calls).toHaveLength(0);
	});

	it('a duplicate (source,target) pair produces zero calls', async () => {
		const { api, calls } = makeFakeApi();
		const snap: Snapshot = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodesByUlid: new Map([
				['A', backendNode({ id: 'A', kind: 'step' })],
				['B', backendNode({ id: 'B', kind: 'step' })]
			]),
			edgesByUlid: new Map()
		};
		const nodes = [flowNode('A', NodeType.STEP, { nexts: new Set(['B']), true_next: 'B' }), flowNode('B', NodeType.STEP)];
		await expect(save(api, 'G1', nodes, snap, new Map(), async () => {})).rejects.toThrow(GuardError);
		expect(calls).toHaveLength(0);
	});

	it('CONDITION true_next===false_next produces zero calls', async () => {
		const { api, calls } = makeFakeApi();
		const snap: Snapshot = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodesByUlid: new Map([
				['B', backendNode({ id: 'B', kind: 'step' })],
				['C', backendNode({ id: 'C', kind: 'step' })]
			]),
			edgesByUlid: new Map()
		};
		const nodes = [flowNode('B', NodeType.CONDITION, { true_next: 'C', false_next: 'C' }), flowNode('C', NodeType.STEP)];
		await expect(save(api, 'G1', nodes, snap, new Map(), async () => {})).rejects.toThrow(GuardError);
		expect(calls).toHaveLength(0);
	});
});

describe('nodeChangedFields (per-field changed?)', () => {
	it('an unchanged node issues ZERO PATCHes (null)', () => {
		const snap = backendNode({ id: 'A', kind: 'step', label: 'A', position: { x: 10, y: 20 }, config: { canvas_id: 'A', backend_id: 'A' } });
		const flow = flowNode('A', NodeType.STEP, { name: 'A' });
		flow.position = { x: 10, y: 20 };
		expect(nodeChangedFields(flow, snap, [])).toBeNull();
	});

	it('bookkeeping keys never by themselves trigger a PATCH', () => {
		const snap = backendNode({ id: 'A', kind: 'step', label: 'A', config: {} });
		const flow = flowNode('A', NodeType.STEP, { name: 'A' });
		// configForNode injects canvas_id/backend_id; those are excluded from the test
		expect(nodeChangedFields(flow, snap, [])).toBeNull();
	});

	it('position-only drag PATCHes just position', () => {
		const snap = backendNode({ id: 'A', kind: 'step', label: 'A', position: { x: 0, y: 0 } });
		const flow = flowNode('A', NodeType.STEP, { name: 'A' });
		flow.position = { x: 5, y: 9 };
		const patch = nodeChangedFields(flow, snap, []);
		expect(patch).toEqual({ position: { x: 5, y: 9 } });
	});

	it('tools compared as an order-independent set', () => {
		const snap = backendNode({ id: 'A', kind: 'step', label: 'A', tools: ['t1', 't2'] });
		const flow = flowNode('A', NodeType.STEP, { name: 'A' });
		// same set, different order -> NOT a change
		expect(nodeChangedFields(flow, snap, ['t2', 't1'])).toBeNull();
		// different set -> change
		expect(nodeChangedFields(flow, snap, ['t1', 't3'])!.tools).toEqual(['t1', 't3']);
	});

	it('config compared on the whitelist only (description change -> PATCH)', () => {
		const snap = backendNode({ id: 'A', kind: 'step', label: 'A', config: { ui_type: 'STEP', description: 'old' } });
		const flow = flowNode('A', NodeType.STEP, { name: 'A', description: 'new' });
		const patch = nodeChangedFields(flow, snap, []);
		expect(patch).not.toBeNull();
		expect((patch!.config as Record<string, unknown>).description).toBe('new');
	});
});

describe('diff: created/updated/deleted sets', () => {
	it('classifies creates, patches and deletes for nodes and edges', () => {
		const snap: Snapshot = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodesByUlid: new Map([
				['A', backendNode({ id: 'A', kind: 'start', label: 'A' })],
				['B', backendNode({ id: 'B', kind: 'step', label: 'B' })],
				['Z', backendNode({ id: 'Z', kind: 'step', label: 'Z' })] // to be deleted
			]),
			edgesByUlid: new Map([['e1', { id: 'e1', graph: 'G1', source: 'A', target: 'Z', condition: '' }]])
		};
		// canvas: A (renamed), B (unchanged), NEW node, edge A->B; Z removed
		const A = flowNode('A', NodeType.START, { name: 'A2', nexts: new Set(['B']) });
		const B = flowNode('B', NodeType.STEP, { name: 'B' });
		const NEW = flowNode('new-canvas', NodeType.STEP, { name: 'New' });
		const plan = diff([A, B, NEW], snap, new Map());
		expect(plan.nodeCreates.map((n) => n.id)).toEqual(['new-canvas']);
		expect(plan.nodePatches.map((p) => p.ulid)).toEqual(['A']);
		expect(plan.nodeDeletes).toEqual(['Z']);
		expect(plan.edgeCreates.map((e) => `${e.source}->${e.target}`)).toEqual(['A->B']);
		expect(plan.edgeDeletes).toEqual(['e1']);
	});

	it('condition-only change => delete old edge then create new for the same pair', () => {
		const snap: Snapshot = {
			graph: { id: 'G1', name: 'g', company: 'C1' },
			nodesByUlid: new Map([
				['A', backendNode({ id: 'A', kind: 'step', label: 'A' })],
				['B', backendNode({ id: 'B', kind: 'step', label: 'B' })]
			]),
			edgesByUlid: new Map([['e1', { id: 'e1', graph: 'G1', source: 'A', target: 'B', condition: 'true' }]])
		};
		const A = flowNode('A', NodeType.CONDITION, { name: 'A', false_next: 'B' });
		const B = flowNode('B', NodeType.STEP, { name: 'B' });
		const plan = diff([A, B], snap, new Map());
		expect(plan.edgeDeletes).toEqual(['e1']);
		expect(plan.edgeCreates).toEqual([{ source: 'A', target: 'B', condition: 'false' }]);
	});
});

describe('applyDiff: ordering + error re-load', () => {
	it('applies in order nodes-create, edges-delete, edges-create, nodes-delete', async () => {
		const { api, calls } = makeFakeApi();
		const plan = {
			nodeCreates: [flowNode('n-new', NodeType.STEP, { name: 'N' })],
			nodePatches: [{ ulid: 'P', patch: { label: 'p' } }],
			nodeDeletes: ['DEL'],
			edgeCreates: [{ source: 'A', target: 'B', condition: '' }],
			edgeDeletes: ['e-old']
		};
		await applyDiff(api, 'G1', plan, async () => {});
		expect(calls.map((c) => c.method)).toEqual([
			'createNode',
			'updateNode',
			'deleteEdge',
			'createEdge',
			'deleteNode'
		]);
	});

	it('re-loads on a 400 and surfaces the mapped (field-name) message', async () => {
		let reloaded = 0;
		const { api } = makeFakeApi({
			createEdge: () => {
				throw new ApiError('bad', 400, { target: ['an edge cannot connect a node to itself'] });
			}
		});
		const plan = {
			nodeCreates: [],
			nodePatches: [],
			nodeDeletes: [],
			edgeCreates: [{ source: 'A', target: 'A', condition: '' }],
			edgeDeletes: []
		};
		await expect(
			applyDiff(api, 'G1', plan, async () => {
				reloaded++;
			})
		).rejects.toThrow("A node can't connect to itself.");
		expect(reloaded).toBe(1);
	});

	it('re-loads on a leaked 500 duplicate edge and surfaces the heuristic copy', async () => {
		let reloaded = 0;
		const { api } = makeFakeApi({
			createEdge: () => {
				throw new ApiError('server error', 500, null);
			}
		});
		const plan = {
			nodeCreates: [],
			nodePatches: [],
			nodeDeletes: [],
			edgeCreates: [{ source: 'A', target: 'B', condition: '' }],
			edgeDeletes: []
		};
		await expect(
			applyDiff(api, 'G1', plan, async () => {
				reloaded++;
			})
		).rejects.toThrow('That connection already exists.');
		expect(reloaded).toBe(1);
	});
});

describe('mapSerializerError (keyed by FIELD NAME)', () => {
	it('maps each known serializer 400 field to friendly copy', () => {
		expect(mapSerializerError({ slug: ['x'] })).toMatch(/already exists/);
		expect(mapSerializerError({ target: ['an edge cannot connect a node to itself'] })).toMatch(/itself/);
		expect(mapSerializerError({ target: ['an edge must connect nodes of the same graph'] })).toMatch(/same graph/);
		expect(mapSerializerError({ graph: ['edge graph must match'] })).toMatch(/same graph/);
		expect(mapSerializerError({ company: ['ambiguous; specify a company'] })).toMatch(/active company/);
		expect(mapSerializerError({ weird: ['?'] })).toBeNull();
	});
});
