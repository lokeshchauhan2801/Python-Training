// src/lib/run.ts — NEWLY AUTHORED (not vendored).
//
// Run-from-canvas (design 4.3). A "run" reuses the EXISTING chat path: a
// pre-run save flush, an active-company guard, activate (JSON), new session
// (form), send (form). No FastAPI/Ollama, no new backend endpoint.
//
// Dependency-injected (api + chat helpers) so it is unit-testable without a DOM.

import type { AgentsApi } from './api';
import { ApiError } from './api';
import type { ChatResponse, NewSessionResponse, SendResponse } from './api';

export const MSG_COMPANY_MISMATCH = 'Active company mismatch — reload the canvas.';
export const MSG_LM_FAILED = 'The language model request failed, try again.';
export const MSG_LM_UNCONFIGURED = 'The language model is not configured.';
export const MSG_MEMBER_NOT_OWNER =
	"This company isn't available for editing — switch to a company you own.";

export interface RunDeps {
	api: AgentsApi;
	newSession: (companyId: string) => Promise<ChatResponse<NewSessionResponse>>;
	send: (sessionId: string, question: string) => Promise<ChatResponse<SendResponse>>;
	/** Flush any unsaved canvas edits; resolves on a clean save, rejects on any non-2xx. */
	saveIfDirty: () => Promise<void>;
	/** Re-load the graph to resync after a failed save. */
	reload: () => Promise<void>;
}

export interface RunContext {
	graphId: string;
	/** graph.company FK id from the load snapshot (design 4.3 step 2). */
	loadedGraphCompany: string;
	activeCompanyId: string;
}

export interface RunOutcome {
	ok: boolean;
	/** Plain-text transcript to render in the RunWindow (read-only, v1). */
	text?: string;
	/** JSON.stringify(chart_data, null, 2) if chart_data is non-null (rendered in a <pre>). */
	chartJson?: string;
	citations?: string[];
	/** Error copy to surface when ok === false. */
	error?: string;
}

/**
 * Run the graph against `prompt`. Returns a RunOutcome rather than throwing for
 * expected run failures (activate 400, send error statuses, company mismatch);
 * unexpected non-2xx on the pre-run save propagate through saveIfDirty and are
 * surfaced as an aborting error outcome.
 */
export async function run(
	deps: RunDeps,
	ctx: RunContext,
	prompt: string
): Promise<RunOutcome> {
	// Step 1: flush unsaved edits first. ABORT on any non-2xx (do NOT activate).
	try {
		await deps.saveIfDirty();
	} catch (err) {
		// saveIfDirty already re-loaded via the adapter; surface the save error.
		return { ok: false, error: runErrorMessage(err) };
	}

	// Step 2: active-company guard (precondition).
	if (ctx.loadedGraphCompany !== ctx.activeCompanyId) {
		return { ok: false, error: MSG_COMPANY_MISMATCH };
	}

	// Step 3: activate (DRF JSON, no company field). On 400 render detail VERBATIM.
	try {
		await deps.api.activateGraph(ctx.graphId);
	} catch (err) {
		if (err instanceof ApiError) {
			// member-not-owner surfaces here first (first write fails for a company
			// the user does not own).
			if (err.status === 403 || err.status === 404) {
				return { ok: false, error: MSG_MEMBER_NOT_OWNER };
			}
			if (err.status === 400) {
				return { ok: false, error: activateDetail(err.body) };
			}
		}
		return { ok: false, error: runErrorMessage(err) };
	}

	// Step 4: new session (form). Assert returned company_id === activeCompanyId.
	const sessionRes = await deps.newSession(ctx.activeCompanyId);
	if (!sessionRes.ok) {
		if (sessionRes.status === 403 || sessionRes.status === 404) {
			return { ok: false, error: MSG_MEMBER_NOT_OWNER };
		}
		return { ok: false, error: chatErrorMessage(sessionRes.status, sessionRes.body) };
	}
	if (sessionRes.body?.company_id !== ctx.activeCompanyId) {
		return { ok: false, error: MSG_COMPANY_MISMATCH };
	}
	const sessionId = sessionRes.body.session_id;

	// Step 5: send (form). Render content / chart_data / citations; map errors.
	const sendRes = await deps.send(sessionId, prompt);
	if (!sendRes.ok) {
		return { ok: false, error: chatErrorMessage(sendRes.status, sendRes.body) };
	}
	const assistant = sendRes.body?.assistant;
	const outcome: RunOutcome = { ok: true, text: assistant?.content ?? '' };
	if (assistant?.chart_data != null) {
		outcome.chartJson = JSON.stringify(assistant.chart_data, null, 2);
	}
	if (Array.isArray(assistant?.citations) && assistant.citations.length > 0) {
		outcome.citations = assistant.citations;
	}
	return outcome;
}

/** Render activate's 400 `detail` VERBATIM (design 4.3 step 3). */
function activateDetail(body: unknown): string {
	if (body && typeof body === 'object' && 'detail' in (body as Record<string, unknown>)) {
		return String((body as Record<string, unknown>).detail);
	}
	return runErrorMessage(new ApiError('activate failed', 400, body));
}

/** Map a chat endpoint's error status to the pinned copy (design 4.3 step 5). */
function chatErrorMessage(status: number, body: unknown): string {
	if (status === 400) {
		if (body && typeof body === 'object' && 'error' in (body as Record<string, unknown>)) {
			return String((body as Record<string, unknown>).error);
		}
		return 'The request was rejected.';
	}
	if (status === 502) return MSG_LM_FAILED;
	if (status === 503) return MSG_LM_UNCONFIGURED;
	return `The run failed (${status}).`;
}

function runErrorMessage(err: unknown): string {
	if (err instanceof ApiError) {
		if (err.status === 502) return MSG_LM_FAILED;
		if (err.status === 503) return MSG_LM_UNCONFIGURED;
		return err.message;
	}
	if (err instanceof Error) return err.message;
	return String(err);
}
