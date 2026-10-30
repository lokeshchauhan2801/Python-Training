import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useEdgesState,
  useNodesState,
  Node as FlowNode,
  Edge as FlowEdge,
  Connection,
  ReactFlowInstance,
} from "reactflow";
import {
  api,
  Agent,
  AvailableFunction,
  Step,
  Tool,
  companyId,
  companyName,
} from "./api";
// The backend is a two-level Agent -> Steps model. Each CANVAS NODE is a STEP;
// the dropdown lists AGENTS. Selecting an agent shows ONLY that agent's steps,
// wired START -> steps (via next_step) -> END, fully isolated.

const START_ID = "__start__";
const END_ID = "__end__";

interface StepNodeData {
  raw: Step;
  label: React.ReactNode;
}

interface AnchorNodeData {
  label: React.ReactNode;
}

type CanvasNode = FlowNode<StepNodeData | AnchorNodeData>;

function toStepNode(step: Step, toolNameById: Record<string, string>): CanvasNode {
  const toolNames = (step.tools || [])
    .map((id) => toolNameById[String(id)])
    .filter(Boolean);

  const content = (
    <div>
      <div className="ab-node-kind">step</div>
      <div className="ab-node-label">{step.name || step.slug}</div>
      {toolNames.length > 0 && (
        <div className="ab-node-tools">{toolNames.join(", ")}</div>
      )}
    </div>
  );

  return {
    id: String(step.id),
    position: { x: step.position_x ?? 0, y: step.position_y ?? 0 },
    data: { raw: step, label: content },
    type: "default",
    className: "ab-node ab-node-step",
  };
}

