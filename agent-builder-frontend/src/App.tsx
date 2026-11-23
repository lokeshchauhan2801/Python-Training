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
  Position,
  ReactFlowInstance,
} from "reactflow";
import {
  api,
  Agent,
  AgentConfig,
  AgentLayout,
  AvailableFunction,
  ConfigOptions,
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
    // Wires run left to right: in on the left, out on the right.
    targetPosition: Position.Left,
    sourcePosition: Position.Right,
    className: "ab-node ab-node-step",
  };
}

// START only has an output, END only an input.
function anchorNode(id: string, label: string, x: number, y: number, cls: string): CanvasNode {
  const isStart = id === START_ID;
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
    type: isStart ? "input" : "output",
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    className: `ab-node ${cls}`,
  };
}

// The selected agent survives a page reload: the canvas is an iframe whose
// src never carries it, so it is kept per company in this browser. A stale id
// is harmless: the server falls back to its default agent.
function agentStorageKey(company: string | null | undefined): string {
  return `agentBuilder.selectedAgent.${company || "global"}`;
}

function rememberedAgent(company: string | null | undefined): string | null {
  try {
    return window.localStorage.getItem(agentStorageKey(company));
  } catch {
    return null;
  }
}

function rememberAgent(company: string | null | undefined, agentId: string | null): void {
  try {
    if (agentId) window.localStorage.setItem(agentStorageKey(company), String(agentId));
  } catch {
    /* storage blocked: the default agent loads next time */
  }
}

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  // A new agent's config prefill and the allowed values (from the server).
  const [configDefaults, setConfigDefaults] = useState<AgentConfig | null>(null);
  const [configOptions, setConfigOptions] = useState<ConfigOptions | null>(null);
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

  // Drag state: the latest unsaved position per node, its debounce timer, and
  // the position saves still in flight (a reload waits for them, so it never
  // reads a position older than the one on screen).
  const posTimers = React.useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const posLatest = React.useRef<Record<string, { x: number; y: number }>>({});
  const pendingSaves = React.useRef<Set<Promise<unknown>>>(new Set());
  const flushRef = React.useRef<(id: string) => Promise<unknown>>(() => Promise.resolve());
  const track = useCallback((p: Promise<unknown>) => {
    pendingSaves.current.add(p);
    void p.finally(() => pendingSaves.current.delete(p));
    return p;
  }, []);

  // Rebuild the React Flow canvas (step nodes + START/END anchors + derived
  // edges) from the selected agent's steps and its saved START/END layout.
  const renderFlow = useCallback(
    (flowSteps: Step[], flowTools: Tool[], layout?: AgentLayout | null) => {
      const names: Record<string, string> = {};
      flowTools.forEach((t) => {
        names[String(t.id)] = t.name || t.slug;
      });

      // A node still being dragged or saved keeps its on-screen position.
      const stepNodes = flowSteps.map((s) => {
        const node = toStepNode(s, names);
        const local = posLatest.current[node.id];
        return local ? { ...node, position: { ...local } } : node;
      });

      // Place the START/END anchors relative to the spread of step nodes so
      // every node (including unwired, freshly created ones) is on screen.
      const xs = flowSteps.map((s) => s.position_x ?? 0);
      const ys = flowSteps.map((s) => s.position_y ?? 0);
      const minX = xs.length ? Math.min(...xs) : 300;
      const maxX = xs.length ? Math.max(...xs) : 300;
      const midY = ys.length ? ys.reduce((s, y) => s + y, 0) / ys.length : 200;

      const startAt = posLatest.current[START_ID] || layout?.start;
      const endAt = posLatest.current[END_ID] || layout?.end;
      const canvasNodes: CanvasNode[] = [
        anchorNode(
          START_ID, "START",
          startAt?.x ?? minX - 240, startAt?.y ?? midY,
          "ab-node-start"
        ),
        ...stepNodes,
        anchorNode(
          END_ID, "END",
          endAt?.x ?? maxX + 240, endAt?.y ?? midY,
          "ab-node-end"
        ),
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
      // Flush pending drags, then wait for every position save to land.
      Object.keys(posTimers.current).forEach((id) => {
        clearTimeout(posTimers.current[id]);
        delete posTimers.current[id];
      });
      await Promise.allSettled([
        ...Object.keys(posLatest.current).map((id) => flushRef.current(id)),
        ...pendingSaves.current,
      ]);
      const payload = await api.loadFlow(company, agentId || undefined);
      const allAgents = payload.agents || [];
      const flowTools = payload.tools || [];
      const flowSteps = payload.steps || [];
      setAgents(allAgents);
      setTools(flowTools);
      setSteps(flowSteps);
      setConfigDefaults(payload.config_defaults || null);
      setConfigOptions(payload.config_options || null);
      setSelectedAgentId(payload.selected_agent_id);
      rememberAgent(company, payload.selected_agent_id);
      const selected = allAgents.find((a) => String(a.id) === String(payload.selected_agent_id));
      renderFlow(flowSteps, flowTools, selected?.layout);
    },
    [company, renderFlow]
  );

  useEffect(() => {
    (async () => {
      try {
        setLoading(true);
        // Reopen the agent picked last time (the page reload resets the iframe).
        await refresh(rememberedAgent(company));
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
    async (values: {
      name: string;
      slug: string;
      prompt: string;
      global: boolean;
      config: AgentConfig;
    }) => {
      if (busy) return;
      setBusy(true);
      try {
        const created = await api.createAgent({
          slug: values.slug,
          name: values.name,
          prompt: values.prompt,
          config: values.config,
          // null = global (every company); only an administrator may.
          company: values.global ? null : company || undefined,
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

  // Save the agent's role (its Langfuse prompt).
  const saveAgentPrompt = useCallback(
    async (agentId: string, prompt: string) => {
      try {
        await api.updateAgent(agentId, { prompt: prompt.trim() });
        await refresh(selectedAgentId);
        showSuccess("Role saved to Langfuse");
        return true;
      } catch (e) {
        showError(e);
        return false;
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
  );

  // Save the agent's config (a new version of its Langfuse prompt).
  const saveAgentConfig = useCallback(
    async (agentId: string, config: AgentConfig) => {
      try {
        await api.updateAgent(agentId, { config });
        await refresh(selectedAgentId);
        showSuccess("Config saved to Langfuse");
        return true;
      } catch (e) {
        showError(e);
        return false;
      }
    },
    [refresh, selectedAgentId, showError, showSuccess]
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
  const POSITION_DEBOUNCE_MS = 1000;

  // Save a node's latest dragged position: a step's own position_x/y, or
  // START/END in the agent's layout. The local position is kept until the
  // save lands, so a reload in between cannot snap the node back.
  const flushPosition = useCallback(
    (id: string): Promise<unknown> => {
      const pos = posLatest.current[id];
      if (!pos) return Promise.resolve();
      const done = () => {
        if (posLatest.current[id] === pos) delete posLatest.current[id];
      };
      let save: Promise<unknown>;
      if (id === START_ID || id === END_ID) {
        if (!selectedAgentId) return Promise.resolve(done());
        const key = id === START_ID ? "start" : "end";
        const point = { x: pos.x, y: pos.y };
        setAgents((prev) =>
          prev.map((a) =>
            String(a.id) === selectedAgentId
              ? { ...a, layout: { ...(a.layout || {}), [key]: point } }
              : a
          )
        );
        save = api.updateAgent(selectedAgentId, { layout: { [key]: point } });
      } else {
        setSteps((prev) =>
          prev.map((s) =>
            String(s.id) === id ? { ...s, position_x: pos.x, position_y: pos.y } : s
          )
        );
        save = api.updateStep(id, { position_x: pos.x, position_y: pos.y });
      }
      return track(save.then(done, (e) => {
        done();
        showError(e);
      }));
    },
    [selectedAgentId, showError, track]
  );
  flushRef.current = flushPosition;

  const schedulePosition = useCallback(
    (node: CanvasNode) => {
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
          // The backend clears the source's END link (a step ends the flow or
          // goes on, never both). If the source WAS the last step, END moves to
          // the new step: START -> A -> END plus A -> B gives START -> A -> B -> END.
          const src = steps.find((s) => String(s.id) === source);
          const tgt = steps.find((s) => String(s.id) === target);
          await api.updateStep(source, { next_step: target });
          if (tgt && (tgt.connected_from_start || (src?.connected_to_end && !tgt.next_step && !tgt.connected_to_end))) {
            await api.updateStep(target, {
              // B now follows A, so it is no longer an entry step.
              connected_from_start: false,
              connected_to_end: !!(src?.connected_to_end && !tgt.next_step) || tgt.connected_to_end,
            });
          }
        } else {
          return; // START->END or any anchor-to-anchor link is invalid
        }
        await refresh(selectedAgentId);
      } catch (e) {
        showError(e);
        await refresh(selectedAgentId);
      }
    },
    [refresh, selectedAgentId, showError, steps]
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
                  {a.company == null ? " (global)" : ""}
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

        {selectedAgent && (
          <RoleEditor
            key={`agent-role-${selectedAgent.id}-${selectedAgent.prompt}`}
            value={selectedAgent.prompt || ""}
            onSave={(text) => saveAgentPrompt(String(selectedAgent.id), text)}
          />
        )}

        {selectedAgent && selectedAgent.config && configOptions && (
          <ConfigEditor
            key={`agent-config-${selectedAgent.id}-${JSON.stringify(selectedAgent.config)}`}
            value={selectedAgent.config}
            defaults={configDefaults}
            options={configOptions}
            onSave={(config) => saveAgentConfig(String(selectedAgent.id), config)}
          />
        )}

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

      {showAddAgent && configDefaults && configOptions && (
        <CreateAgentDialog
          defaults={configDefaults}
          options={configOptions}
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

  // Load the registered handler functions once per selected step: a built-in
  // tool with no Tool row yet is listed as an unchecked box, and ticking it
  // creates the row and attaches it.
  useEffect(() => {
    let cancelled = false;
    api
      .availableFunctions(company || undefined, String(step.id))
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
  const toolSlugs = new Set(tools.map((t) => t.slug));
  const missingFunctions = functions.filter((fn) => !toolSlugs.has(fn.slug));

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
      {tools.length === 0 && missingFunctions.length === 0 && (
        <div className="ab-empty">No tools available.</div>
      )}
      {tools.map((t) => (
        <div className="ab-tool-row" key={`step-${step.id}-tool-${t.id}`}>
          <input
            type="checkbox"
            checked={stepToolIds.has(String(t.id))}
            onChange={(e) => onToggleTool(String(t.id), e.target.checked)}
          />
          <span>
            {t.name || t.slug}{" "}
            <span className="ab-node-kind" style={{ display: "inline" }}>({t.slug})</span>
          </span>
        </div>
      ))}

      {missingFunctions.map((fn) => (
        <div className="ab-tool-row" key={`step-${step.id}-fn-${fn.slug}`}>
          <input
            type="checkbox"
            checked={false}
            onChange={(e) => e.target.checked && onAttachFunction(fn.slug)}
          />
          <span title={fn.description}>
            {fn.name}{" "}
            <span className="ab-node-kind" style={{ display: "inline" }}>({fn.slug})</span>
          </span>
        </div>
      ))}
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

interface RoleEditorProps {
  value: string;
  onSave: (text: string) => Promise<boolean>;
}

/** The agent's role (its Langfuse prompt): what it is for and which tasks it
 * can do. Every step of the agent answers in this role. */
function RoleEditor({ value, onSave }: RoleEditorProps) {
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  return (
    <label className="ab-field">
      <span>Agent role (Langfuse prompt)</span>
      <textarea
        className="ab-input"
        rows={4}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder="What this agent is for and which tasks it can do."
      />
      <button
        className="ab-btn ab-btn-primary"
        style={{ marginTop: "6px" }}
        disabled={saving || draft.trim() === value.trim()}
        onClick={async () => {
          setSaving(true);
          await onSave(draft);
          setSaving(false);
        }}
      >
        {saving ? "Saving…" : "Save role"}
      </button>
    </label>
  );
}

/** The config form's values as typed (numbers stay strings until saved). */
interface ConfigDraft {
  modelKey: string; // "<provider>::<model>"
  temperature: string;
  max_tokens: string;
  top_k: string;
}

const modelKey = (provider: string, model: string) => `${provider}::${model}`;

function toDraft(config: AgentConfig): ConfigDraft {
  return {
    modelKey: modelKey(config.provider, config.model),
    temperature: String(config.temperature),
    max_tokens: String(config.max_tokens),
    top_k: String(config.retrieval?.top_k ?? ""),
  };
}

/** The draft as an AgentConfig, or the first error message. The server
 * validates again; this only catches mistakes before the request. */
function fromDraft(draft: ConfigDraft, options: ConfigOptions): AgentConfig | string {
  const [provider, model] = draft.modelKey.split("::");
  if (!options.models.some((m) => m.provider === provider && m.model === model)) {
    return "Pick a model from the list.";
  }
  const lim = options.limits;
  const checks: [string, string, [number, number], boolean][] = [
    ["Temperature", draft.temperature, lim.temperature, false],
    ["Max tokens", draft.max_tokens, lim.max_tokens, true],
    ["Passages per question", draft.top_k, lim.top_k, true],
  ];
  for (const [label, raw, [low, high], whole] of checks) {
    const n = Number(raw);
    if (raw.trim() === "" || Number.isNaN(n)) return `${label} must be a number.`;
    if (whole && !Number.isInteger(n)) return `${label} must be a whole number.`;
    if (n < low || n > high) return `${label} must be between ${low} and ${high}.`;
  }
  return {
    provider,
    model,
    temperature: Number(draft.temperature),
    max_tokens: Number(draft.max_tokens),
    retrieval: { top_k: Number(draft.top_k) },
  };
}

interface ConfigFieldsProps {
  draft: ConfigDraft;
  options: ConfigOptions;
  onChange: (draft: ConfigDraft) => void;
}

/** Model, temperature, max tokens and retrieval inputs. */
function ConfigFields({ draft, options, onChange }: ConfigFieldsProps) {
  const set = (field: keyof ConfigDraft) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>
  ) => onChange({ ...draft, [field]: e.target.value });
  const lim = options.limits;
  const known = options.models.some(
    (m) => modelKey(m.provider, m.model) === draft.modelKey
  );

  return (
    <>
      <label className="ab-field">
        <span>Model</span>
        <select className="ab-select" value={draft.modelKey} onChange={set("modelKey")}>
          {!known && <option value={draft.modelKey}>{draft.modelKey.replace("::", " / ")} (not allowed)</option>}
          {options.models.map((m) => (
            <option key={modelKey(m.provider, m.model)} value={modelKey(m.provider, m.model)}>
              {m.model} ({m.provider})
            </option>
          ))}
        </select>
      </label>
      <div className="ab-config-grid">
        <label className="ab-field">
          <span>Temperature ({lim.temperature[0]}–{lim.temperature[1]})</span>
          <input
            className="ab-input"
            type="number"
            step="0.1"
            min={lim.temperature[0]}
            max={lim.temperature[1]}
            value={draft.temperature}
            onChange={set("temperature")}
          />
        </label>
        <label className="ab-field">
          <span>Max tokens (≤ {lim.max_tokens[1]})</span>
          <input
            className="ab-input"
            type="number"
            step="1"
            min={lim.max_tokens[0]}
            max={lim.max_tokens[1]}
            value={draft.max_tokens}
            onChange={set("max_tokens")}
          />
        </label>
        <label className="ab-field">
          <span>Passages per question</span>
          <input
            className="ab-input"
            type="number"
            step="1"
            min={lim.top_k[0]}
            max={lim.top_k[1]}
            value={draft.top_k}
            onChange={set("top_k")}
          />
        </label>
      </div>
    </>
  );
}

interface ConfigEditorProps {
  value: AgentConfig;
  defaults: AgentConfig | null;
  options: ConfigOptions;
  onSave: (config: AgentConfig) => Promise<boolean>;
}

/** The selected agent's config (its Langfuse prompt's config). Saving adds a
 * new prompt version in Langfuse and keeps the role text. */
function ConfigEditor({ value, defaults, options, onSave }: ConfigEditorProps) {
  const [draft, setDraft] = useState<ConfigDraft>(() => toDraft(value));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const unchanged = JSON.stringify(draft) === JSON.stringify(toDraft(value));

  return (
    <div className="ab-field">
      <span>Agent config (Langfuse prompt config)</span>
      {error && (
        <div className="ab-banner ab-banner-error" role="alert">
          {error}
        </div>
      )}
      <ConfigFields draft={draft} options={options} onChange={setDraft} />
      <div style={{ display: "flex", gap: "6px" }}>
        <button
          className="ab-btn ab-btn-primary"
          disabled={saving || unchanged}
          onClick={async () => {
            const parsed = fromDraft(draft, options);
            if (typeof parsed === "string") {
              setError(parsed);
              return;
            }
            setError("");
            setSaving(true);
            await onSave(parsed);
            setSaving(false);
          }}
        >
          {saving ? "Saving…" : "Save config"}
        </button>
        {defaults && (
          <button
            className="ab-btn"
            disabled={saving}
            onClick={() => setDraft(toDraft(defaults))}
            title="Fill in the default values (save to apply)"
          >
            Use defaults
          </button>
        )}
      </div>
    </div>
  );
}

interface CreateAgentDialogProps {
  defaults: AgentConfig;
  options: ConfigOptions;
  busy: boolean;
  existingSlugs: string[];
  onCancel: () => void;
  onCreate: (values: {
    name: string;
    slug: string;
    prompt: string;
    global: boolean;
    config: AgentConfig;
  }) => void;
}

function CreateAgentDialog({
  defaults,
  options,
  busy,
  existingSlugs,
  onCancel,
  onCreate,
}: CreateAgentDialogProps) {
  const [name, setName] = useState<string>("New agent");
  const [slug, setSlug] = useState<string>(`agent-${Date.now().toString(36)}`);
  const [slugEdited, setSlugEdited] = useState<boolean>(false);
  const [prompt, setPrompt] = useState<string>("");
  const [isGlobal, setIsGlobal] = useState<boolean>(false);
  // Prefilled with the defaults: the model, temperature, max tokens and
  // retrieval chat runs with today.
  const [configDraft, setConfigDraft] = useState<ConfigDraft>(() => toDraft(defaults));
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
    const parsed = fromDraft(configDraft, options);
    if (typeof parsed === "string") {
      setLocalError(parsed);
      return;
    }
    setLocalError("");
    onCreate({
      name: name.trim(),
      slug: finalSlug,
      prompt: prompt.trim(),
      global: isGlobal,
      config: parsed,
    });
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
          <span>Role and tasks (optional, saved to Langfuse)</span>
          <textarea
            className="ab-input"
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="e.g. You are the meeting agent. You summarise meetings and list their action items."
          />
        </label>

        <label className="ab-field" style={{ flexDirection: "row", alignItems: "center", gap: "8px" }}>
          <input
            type="checkbox"
            checked={isGlobal}
            onChange={(e) => setIsGlobal(e.target.checked)}
          />
          <span>Global: every company can use it (administrators only)</span>
        </label>

        <div className="ab-section-title">Config (saved to Langfuse)</div>
        <ConfigFields draft={configDraft} options={options} onChange={setConfigDraft} />

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
      // Left unwired: the server adds the step after the flow's last step.
      connected_from_start: false,
      connected_to_end: false,
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
                    ({t.slug})
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
