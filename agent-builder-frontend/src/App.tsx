import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useEdgesState,
  useNodesState,
  Node as FlowNode,
  Edge as FlowEdge,
  MarkerType,
  Position,
  ReactFlowInstance,
} from "reactflow";
import {
  api,
  Agent,
  AvailableFunction,
  FlowStepSummary,
  Step,
  Tool,
  companyId,
  companyName,
} from "./api";
// The backend is a two-level Agent -> Steps model. An Agent is a SUPERVISOR and
// its Steps are its WORKERS: the canvas draws START -> Supervisor -> every
// worker -> END. The dropdown lists AGENTS; the wiring is implied (a worker is
// connected to its supervisor just by belonging to the agent).

const START_ID = "__start__";
const END_ID = "__end__";
const SUPERVISOR_ID = "__supervisor__";
const WORKER_GAP_Y = 170;

interface StepNodeData {
  raw: Step;
  label: React.ReactNode;
}

interface AnchorNodeData {
  label: React.ReactNode;
}

type CanvasNode = FlowNode<StepNodeData | AnchorNodeData>;

type AgentFlows = Record<string, FlowStepSummary[]>;

function toStepNode(
  step: Step,
  toolNameById: Record<string, string>,
  agentNameById: Record<string, string>,
  agentFlows: AgentFlows
): CanvasNode {
  const toolNames = (step.tools || [])
    .map((id) => toolNameById[String(id)])
    .filter(Boolean);
  // A hand-off worker gives its part to another agent instead of its tools.
  const delegateId = step.delegate_agent ? String(step.delegate_agent) : "";
  const delegateName = delegateId ? agentNameById[delegateId] || "another agent" : "";
  const subSteps = delegateId ? agentFlows[delegateId] || [] : [];

  const content = (
    <div>
      <div className="ab-node-kind">{delegateName ? "Worker · hand-off" : "Worker"}</div>
      <div className="ab-node-label">{step.name || step.slug}</div>
      {delegateName ? (
        <div className="ab-node-tools">
          <div className="ab-node-handoff">→ {delegateName}</div>
          {subSteps.length
            ? subSteps
                .map((s) => (s.tools.length ? `${s.name}: ${s.tools.join(", ")}` : `${s.name}: answers itself`))
                .join(" · ")
            : "that agent has no workers"}
        </div>
      ) : (
        <div className="ab-node-tools">
          {toolNames.length ? toolNames.join(", ") : "No tools: answers itself with the LLM"}
        </div>
      )}
    </div>
  );

  return {
    id: String(step.id),
    position: { x: step.position_x ?? 0, y: step.position_y ?? 0 },
    data: { raw: step, label: content },
    type: "default",
    // Wires come in from the supervisor on the left, go out to END on the right.
    targetPosition: Position.Left,
    sourcePosition: Position.Right,
    className: "ab-node ab-node-step" + (delegateName ? " ab-node-handoff-step" : ""),
  };
}

function supervisorNode(agent: Agent | undefined, workers: number, x: number, y: number): CanvasNode {
  return {
    id: SUPERVISOR_ID,
    position: { x, y },
    data: {
      label: (
        <div>
          <div className="ab-node-kind">Supervisor</div>
          <div className="ab-node-label">{agent ? agent.name || agent.slug : "Agent"}</div>
          <div className="ab-node-tools">
            Splits each message and sends every part to the right worker
            {workers ? ` (${workers})` : ""}
          </div>
        </div>
      ),
    },
    type: "default",
    targetPosition: Position.Left,
    sourcePosition: Position.Right,
    className: "ab-node ab-node-supervisor",
  };
}

function anchorNode(id: string, label: string, x: number, y: number, cls: string): CanvasNode {
  // START only has an outgoing handle, END only an incoming one.
  return {
    id,
    position: { x, y },
    data: {
      label: (
        <div>
          <div className="ab-node-label">{label}</div>
        </div>
      ),
    },
    type: id === START_ID ? "input" : "output",
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    className: `ab-node ${cls}`,
  };
}

// START / Supervisor / END are canvas-only nodes (not backend rows), so where
// the user dragged them is remembered per agent in this browser.
type AnchorPositions = Record<string, { x: number; y: number }>;

function anchorKey(agentId: string | null): string {
  return `ab-anchors:${agentId || ""}`;
}

function loadAnchors(agentId: string | null): AnchorPositions {
  try {
    return JSON.parse(localStorage.getItem(anchorKey(agentId)) || "{}");
  } catch {
    return {};
  }
}

function saveAnchor(agentId: string | null, id: string, x: number, y: number) {
  try {
    const all = loadAnchors(agentId);
    all[id] = { x, y };
    localStorage.setItem(anchorKey(agentId), JSON.stringify(all));
  } catch {
    // Storage unavailable: the anchor goes back to its default spot.
  }
}

/** The agent's workers, in the order the backend lists them. */
function orderWorkers(steps: Step[]): Step[] {
  return [...steps].sort(
    (a, b) => a.order - b.order || a.created_at.localeCompare(b.created_at)
  );
}