function anchorNode(id: string, label: string, x: number, y: number, cls: string): CanvasNode {
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
    type: "default",
    className: `ab-node ${cls}`,
    draggable: false,
  };
}

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
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
    (flowSteps: Step[], flowTools: Tool[]) => {
      const names: Record<string, string> = {};
      flowTools.forEach((t) => {
        names[String(t.id)] = t.name || t.slug;
      });

      const stepNodes = flowSteps.map((s) => toStepNode(s, names));

      // Place the START/END anchors relative to the spread of step nodes so
      // every node (including unwired, freshly created ones) is on screen.
      const xs = flowSteps.map((s) => s.position_x ?? 0);
      const ys = flowSteps.map((s) => s.position_y ?? 0);
      const minX = xs.length ? Math.min(...xs) : 300;
      const maxX = xs.length ? Math.max(...xs) : 300;
      const midY = ys.length ? ys.reduce((s, y) => s + y, 0) / ys.length : 200;

      const canvasNodes: CanvasNode[] = [
        anchorNode(START_ID, "START", minX - 240, midY, "ab-node-start"),
        ...stepNodes,
        anchorNode(END_ID, "END", maxX + 240, midY, "ab-node-end"),
      ];

      // Edges are derived from EVERY step's flags/next_step (scoped to the one
      // selected agent — steps all belong to it, so no cross-agent edges).
      const stepIds = new Set(flowSteps.map((s) => String(s.id)));
      const canvasEdges: FlowEdge[] = [];
      flowSteps.forEach((s) => {
        if (s.connected_from_start) {
          canvasEdges.push({
            id: `${START_ID}->${s.id}`,
            source: START_ID,
            target: String(s.id),
          });
        }
        if (s.next_step && stepIds.has(String(s.next_step))) {
          canvasEdges.push({
            id: `${s.id}->${s.next_step}`,
            source: String(s.id),
            target: String(s.next_step),
          });
        }
        if (s.connected_to_end) {
          canvasEdges.push({
            id: `${s.id}->${END_ID}`,
            source: String(s.id),
            target: END_ID,
          });
        }
      });

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
      const payload = await api.loadFlow(company, agentId || undefined);
      const allAgents = payload.agents || [];
      const flowTools = payload.tools || [];
      const flowSteps = payload.steps || [];
      setAgents(allAgents);
      setTools(flowTools);
      setSteps(flowSteps);
      setSelectedAgentId(payload.selected_agent_id);
      renderFlow(flowSteps, flowTools);
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

  const selectedStep = useMemo(() => {
    if (!selectedStepId) return null;
    return steps.find((s) => String(s.id) === selectedStepId) || null;
  }, [selectedStepId, steps]);

  // Create a new empty Agent container, then select it.
  const createAgent = useCallback(
    async (values: { name: string; slug: string }) => {
      if (busy) return;
      setBusy(true);
      try {
        const created = await api.createAgent({
          slug: values.slug,
          name: values.name,
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
      connected_from_start: boolean;
      connected_to_end: boolean;
    }) => {
      if (busy || !selectedAgentId) return;
      setBusy(true);
      try {
        let position = { x: 300, y: 200 };
        if (rfInstance) {
          const pane = document.querySelector(".ab-canvas .react-flow__pane");
          const rect = pane?.getBoundingClientRect();
          if (rect) {
            position = rfInstance.screenToFlowPosition({
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
            });
          }
        }
        await api.createStep({
          agent: selectedAgentId,
          slug: values.slug,
          name: values.name,
          tools: values.tools,
          connected_from_start: values.connected_from_start,
          connected_to_end: values.connected_to_end,
          position_x: position.x,
          position_y: position.y,
          company: company || undefined,
        });
        setShowAddStep(false);
        await refresh(selectedAgentId);
        showSuccess("Step created");
      } catch (e) {
        showError(e);
      } finally {
        setBusy(false);
      }
    },
    [busy, company, refresh, rfInstance, selectedAgentId, showError, showSuccess]
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
      try {
        await api.updateStep(id, { position_x: pos.x, position_y: pos.y });
      } catch (e) {
        showError(e);
      }
    },
    [showError]
  );

  const schedulePosition = useCallback(
    (node: CanvasNode) => {
      if (node.id === START_ID || node.id === END_ID) return;
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
      if (node.id === START_ID || node.id === END_ID) return;
      const id = node.id;
      posLatest.current[id] = { x: node.position.x, y: node.position.y };
      if (posTimers.current[id]) {
        clearTimeout(posTimers.current[id]);
        delete posTimers.current[id];
      }
      void flushPosition(id);
    },
    [flushPosition]
  );

  // Flush any pending position timers on unmount.
  useEffect(() => {
    const timers = posTimers.current;
    return () => {
      Object.values(timers).forEach((t) => clearTimeout(t));
    };
  }, []);

  // Translate a canvas connection into Step PATCHes, then refresh.
  const onConnect = useCallback(
    async (connection: Connection) => {
      const source = connection.source;
      const target = connection.target;
      if (!source || !target || source === target) return;
      try {
        if (source === START_ID && target !== END_ID) {
          await api.updateStep(target, { connected_from_start: true });
        } else if (target === END_ID && source !== START_ID) {
          await api.updateStep(source, { connected_to_end: true, next_step: null });
        } else if (source !== START_ID && target !== END_ID) {
          await api.updateStep(source, { next_step: target });
        } else {
          return; // START->END or any anchor-to-anchor link is invalid
        }
        await refresh(selectedAgentId);
      } catch (e) {
        showError(e);
        await refresh(selectedAgentId);
      }
    },
    [refresh, selectedAgentId, showError]
  );

  // Deleting a derived edge PATCHes the inverse change, then refreshes.
  const onEdgesDelete = useCallback(
    async (deleted: FlowEdge[]) => {
      for (const edge of deleted) {
        try {
          if (edge.source === START_ID) {
            await api.updateStep(edge.target, { connected_from_start: false });
          } else if (edge.target === END_ID) {
            await api.updateStep(edge.source, { connected_to_end: false });
          } else {
            await api.updateStep(edge.source, { next_step: null });
          }
        } catch (e) {
          showError(e);
        }
      }
      await refresh(selectedAgentId);
    },
    [refresh, selectedAgentId, showError]
  );

  const onNodeClick = useCallback((_e: React.MouseEvent, node: CanvasNode) => {
    if (node.id === START_ID || node.id === END_ID) {
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

  const toggleFlag = useCallback(
    async (step: Step, field: "connected_from_start" | "connected_to_end", value: boolean) => {
      try {
        await api.updateStep(String(step.id), { [field]: value });
        await refresh(selectedAgentId);
      } catch (e) {
        showError(e);
      }
    },
    [refresh, selectedAgentId, showError]
  );

  const deleteStep = useCallback(
    async (step: Step) => {
      try {
        await api.deleteStep(String(step.id));
        setSelectedStepId(null);
        await refresh(selectedAgentId);
        showSuccess("Step deleted");
      } catch (e) {
        showError(e);
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
  );

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
        await refresh(id);
        if (rfInstance) {
          setTimeout(() => rfInstance.fitView({ padding: 0.3, duration: 300 }), 0);
        }
      } catch (e) {
        showError(e);
      }
    },
    [refresh, rfInstance, showError]
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
            Add Agent
          </button>
          <button
            className="ab-btn"
            onClick={() => setShowAddStep(true)}
            disabled={busy || !selectedAgentId}
            title={selectedAgentId ? "Add a step to the selected agent" : "Select an agent first"}
          >
            + Step
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
          onConnect={onConnect}
          onEdgesDelete={onEdgesDelete}
          onNodeDrag={(_e, node) => schedulePosition(node as CanvasNode)}
          onNodeDragStop={(_e, node) => persistPosition(node as CanvasNode)}
          onNodeClick={onNodeClick}
          onPaneClick={() => setSelectedStepId(null)}
          onInit={setRfInstance}
          proOptions={{ hideAttribution: true }}
          fitView
          fitViewOptions={{ padding: 0.3 }}
        >
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
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
          <strong>{companyName() || "Agent flow"}</strong>
          <div className="ab-node-kind">
            {selectedAgent
              ? selectedAgent.name || selectedAgent.slug
              : "no agent selected"}
          </div>
        </div>

        <div className="ab-empty" style={{ fontSize: "11px", marginTop: "4px" }}>
          💡 Tip: use "Add Agent" to create a flow, "+ Step" to add steps, then
          drag from a node handle to START/END or another step to wire them.
        </div>

        <div className="ab-section-title">Selected step</div>
        {!selectedStep && (
          <div className="ab-empty">Click a step node on the canvas to edit it.</div>
        )}
        {selectedStep && (
          <StepEditor
            key={selectedStep.id}
            step={selectedStep}
            tools={tools}
            company={company}
            onRename={(name) => renameStep(selectedStep, name)}
            onToggleTool={(id, checked) => toggleStepTool(selectedStep, id, checked)}
            onToggleFlag={(field, value) => toggleFlag(selectedStep, field, value)}
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
          busy={busy}
          existingSlugs={steps.map((s) => s.slug)}
          onCancel={() => setShowAddStep(false)}
          onCreate={createStep}
        />
      )}
    </div>
  );
}

interface StepEditorProps {
  step: Step;
  tools: Tool[];
  company: string;
  onRename: (name: string) => void;
  onToggleTool: (toolId: string, checked: boolean) => void;
  onToggleFlag: (field: "connected_from_start" | "connected_to_end", value: boolean) => void;
  onAttachFunction: (slug: string) => void;
  onDelete: () => void;
}

function StepEditor({
  step,
  tools,
  company,
  onRename,
  onToggleTool,
  onToggleFlag,
  onAttachFunction,
  onDelete,
}: StepEditorProps) {
  const [name, setName] = useState(step.name || "");
  const [functions, setFunctions] = useState<AvailableFunction[]>([]);
  const stepToolIds = new Set((step.tools || []).map(String));

  // Load the registered handler functions once per selected step so the
  // "Add tool from function" list can offer them.
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

  return (
    <div>
      <label className="ab-field">
        <span>Label</span>
        <input
          className="ab-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => onRename(name)}
        />
      </label>
      <div className="ab-node-kind">step · {step.slug}</div>

      <div className="ab-tool-row">
        <input
          type="checkbox"
          checked={step.connected_from_start}
          onChange={(e) => onToggleFlag("connected_from_start", e.target.checked)}
        />
        <span>Connected from START (entry)</span>
      </div>
      <div className="ab-tool-row">
        <input
          type="checkbox"
          checked={step.connected_to_end}
          onChange={(e) => onToggleFlag("connected_to_end", e.target.checked)}
        />
        <span>Connected to END (final)</span>
      </div>

      <button
        className="ab-btn"
        onClick={onDelete}
        style={{ width: "100%", marginTop: "8px", color: "#dc2626" }}
        title="Delete this step"
      >
        Delete Step
      </button>

      <div className="ab-section-title" title="Tools this step can use">
        Step Tools
      </div>
      <div className="ab-empty" style={{ marginBottom: "8px", fontSize: "12px" }}>
        Select which tools this step can use. The router picks one of them per
        step at run time.
      </div>
      {tools.length === 0 && <div className="ab-empty">No tools available.</div>}
      {tools.map((t) => (
        <div className="ab-tool-row" key={`step-${step.id}-tool-${t.id}`}>
          <input
            type="checkbox"
            checked={stepToolIds.has(String(t.id))}
            onChange={(e) => onToggleTool(String(t.id), e.target.checked)}
          />
          <span>
            {t.name || t.slug}{" "}
            <span className="ab-node-kind" style={{ display: "inline" }}>({t.type})</span>
          </span>
        </div>
      ))}

      <div
        className="ab-section-title"
        title="Create a tool directly from a registered handler function and add it to this step"
      >
        Add tool from function
      </div>
      <div className="ab-empty" style={{ marginBottom: "8px", fontSize: "12px" }}>
        Tools come from the backend handler functions. Adding one creates the
        tool (if needed) and attaches it to this step.
      </div>
      {functions.length === 0 && (
        <div className="ab-empty">No handler functions available.</div>
      )}
      {functions.map((fn) => {
        const alreadyOnStep = !!fn.tool_id && stepToolIds.has(String(fn.tool_id));
        return (
          <div className="ab-tool-row" key={`fn-${fn.slug}`} style={{ justifyContent: "space-between" }}>
            <span title={fn.description}>
              {fn.name}{" "}
              <span className="ab-node-kind" style={{ display: "inline" }}>
                ({fn.type})
              </span>
            </span>
            <button
              className="ab-btn"
              style={{ padding: "2px 10px", fontSize: "12px" }}
              disabled={alreadyOnStep}
              onClick={() => onAttachFunction(fn.slug)}
              title={alreadyOnStep ? "Already on this step" : `Add ${fn.name}`}
            >
              {alreadyOnStep ? "Added" : "Add"}
            </button>
          </div>
        );
      })}
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
  onCreate: (values: { name: string; slug: string }) => void;
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
    onCreate({ name: name.trim(), slug: finalSlug });
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

        <div className="ab-empty" style={{ fontSize: "12px", marginTop: "8px" }}>
          A new agent starts empty. Add steps to it with "+ Step".
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
  busy: boolean;
  existingSlugs: string[];
  onCancel: () => void;
  onCreate: (values: {
    name: string;
    slug: string;
    tools: string[];
    connected_from_start: boolean;
    connected_to_end: boolean;
  }) => void;
}

function CreateStepDialog({
  tools,
  busy,
  existingSlugs,
  onCancel,
  onCreate,
}: CreateStepDialogProps) {
  const [name, setName] = useState<string>("New step");
  const [slug, setSlug] = useState<string>(`step-${Date.now().toString(36)}`);
  const [slugEdited, setSlugEdited] = useState<boolean>(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [fromStart, setFromStart] = useState<boolean>(false);
  const [toEnd, setToEnd] = useState<boolean>(false);
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
      connected_from_start: fromStart,
      connected_to_end: toEnd,
    });
  };

  return (
    <div className="ab-modal-overlay" onClick={onCancel}>
      <div className="ab-modal" onClick={(e) => e.stopPropagation()}>
        <h2 className="ab-modal-title">Create step</h2>

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

        <div className="ab-section-title" style={{ marginTop: "8px" }}>
          Flow position
        </div>
        <div className="ab-tool-row">
          <input
            type="checkbox"
            checked={fromStart}
            onChange={(e) => setFromStart(e.target.checked)}
          />
          <span>Connected from START (entry)</span>
        </div>
        <div className="ab-tool-row">
          <input
            type="checkbox"
            checked={toEnd}
            onChange={(e) => setToEnd(e.target.checked)}
          />
          <span>Connected to END (final)</span>
        </div>

        <div className="ab-section-title" style={{ marginTop: "8px" }}>
          Tools (optional)
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
            {busy ? "Creating…" : "Create step"}
          </button>
        </div>
      </div>
    </div>
  );
}
