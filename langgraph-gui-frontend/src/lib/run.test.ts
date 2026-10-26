// src/lib/run.test.ts — Vitest unit tests for the run-from-canvas module.
import { describe, it, expect, vi } from 'vitest';
import type { AgentsApi, ChatResponse, NewSessionResponse, SendResponse } from './api';
import { ApiError } from './api';
import {
	run,
	MSG_COMPANY_MISMATCH,
	MSG_LM_FAILED,
	MSG_LM_UNCONFIGURED,
	MSG_MEMBER_NOT_OWNER,
	type RunDeps,
	type RunContext
} from './run';

const ACTIVATE_DETAIL =
	'This graph is not runnable yet: add a START node and at least one STEP node before activating.';

function ctx(overrides: Partial<RunContext> = {}): RunContext {
	return { graphId: 'G1', loadedGraphCompany: 'C1', activeCompanyId: 'C1', ...overrides };
}

function chatOk<T>(body: T): ChatResponse<T> {
	return { status: 201, ok: true, body };
}
function chatErr<T>(status: number, body: T): ChatResponse<T> {
	return { status, ok: false, body };
}

function makeDeps(overrides: Partial<RunDeps> = {}): { deps: RunDeps; order: string[] } {
	const order: string[] = [];
	const api = {
		activateGraph: vi.fn(async () => {
			order.push('activate');
			return {};
		})
	} as unknown as AgentsApi;
	const deps: RunDeps = {
		api,
		newSession: vi.fn(async (): Promise<ChatResponse<NewSessionResponse>> => {
			order.push('new');
			// Mirrors chat.views.new_session EXACTLY: {id, title, company_id}.
			// (No session_id key — reading it caused POST /chat/undefined/send/.)
			return chatOk({ id: 'S1', title: 't', company_id: 'C1' });
		}),
		send: vi.fn(async (): Promise<ChatResponse<SendResponse>> => {
			order.push('send');
			return chatOk({ assistant: { content: 'hi', citations: [], chart_data: null } });
		}),
		saveIfDirty: vi.fn(async () => {
			order.push('save');
		}),
		reload: vi.fn(async () => {
			order.push('reload');
		}),
		...overrides
	};
	return { deps, order };
}

describe('run: happy path sequence', () => {
	it('runs save -> activate -> new -> send in order and renders content', async () => {
		const { deps, order } = makeDeps();
		const out = await run(deps, ctx(), 'hello');
		expect(order).toEqual(['save', 'activate', 'new', 'send']);
		expect(out.ok).toBe(true);
		expect(out.text).toBe('hi');
		// Regression guard: send must be called with the session's `id` from
		// new_session (not an undefined `session_id`, which 404'd as
		// /chat/undefined/send/).
		expect(deps.send).toHaveBeenCalledWith('S1', 'hello');
	});

	it('appends chart_data JSON and citations when present', async () => {
		const { deps } = makeDeps({
			send: vi.fn(async () =>
				chatOk<SendResponse>({
					assistant: { content: 'body', chart_data: { a: 1 }, citations: ['c1', 'c2'] }
				})
			)
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.chartJson).toBe(JSON.stringify({ a: 1 }, null, 2));
		expect(out.citations).toEqual(['c1', 'c2']);
	});
});

describe('run: pre-run save failure aborts before activate', () => {
	it('does not call activate when saveIfDirty rejects', async () => {
		const { deps, order } = makeDeps({
			saveIfDirty: vi.fn(async () => {
				order.push('save');
				throw new ApiError('A node with this name already exists; rename it.', 400, { slug: ['x'] });
			})
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.ok).toBe(false);
		expect(order).toEqual(['save']);
		expect(deps.api.activateGraph).not.toHaveBeenCalled();
	});
});

describe('run: active-company guard', () => {
	it('aborts before activate when loadedGraphCompany !== activeCompanyId', async () => {
		const { deps, order } = makeDeps();
		const out = await run(deps, ctx({ loadedGraphCompany: 'OTHER' }), 'q');
		expect(out.ok).toBe(false);
		expect(out.error).toBe(MSG_COMPANY_MISMATCH);
		expect(order).toEqual(['save']);
		expect(deps.api.activateGraph).not.toHaveBeenCalled();
	});
});

describe('run: activate 400 renders detail VERBATIM and stops', () => {
	it('surfaces detail and never calls newSession', async () => {
		const { deps } = makeDeps({
			api: {
				activateGraph: vi.fn(async () => {
					throw new ApiError('activate failed', 400, { detail: ACTIVATE_DETAIL });
				})
			} as unknown as AgentsApi
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.ok).toBe(false);
		expect(out.error).toBe(ACTIVATE_DETAIL);
		expect(deps.newSession).not.toHaveBeenCalled();
	});
});

describe('run: newSession company_id mismatch', () => {
	it('aborts when the returned company_id differs from activeCompanyId', async () => {
		const { deps } = makeDeps({
			newSession: vi.fn(async () =>
				chatOk<NewSessionResponse>({ id: 'S1', title: 't', company_id: 'WRONG' })
			)
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.ok).toBe(false);
		expect(out.error).toBe(MSG_COMPANY_MISMATCH);
		expect(deps.send).not.toHaveBeenCalled();
	});
});

describe('run: send error-status mapping', () => {
	it('maps 400 to the returned error message', async () => {
		const { deps } = makeDeps({
			send: vi.fn(async () => chatErr<SendResponse>(400, { error: 'empty question' }))
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.error).toBe('empty question');
	});

	it('maps 502 to the LM-failed message', async () => {
		const { deps } = makeDeps({
			send: vi.fn(async () => chatErr<SendResponse>(502, {}))
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.error).toBe(MSG_LM_FAILED);
	});

	it('maps 503 to the LM-unconfigured message', async () => {
		const { deps } = makeDeps({
			send: vi.fn(async () => chatErr<SendResponse>(503, {}))
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.error).toBe(MSG_LM_UNCONFIGURED);
	});
});

describe('run: member-not-owner surface', () => {
	it('shows the member-not-owner message when activate 403s', async () => {
		const { deps } = makeDeps({
			api: {
				activateGraph: vi.fn(async () => {
					throw new ApiError('forbidden', 403, {});
				})
			} as unknown as AgentsApi
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.error).toBe(MSG_MEMBER_NOT_OWNER);
	});

	it('shows the member-not-owner message when newSession 404s', async () => {
		const { deps } = makeDeps({
			newSession: vi.fn(async () => chatErr<NewSessionResponse>(404, {} as NewSessionResponse))
		});
		const out = await run(deps, ctx(), 'q');
		expect(out.error).toBe(MSG_MEMBER_NOT_OWNER);
	});
});