function isAnchor(id: string): boolean {
  return id === START_ID || id === END_ID || id === SUPERVISOR_ID;
}

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [agentFlows, setAgentFlows] = useState<AgentFlows>({});
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<StepNodeData | AnchorNodeData>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [error, setError] = useState<string>("");
  const [successMessage, setSuccessMessage] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [busy, setBusy] = useState<boolean>(false);
  const [showAddAgent, setShowAddAgent] = useState<boolean>(false);
  const [showAddStep, setShowAddStep] = useState<boolean>(false);
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null);
  // Position PATCHes still in flight, keyed by step id (see ``refresh``).
  const posPending = React.useRef<Record<string, Promise<unknown>>>({});
  // Fit the view once the NEXT node set has rendered (fitting right after a
  // refresh would measure the old positions and leave nodes off-screen).
  const fitPending = React.useRef(false);
  const requestFit = useCallback(() => {
    fitPending.current = true;
  }, []);

  const company = companyId();

  const showError = useCallback((e: any) => {
    const msg = e && e.message ? e.message : String(e);
    setError(msg);
    setTimeout(() => setError(""), 5000);
  }, []);

  const showSuccess = useCallback((msg: string) => {
    setSuccessMessage(msg);
    setTimeout(() => setSuccessMessage(""), 3000);
  }, []);

  // Rebuild the React Flow canvas (step nodes + synthetic START/END anchors +
  // derived edges) from the selected agent's steps.
  const renderFlow = useCallback(
    (
      flowSteps: Step[],
      flowTools: Tool[],
      flowAgents: Agent[],
      agentId: string | null,
      flows: AgentFlows
    ) => {
      const names: Record<string, string> = {};
      flowTools.forEach((t) => {
        names[String(t.id)] = t.name || t.slug;
      });
      const agentNames: Record<string, string> = {};
      flowAgents.forEach((a) => {
        agentNames[String(a.id)] = a.name || a.slug;
      });

      const workers = orderWorkers(flowSteps);
      const stepNodes = workers.map((s) => toStepNode(s, names, agentNames, flows));

      // START -> Supervisor sit left of the workers' column and END right of
      // it, centred on the column, unless the user dragged them somewhere.
      const xs = workers.map((s) => s.position_x ?? 0);
      const ys = workers.map((s) => s.position_y ?? 0);
      const left = xs.length ? Math.min(...xs) : 0;
      const right = xs.length ? Math.max(...xs) : 0;
      const midY = ys.length ? (Math.min(...ys) + Math.max(...ys)) / 2 : 0;
      const saved = loadAnchors(agentId);
      const sup = saved[SUPERVISOR_ID] || { x: left - 320, y: midY };
      const start = saved[START_ID] || { x: sup.x - 260, y: sup.y };
      const end = saved[END_ID] || { x: right + 340, y: midY };
      const agent = flowAgents.find((a) => String(a.id) === agentId);

      const canvasNodes: CanvasNode[] = [
        anchorNode(START_ID, "START", start.x, start.y, "ab-node-start"),
        supervisorNode(agent, workers.length, sup.x, sup.y),
        ...stepNodes,
        anchorNode(END_ID, "END", end.x, end.y, "ab-node-end"),
      ];

      // The wiring is implied: START -> Supervisor -> every worker -> END
      // (each worker's answer goes into the reply).
      const wire = (source: string, target: string, cls = "ab-edge-live"): FlowEdge => ({
        id: `${source}->${target}`,
        source,
        target,
        type: "default",
        markerEnd: { type: MarkerType.ArrowClosed },
        className: cls,
        deletable: false,
        focusable: false,
      });
      const canvasEdges: FlowEdge[] = [wire(START_ID, SUPERVISOR_ID)];
      workers.forEach((s) => {
        canvasEdges.push(wire(SUPERVISOR_ID, String(s.id)));
        canvasEdges.push(wire(String(s.id), END_ID, "ab-edge-answer"));
      });
      if (!workers.length) canvasEdges.push(wire(SUPERVISOR_ID, END_ID, "ab-edge-idle"));

      // Always apply the incoming nodes with their SAVED positions so a
      // re-render (onConnect, tool toggle, reselect) does not snap a node back.
      setNodes(canvasNodes.map((n) => ({ ...n, position: { ...n.position } })));
      setEdges(canvasEdges);
    },
    [setNodes, setEdges]
  );

  // Fetch the full bootstrap and render the selected agent's steps.
  const refresh = useCallback(
    async (agentId?: string | null) => {
      // Let in-flight position saves land first, or the reload would put a
      // just-dragged node back where it was.
      await Promise.all(Object.values(posPending.current));
      const payload = await api.loadFlow(company, agentId || undefined);
      const allAgents = payload.agents || [];
      const flowTools = payload.tools || [];
      const flowSteps = payload.steps || [];
      setAgents(allAgents);
      setTools(flowTools);
      const flows = payload.agent_flows || {};
      setSteps(flowSteps);
      setAgentFlows(flows);
      setSelectedAgentId(payload.selected_agent_id);
      renderFlow(flowSteps, flowTools, allAgents, payload.selected_agent_id, flows);
    },
    [company, renderFlow]
  );

  useEffect(() => {
    (async () => {
      try {
        setLoading(true);
        await refresh();
      } catch (e) {
        showError(e);
      } finally {
        setLoading(false);
      }
    })();
    // Intentionally run once on mount; refresh is stable via useCallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedAgent = useMemo(
    () => agents.find((a) => String(a.id) === selectedAgentId) || null,
    [agents, selectedAgentId]
  );

  const workers = useMemo(() => orderWorkers(steps), [steps]);

  const selectedStep = useMemo(() => {
    if (!selectedStepId) return null;
    return steps.find((s) => String(s.id) === selectedStepId) || null;
  }, [selectedStepId, steps]);

  // Create a new empty Agent container, then select it.
  const createAgent = useCallback(
    async (values: { name: string; slug: string; description: string }) => {
      if (busy) return;
      setBusy(true);
      try {
        const created = await api.createAgent({
          slug: values.slug,
          name: values.name,
          description: values.description,
          company: company || undefined,
        });
        setShowAddAgent(false);
        setSelectedStepId(null);
        if (created) await refresh(String(created.id));
        showSuccess("Agent created");
      } catch (e) {
        showError(e);
      } finally {
        setBusy(false);
      }
    },
    [busy, company, refresh, showError, showSuccess]
  );

  // Create a Step under the currently selected agent.
  const createStep = useCallback(
    async (values: {
      name: string;
      slug: string;
      tools: string[];
      delegate_agent: string | null;
    }) => {
      if (busy || !selectedAgentId) return;
      setBusy(true);
      try {
        // A new worker joins the bottom of the workers' column.
        const last = workers[workers.length - 1];
        const position = last
          ? { x: last.position_x ?? 0, y: Math.max(...workers.map((s) => s.position_y ?? 0)) + WORKER_GAP_Y }
          : { x: 0, y: 0 };
        await api.createStep({
          agent: selectedAgentId,
          slug: values.slug,
          name: values.name,
          tools: values.tools,
          delegate_agent: values.delegate_agent,
          position_x: position.x,
          position_y: position.y,
          company: company || undefined,
        });
        setShowAddStep(false);
        await refresh(selectedAgentId);
        requestFit();
        showSuccess("Worker added to the supervisor");
      } catch (e) {
        showError(e);
      } finally {
        setBusy(false);
      }
    },
    [busy, company, refresh, selectedAgentId, showError, showSuccess, workers]
  );

  // Debounced position save: while a STEP node is dragged we coalesce rapid
  // moves and PATCH its position ~1s after the last movement. flushPosition
  // updates the LOCAL steps state immediately so a subsequent renderFlow()
  // keeps the dragged position (the position-reset fix).
  const posTimers = React.useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const posLatest = React.useRef<Record<string, { x: number; y: number }>>({});
  const POSITION_DEBOUNCE_MS = 1000;

  const flushPosition = useCallback(
    async (id: string) => {
      const pos = posLatest.current[id];
      if (!pos) return;
      delete posLatest.current[id];
      setSteps((prev) =>
        prev.map((s) =>
          String(s.id) === id
            ? { ...s, position_x: pos.x, position_y: pos.y }
            : s
        )
      );
      const save = api
        .updateStep(id, { position_x: pos.x, position_y: pos.y })
        .catch((e) => showError(e));
      posPending.current[id] = save;
      try {
        await save;
      } finally {
        if (posPending.current[id] === save) delete posPending.current[id];
      }
    },
    [showError]
  );

  const schedulePosition = useCallback(
    (node: CanvasNode) => {
      if (isAnchor(node.id)) return;
      const id = node.id;
      posLatest.current[id] = { x: node.position.x, y: node.position.y };
      if (posTimers.current[id]) clearTimeout(posTimers.current[id]);
      posTimers.current[id] = setTimeout(() => {
        delete posTimers.current[id];
        void flushPosition(id);
      }, POSITION_DEBOUNCE_MS);
    },
    [flushPosition]
  );

  // On drag stop, flush immediately (cancel the pending debounce).
  const persistPosition = useCallback(
    (node: CanvasNode) => {
      if (isAnchor(node.id)) {
        saveAnchor(selectedAgentId, node.id, node.position.x, node.position.y);
        return;
      }
      const id = node.id;
      if (posTimers.current[id]) {
        clearTimeout(posTimers.current[id]);
        delete posTimers.current[id];
      }
      // A plain click also ends a "drag": nothing moved, so nothing to save.
      const saved = (node.data as StepNodeData).raw;
      if (
        !posLatest.current[id] &&
        saved &&
        saved.position_x === node.position.x &&
        saved.position_y === node.position.y
      ) {
        return;
      }
      posLatest.current[id] = { x: node.position.x, y: node.position.y };
      void flushPosition(id);
    },
    [flushPosition, selectedAgentId]
  );

  useEffect(() => {
    if (!fitPending.current || !rfInstance || nodes.length === 0) return;
    fitPending.current = false;
    // Two frames: React Flow measures the new nodes, then we fit to them.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => rfInstance.fitView({ padding: 0.3, duration: 300, maxZoom: 1 }))
    );
  }, [nodes, rfInstance]);

  // Flush any pending position timers on unmount.
  useEffect(() => {
    const timers = posTimers.current;
    return () => {
      Object.values(timers).forEach((t) => clearTimeout(t));
    };
  }, []);

  const onNodeClick = useCallback((_e: React.MouseEvent, node: CanvasNode) => {
    if (isAnchor(node.id)) {
      setSelectedStepId(null);
      return;
    }
    setSelectedStepId(node.id);
  }, []);

  // Sidebar actions -------------------------------------------------------

  const renameStep = useCallback(
    async (step: Step, name: string) => {
      if (name === step.name) return;
      try {
        await api.updateStep(String(step.id), { name });
        await refresh(selectedAgentId);
      } catch (e) {
        showError(e);
      }
    },
    [refresh, selectedAgentId, showError]
  );

  // Rename an agent (the selected one, or a sub-agent from the step panel).
  // The slug stays as it is, so chat sessions keep their selection.
  const renameAgent = useCallback(
    async (agentId: string, name: string) => {
      const current = agents.find((a) => String(a.id) === agentId);
      const clean = name.trim();
      if (!clean) {
        showError(new Error("An agent name can't be empty."));
        return false;
      }
      if (current && clean === (current.name || "")) return true;
      try {
        await api.updateAgent(agentId, { name: clean });
        await refresh(selectedAgentId);
        showSuccess("Agent renamed");
        return true;
      } catch (e) {
        showError(e);
        return false;
      }
    },
    [agents, refresh, selectedAgentId, showError, showSuccess]
  );

  const saveAgentDescription = useCallback(
    async (agentId: string, description: string) => {
      try {
        await api.updateAgent(agentId, { description: description.trim() });
        await refresh(selectedAgentId);
        showSuccess("Description saved");
        return true;
      } catch (e) {
        showError(e);
        return false;
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
  );

  // Hand this step's work to another agent (sub-agent), or back to its tools.
  const setStepDelegate = useCallback(
    async (step: Step, agentId: string | null) => {
      if ((step.delegate_agent || null) === agentId) return;
      try {
        await api.updateStep(String(step.id), { delegate_agent: agentId });
        await refresh(selectedAgentId);
        showSuccess(agentId ? "Worker now hands off to another agent" : "Worker uses its own tools");
      } catch (e) {
        showError(e);
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
  );

  const toggleStepTool = useCallback(
    async (step: Step, toolId: string, checked: boolean) => {
      const next = new Set((step.tools || []).map(String));
      if (checked) next.add(toolId);
      else next.delete(toolId);
      try {
        await api.updateStep(String(step.id), { tools: Array.from(next) });
        await refresh(selectedAgentId);
        showSuccess("Tools updated");
      } catch (e) {
        showError(e);
        await refresh(selectedAgentId);
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
  );

  const deleteStep = useCallback(
    async (step: Step) => {
      if (!window.confirm(`Delete worker "${step.name || step.slug}"?`)) {
        return;
      }
      try {
        await api.deleteStep(String(step.id));
        setSelectedStepId(null);
        await refresh(selectedAgentId);
        showSuccess("Worker deleted");
      } catch (e) {
        showError(e);
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
  );

  // Lay the workers out in one column (supervisor and START/END follow).
  const tidyLayout = useCallback(async () => {
    if (!selectedAgentId) return;
    const moves = workers.map((s, i) => ({ s, x: 0, y: i * WORKER_GAP_Y }));
    try {
      localStorage.removeItem(anchorKey(selectedAgentId));
    } catch {
      // Nothing saved for the anchors; they go to their default spot anyway.
    }
    try {
      await Promise.all(
        moves.map(({ s, x, y }) =>
          api.updateStep(String(s.id), { position_x: x, position_y: y })
        )
      );
      requestFit();
      await refresh(selectedAgentId);
    } catch (e) {
      showError(e);
    }
  }, [refresh, requestFit, selectedAgentId, showError, workers]);

  // Attach a tool to the selected STEP from a registered handler function.
  const attachFunction = useCallback(
    async (step: Step, slug: string) => {
      try {
        await api.attachFunction({
          slug,
          step: String(step.id),
          company: company || undefined,
        });
        await refresh(selectedAgentId);
        showSuccess(`Added "${slug}"`);
      } catch (e) {
        showError(e);
      }
    },
    [company, refresh, selectedAgentId, showError, showSuccess]
  );

  // Selecting an agent REPLACES the whole graph with ONLY that agent's steps.
  const onSelectAgent = useCallback(
    async (id: string) => {
      if (!id) return;
      setSelectedStepId(null);
      try {
        requestFit();
        await refresh(id);
      } catch (e) {
        showError(e);
      }
    },
    [refresh, requestFit, showError]
  );

  return (
    <div className="ab-shell">
      <div className="ab-canvas">
        <div className="ab-toolbar">
          <button
            className="ab-btn"
            onClick={() => setShowAddAgent(true)}
            disabled={busy}
          >
            New agent
          </button>
          <button
            className="ab-btn"
            onClick={() => setShowAddStep(true)}
            disabled={busy || !selectedAgentId}
            title={selectedAgentId ? "Add a worker to this agent's supervisor" : "Select an agent first"}
          >
            + Worker
          </button>
          <button
            className="ab-btn"
            onClick={tidyLayout}
            disabled={busy || steps.length === 0}
            title="Arrange the workers in one column next to the supervisor"
          >
            Tidy layout
          </button>
          {agents.length > 0 && (
            <select
              className="ab-select"
              style={{ width: "auto" }}
              value={selectedAgentId || ""}
              onChange={(e) => onSelectAgent(e.target.value)}
              title="Select an agent to view its flow"
            >
              {agents.map((a) => (
                <option key={String(a.id)} value={String(a.id)}>
                  {a.name || a.slug}
                </option>
              ))}
            </select>
          )}
        </div>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodesConnectable={false}
          edgesFocusable={false}
          onNodeDrag={(_e, node) => schedulePosition(node as CanvasNode)}
          onNodeDragStop={(_e, node) => persistPosition(node as CanvasNode)}
          onNodeClick={onNodeClick}
          onPaneClick={() => setSelectedStepId(null)}
          onInit={setRfInstance}
          proOptions={{ hideAttribution: true }}
          fitView
          fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
        >
          <Background />
          <Controls />
          <MiniMap
            pannable
            zoomable
            className="ab-minimap"
            nodeColor="var(--ab-muted)"
            maskColor="rgba(0, 0, 0, 0.35)"
          />
        </ReactFlow>
      </div>

      <aside className="ab-sidebar">
        {successMessage && (
          <div className="ab-banner ab-banner-success" role="status">
            ✓ {successMessage}
          </div>
        )}
        {error && (
          <div className="ab-banner ab-banner-error" role="alert">
            ⚠ {error}
          </div>
        )}
        {loading && (
          <div className="ab-empty" style={{ textAlign: "center", padding: "32px 0" }}>
            <div className="ab-spinner" role="status" aria-label="Loading flow"></div>
            <div style={{ marginTop: "12px" }}>Loading flow…</div>
          </div>
        )}

        <div className="ab-banner ab-banner-info">
          <div className="ab-node-kind">{companyName() || "Agent flow"}</div>
          {selectedAgent ? (
            <InlineName
              key={`agent-name-${selectedAgent.id}`}
              value={selectedAgent.name || selectedAgent.slug}
              label="Agent name"
              onSave={(name) => renameAgent(String(selectedAgent.id), name)}
            />
          ) : (
            <strong>No agent selected</strong>
          )}
          {selectedAgent && (
            <DescriptionEditor
              key={`agent-about-${selectedAgent.id}-${selectedAgent.description}`}
              value={selectedAgent.description || ""}
              onSave={(text) => saveAgentDescription(String(selectedAgent.id), text)}
            />
          )}
        </div>

        {!selectedStep && (
          <FlowOverview
            workers={workers}
            tools={tools}
            agentNameById={Object.fromEntries(agents.map((a) => [String(a.id), a.name || a.slug]))}
            onSelect={(id) => setSelectedStepId(id)}
          />
        )}
        {selectedStep && (
          <StepEditor
            key={selectedStep.id}
            step={selectedStep}
            tools={tools}
            agents={agents.filter((a) => String(a.id) !== selectedAgentId)}
            agentFlows={agentFlows}
            company={company}
            onOpenAgent={(id) => onSelectAgent(id)}
            onRenameAgent={renameAgent}
            onClose={() => setSelectedStepId(null)}
            onRename={(name) => renameStep(selectedStep, name)}
            onSetDelegate={(agentId) => setStepDelegate(selectedStep, agentId)}
            workerCount={workers.length}
            onToggleTool={(id, checked) => toggleStepTool(selectedStep, id, checked)}
            onAttachFunction={(slug) => attachFunction(selectedStep, slug)}
            onDelete={() => deleteStep(selectedStep)}
          />
        )}
      </aside>

      {showAddAgent && (
        <CreateAgentDialog
          busy={busy}
          existingSlugs={agents.map((a) => a.slug)}
          onCancel={() => setShowAddAgent(false)}
          onCreate={createAgent}
        />
      )}

      {showAddStep && selectedAgentId && (
        <CreateStepDialog
          tools={tools}
          agents={agents.filter((a) => String(a.id) !== selectedAgentId)}
          busy={busy}
          existingSlugs={steps.map((s) => s.slug)}
          onCancel={() => setShowAddStep(false)}
          onCreate={createStep}
        />
      )}
    </div>
  );
}

interface DescriptionEditorProps {
  value: string;
  onSave: (text: string) => Promise<boolean>;
}

/** "What this agent does": shown as text with Edit; the supervisor routes by
 * it and a no-tools worker answers with it as its instructions. */
function DescriptionEditor({ value, onSave }: DescriptionEditorProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  if (!editing) {
    return (
      <div className="ab-about">
        <span className={value ? "" : "ab-about-empty"}>
          {value || "No description yet. Add one so the supervisor knows what this agent is for."}
        </span>
        <button
          className="ab-link"
          aria-label="Edit agent description"
          onClick={() => {
            setDraft(value);
            setEditing(true);
          }}
        >
          ✎ {value ? "Edit" : "Add"}
        </button>
      </div>
    );
  }
  return (
    <div className="ab-inline-name-edit">
      <textarea
        className="ab-input"
        aria-label="Agent description"
        rows={3}
        value={draft}
        autoFocus
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setEditing(false);
        }}
      />
      <div className="ab-inline-name-actions">
        <button
          className="ab-btn ab-btn-primary"
          disabled={saving}
          onClick={async () => {
            setSaving(true);
            const ok = await onSave(draft);
            setSaving(false);
            if (ok) setEditing(false);
          }}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="ab-btn" onClick={() => setEditing(false)} disabled={saving}>
          Cancel
        </button>
      </div>
    </div>
  );
}

interface InlineNameProps {
  value: string;
  label: string;
  onSave: (name: string) => Promise<boolean>;
}

/** A name shown as text with a Rename button; edits in place (Enter saves,
 * Esc cancels). */
function InlineName({ value, label, onSave }: InlineNameProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  const start = () => {
    setDraft(value);
    setEditing(true);
  };
  const save = async () => {
    setSaving(true);
    const ok = await onSave(draft);
    setSaving(false);
    if (ok) setEditing(false);
  };

  if (!editing) {
    return (
      <div className="ab-inline-name">
        <strong title={label}>{value}</strong>
        <button className="ab-link" onClick={start} aria-label={`Rename ${label.toLowerCase()}`}>
          ✎ Rename
        </button>
      </div>
    );
  }
  return (
    <div className="ab-inline-name-edit">
      <input
        className="ab-input"
        aria-label={label}
        value={draft}
        autoFocus
        maxLength={120}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void save();
          if (e.key === "Escape") setEditing(false);
        }}
      />
      <div className="ab-inline-name-actions">
        <button className="ab-btn ab-btn-primary" onClick={save} disabled={saving || !draft.trim()}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="ab-btn" onClick={() => setEditing(false)} disabled={saving}>
          Cancel
        </button>
      </div>
    </div>
  );
}

interface FlowOverviewProps {
  workers: Step[];
  tools: Tool[];
  agentNameById: Record<string, string>;
  onSelect: (stepId: string) => void;
}

/** Sidebar summary when no worker is selected: who the supervisor can send
 * work to, and how a message is handled. */
function FlowOverview({ workers, tools, agentNameById, onSelect }: FlowOverviewProps) {
  const toolName = (id: string) => {
    const tool = tools.find((t) => String(t.id) === id);
    return tool ? tool.name || tool.slug : "";
  };
  const describe = (s: Step) => {
    if (s.delegate_agent) {
      return `hands its parts to ${agentNameById[String(s.delegate_agent)] || "another agent"}`;
    }
    const names = (s.tools || []).map((id) => toolName(String(id))).filter(Boolean);
    return names.length ? names.join(", ") : "no tools: answers itself with the LLM";
  };

  return (
    <div>
      <div className="ab-section-title">The supervisor can send work to</div>
      {workers.length === 0 ? (
        <div className="ab-empty">No workers yet. Click "+ Worker" to add the first one.</div>
      ) : (
        <div className="ab-flow-list">
          {workers.map((s) => (
            <button key={`flow-${s.id}`} className="ab-flow-item" onClick={() => onSelect(String(s.id))}>
              <span>
                <strong>{s.name || s.slug}</strong>
                <span className="ab-flow-meta">{describe(s)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
      <div className="ab-help">
        <div>How a chat message is handled:</div>
        <div>1. The supervisor reads it and splits it into parts ("GDP figures", "summarise the chat", "pie chart"…).</div>
        <div>2. Each part goes to the worker that fits it best, by its tools and the agent's description.</div>
        <div>3. Parts that don't need each other run at the same time. A part that needs another's answer (a chart of the GDP figures) waits for it and gets only that answer.</div>
        <div>4. Every worker's answer goes into the reply, in the order you asked. A worker with no tools answers itself with the LLM, in its role.</div>
      </div>
    </div>
  );
}

interface StepEditorProps {
  step: Step;
  tools: Tool[];
  agents: Agent[];
  agentFlows: AgentFlows;
  company: string;
  workerCount: number;
  onRename: (name: string) => void;
  onSetDelegate: (agentId: string | null) => void;
  onOpenAgent: (agentId: string) => void;
  onRenameAgent: (agentId: string, name: string) => Promise<boolean>;
  onClose: () => void;
  onToggleTool: (toolId: string, checked: boolean) => void;
  onAttachFunction: (slug: string) => void;
  onDelete: () => void;
}

function StepEditor({
  step,
  tools,
  agents,
  agentFlows,
  company,
  workerCount,
  onRename,
  onSetDelegate,
  onOpenAgent,
  onRenameAgent,
  onClose,
  onToggleTool,
  onAttachFunction,
  onDelete,
}: StepEditorProps) {
  const [name, setName] = useState(step.name || "");
  const [functions, setFunctions] = useState<AvailableFunction[]>([]);
  // "agent" while the user is choosing one, even before it is saved.
  const [mode, setMode] = useState<"tools" | "agent">(step.delegate_agent ? "agent" : "tools");
  const stepToolIds = new Set((step.tools || []).map(String));
  const delegateId = step.delegate_agent ? String(step.delegate_agent) : "";
  const delegate = agents.find((a) => String(a.id) === delegateId);
  const delegateFlow = delegateId ? agentFlows[delegateId] || [] : [];
  // An agent whose flow already hands off (directly or not) to THIS step's
  // agent would loop back, so it is shown but cannot be picked.
  const loopsBack = useMemo(() => {
    const reaches = (agentId: string, seen: Set<string>): boolean => {
      if (agentId === String(step.agent)) return true;
      if (seen.has(agentId)) return false;
      seen.add(agentId);
      return (agentFlows[agentId] || []).some(
        (s) => !!s.delegate_agent && reaches(String(s.delegate_agent), seen)
      );
    };
    return new Set(agents.filter((a) => reaches(String(a.id), new Set())).map((a) => String(a.id)));
  }, [agentFlows, agents, step.agent]);

  // Load the registered handler functions once per selected step so the
  // tool list can offer the ones without a Tool row yet.
  useEffect(() => {
    let cancelled = false;
    api
      .availableFunctions(company || undefined)
      .then((fns) => {
        if (!cancelled) setFunctions(fns);
      })
      .catch(() => {
        if (!cancelled) setFunctions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [company, step.id, step.tools]);

  // One list: every handler function (ticking one that has no Tool row yet
  // creates it) plus any prompt-only (langfuse) tools of the company.
  const toolOptions = useMemo(() => {
    const options = functions.map((fn) => {
      const row = tools.find((t) => t.slug === fn.slug);
      return {
        key: fn.slug,
        name: row?.name || fn.name,
        description: fn.description,
        toolId: row ? String(row.id) : fn.tool_id ? String(fn.tool_id) : null,
      };
    });
    const known = new Set(options.map((o) => o.key));
    tools
      .filter((t) => !known.has(t.slug))
      .forEach((t) =>
        options.push({
          key: t.slug,
          name: t.name || t.slug,
          description: t.description,
          toolId: String(t.id),
        })
      );
    return options;
  }, [functions, tools]);

  const chooseMode = (next: "tools" | "agent") => {
    setMode(next);
    if (next === "tools" && delegateId) onSetDelegate(null);
  };

  const position = `One of ${workerCount} worker${workerCount === 1 ? "" : "s"}: the supervisor sends it the parts of a message it fits.`;

  return (
    <div>
      <div className="ab-editor-head">
        <div className="ab-section-title" style={{ margin: 0 }}>Edit worker</div>
        <button className="ab-link" onClick={onClose} title="Back to the flow overview">
          ← Flow overview
        </button>
      </div>

      <label className="ab-field">
        <span>Name</span>
        <input
          className="ab-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => onRename(name)}
        />
      </label>
      <div className="ab-position">
        {position}
      </div>

      <div className="ab-section-title">Who does this worker's parts?</div>
      <div className="ab-mode">
        <button
          className={mode === "tools" ? "ab-mode-card active" : "ab-mode-card"}
          onClick={() => chooseMode("tools")}
        >
          <strong>Its own tools</strong>
          <span>Answers with the tools you tick below.</span>
        </button>
        <button
          className={mode === "agent" ? "ab-mode-card active" : "ab-mode-card"}
          onClick={() => chooseMode("agent")}
          disabled={agents.length === 0}
          title={agents.length === 0 ? 'Create another agent with "Add Agent" first' : ""}
        >
          <strong>Another agent</strong>
          <span>Hands its parts to another agent (and its workers).</span>
        </button>
      </div>
      {agents.length === 0 && (
        <div className="ab-empty" style={{ fontSize: "12px" }}>
          To hand off to another agent, first create one with "New agent".
        </div>
      )}

      {mode === "agent" ? (
        <div>
          <label className="ab-field">
            <span>Agent</span>
            <select
              className="ab-input"
              value={delegateId}
              onChange={(e) => onSetDelegate(e.target.value || null)}
            >
              <option value="" disabled>
                Choose an agent…
              </option>
              {agents.map((a) => (
                <option
                  key={`delegate-${a.id}`}
                  value={String(a.id)}
                  disabled={loopsBack.has(String(a.id))}
                >
                  {a.name || a.slug}
                  {loopsBack.has(String(a.id)) ? " (hands off back to this agent)" : ""}
                </option>
              ))}
            </select>
          </label>
          {delegate && (
            <div className="ab-preview">
              <InlineName
                key={`sub-agent-name-${delegate.id}`}
                value={delegate.name || delegate.slug}
                label="Sub-agent name"
                onSave={(name) => onRenameAgent(String(delegate.id), name)}
              />
              {delegate.description && <div className="ab-about">{delegate.description}</div>}
              <div className="ab-preview-title">Its workers</div>
              {delegateFlow.length === 0 ? (
                <div className="ab-position-warn">
                  This agent has no runnable flow, so this step falls back to its own tools.
                </div>
              ) : (
                <ol className="ab-flow-list">
                  {delegateFlow.map((s, i) => (
                    <li key={`preview-${s.id}`} className="ab-flow-meta-row">
                      <span className="ab-flow-num">{i + 1}</span>
                      <span>
                        <strong>{s.name}</strong>
                        <span className="ab-flow-meta">
                          {s.delegate_agent
                            ? "hands off to another agent"
                            : s.tools.join(", ") || "no tools: answers itself with the LLM"}
                        </span>
                      </span>
                    </li>
                  ))}
                </ol>
              )}
              <div className="ab-empty" style={{ fontSize: "12px" }}>
                When a part needs another part's answer (e.g. a chart of
                figures found first), it gets that answer.
              </div>
              <button className="ab-link" onClick={() => onOpenAgent(delegateId)}>
                Open {delegate.name || delegate.slug} to edit it →
              </button>
            </div>
          )}
        </div>
      ) : (
        <div>
          {toolOptions.length === 0 && <div className="ab-empty">No tools available.</div>}
          {toolOptions.map((opt) => (
            <label className="ab-tool-row" key={`step-${step.id}-tool-${opt.key}`} title={opt.description}>
              <input
                type="checkbox"
                checked={!!opt.toolId && stepToolIds.has(opt.toolId)}
                onChange={(e) =>
                  opt.toolId
                    ? onToggleTool(opt.toolId, e.target.checked)
                    : e.target.checked && onAttachFunction(opt.key)
                }
              />
              <span>
                {opt.name}
                {opt.description && <span className="ab-flow-meta">{opt.description}</span>}
              </span>
            </label>
          ))}
          {stepToolIds.size === 0 && (
            <div className="ab-empty" style={{ fontSize: "12px" }}>
              No tools ticked: this worker answers itself with the LLM, in the
              role of this agent (its name and description).
            </div>
          )}
        </div>
      )}

      <button className="ab-btn ab-btn-danger" onClick={onDelete} title="Delete this worker">
        Delete worker
      </button>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

interface CreateAgentDialogProps {
  busy: boolean;
  existingSlugs: string[];
  onCancel: () => void;
  onCreate: (values: { name: string; slug: string; description: string }) => void;
}

function CreateAgentDialog({
  busy,
  existingSlugs,
  onCancel,
  onCreate,
}: CreateAgentDialogProps) {
  const [name, setName] = useState<string>("New agent");
  const [slug, setSlug] = useState<string>(`agent-${Date.now().toString(36)}`);
  const [slugEdited, setSlugEdited] = useState<boolean>(false);
  const [description, setDescription] = useState<string>("");
  const [localError, setLocalError] = useState<string>("");

  const onNameChange = (value: string) => {
    setName(value);
    if (!slugEdited) {
      const derived = slugify(value);
      setSlug(derived || `agent-${Date.now().toString(36)}`);
    }
  };

  const submit = () => {
    const finalSlug = slug.trim();
    if (!name.trim()) {
      setLocalError("Name is required.");
      return;
    }
    if (!finalSlug) {
      setLocalError("Slug is required.");
      return;
    }
    if (existingSlugs.includes(finalSlug)) {
      setLocalError(`An agent with slug "${finalSlug}" already exists.`);
      return;
    }
    setLocalError("");
    onCreate({ name: name.trim(), slug: finalSlug, description: description.trim() });
  };

  return (
    <div className="ab-modal-overlay" onClick={onCancel}>
      <div className="ab-modal" onClick={(e) => e.stopPropagation()}>
        <h2 className="ab-modal-title">Create agent</h2>

        {localError && (
          <div className="ab-banner ab-banner-error" role="alert">
            {localError}
          </div>
        )}

        <label className="ab-field">
          <span>Name</span>
          <input
            className="ab-input"
            value={name}
            autoFocus
            onChange={(e) => onNameChange(e.target.value)}
            placeholder="e.g. Chat agent"
          />
        </label>

        <label className="ab-field">
          <span>Slug</span>
          <input
            className="ab-input"
            value={slug}
            onChange={(e) => {
              setSlugEdited(true);
              setSlug(slugify(e.target.value));
            }}
            placeholder="e.g. chat-agent"
          />
        </label>

        <label className="ab-field">
          <span>What this agent does (optional)</span>
          <textarea
            className="ab-input"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. Summarises the conversation in a few short lines."
          />
        </label>

        <div className="ab-empty" style={{ fontSize: "12px", marginTop: "8px" }}>
          The supervisor reads the description to decide which parts of a
          message to send here. Add workers to the new agent with "+ Worker".
        </div>

        <div className="ab-modal-footer">
          <button className="ab-btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="ab-btn ab-btn-primary"
            onClick={submit}
            disabled={busy || !name.trim() || !slug.trim()}
          >
            {busy ? "Creating…" : "Create agent"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface CreateStepDialogProps {
  tools: Tool[];
  agents: Agent[];
  busy: boolean;
  existingSlugs: string[];
  onCancel: () => void;
  onCreate: (values: {
    name: string;
    slug: string;
    tools: string[];
    delegate_agent: string | null;
  }) => void;
}

function CreateStepDialog({
  tools,
  agents,
  busy,
  existingSlugs,
  onCancel,
  onCreate,
}: CreateStepDialogProps) {
  const [name, setName] = useState<string>("New worker");
  const [slug, setSlug] = useState<string>(`step-${Date.now().toString(36)}`);
  const [slugEdited, setSlugEdited] = useState<boolean>(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [delegate, setDelegate] = useState<string>("");
  const [localError, setLocalError] = useState<string>("");

  const onNameChange = (value: string) => {
    setName(value);
    if (!slugEdited) {
      const derived = slugify(value);
      setSlug(derived || `step-${Date.now().toString(36)}`);
    }
  };

  const togglePick = (id: string, checked: boolean) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const submit = () => {
    const finalSlug = slug.trim();
    if (!name.trim()) {
      setLocalError("Name is required.");
      return;
    }
    if (!finalSlug) {
      setLocalError("Slug is required.");
      return;
    }
    if (existingSlugs.includes(finalSlug)) {
      setLocalError(`A step with slug "${finalSlug}" already exists.`);
      return;
    }
    setLocalError("");
    onCreate({
      name: name.trim(),
      slug: finalSlug,
      tools: Array.from(picked),
      delegate_agent: delegate || null,
    });
  };

  return (
    <div className="ab-modal-overlay" onClick={onCancel}>
      <div className="ab-modal" onClick={(e) => e.stopPropagation()}>
        <h2 className="ab-modal-title">Add worker</h2>

        {localError && (
          <div className="ab-banner ab-banner-error" role="alert">
            {localError}
          </div>
        )}

        <label className="ab-field">
          <span>Name</span>
          <input
            className="ab-input"
            value={name}
            autoFocus
            onChange={(e) => onNameChange(e.target.value)}
            placeholder="e.g. Research"
          />
        </label>

        <label className="ab-field">
          <span>Slug</span>
          <input
            className="ab-input"
            value={slug}
            onChange={(e) => {
              setSlugEdited(true);
              setSlug(slugify(e.target.value));
            }}
            placeholder="e.g. research"
          />
        </label>

        <div className="ab-empty" style={{ marginTop: "4px", fontSize: "12px" }}>
          The step is added to the end of the flow, just before END.
        </div>

        <div className="ab-section-title" style={{ marginTop: "8px" }}>
          Run by agent (multi-agent)
        </div>
        <select
          className="ab-input"
          value={delegate}
          onChange={(e) => setDelegate(e.target.value)}
        >
          <option value="">Its own tools</option>
          {agents.map((a) => (
            <option key={`new-step-delegate-${a.id}`} value={String(a.id)}>
              {a.name || a.slug}
            </option>
          ))}
        </select>
        {agents.length === 0 && (
          <div className="ab-empty" style={{ marginTop: "4px", fontSize: "12px" }}>
          No other agent yet. Use "Add Agent" to create one (e.g. a Message
          agent with its own steps), then pick it here.
        </div>
      )}

        <div className="ab-section-title" style={{ marginTop: "8px" }}>
          Tools {delegate ? "(not used: the agent above does its parts)" : "(optional: none means it answers itself)"}
        </div>
        {tools.length === 0 ? (
          <div className="ab-empty">No tools available for this company.</div>
        ) : (
          <div className="ab-tool-picker">
            {tools.map((t) => (
              <div className="ab-tool-row" key={`new-step-tool-${t.id}`}>
                <input
                  type="checkbox"
                  checked={picked.has(String(t.id))}
                  onChange={(e) => togglePick(String(t.id), e.target.checked)}
                />
                <span>
                  {t.name || t.slug}{" "}
                  <span className="ab-node-kind" style={{ display: "inline" }}>
                    ({t.type})
                  </span>
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="ab-modal-footer">
          <button className="ab-btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="ab-btn ab-btn-primary"
            onClick={submit}
            disabled={busy || !name.trim() || !slug.trim()}
          >
            {busy ? "Adding…" : "Add worker"}
          </button>
        </div>
      </div>
    </div>
  );
}
