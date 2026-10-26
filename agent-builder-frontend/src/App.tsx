import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useEdgesState,
  useNodesState,
  Node as FlowNode,
  Edge as FlowEdge,
  Connection,
  NodeChange,
  EdgeChange,
} from "reactflow";
import { api, Graph, Node, Edge, Tool } from "./api";
import RunModal from "./RunModal";

const KIND_CLASS: Record<string, string> = {
  start: "ab-node ab-node-start",
  step: "ab-node ab-node-step",
  end: "ab-node ab-node-end",
  tool: "ab-node ab-node-step",
  condition: "ab-node ab-node-step",
};

interface NodeData {
  raw: Node;
  label: React.ReactNode;
}

function toFlowNode(node: Node, toolsBySlugId: Record<number, string>): FlowNode<NodeData> {
  const pos = node.position && typeof node.position === "object" ? node.position : {};
  const toolNames = (node.tools || [])
    .map((id) => toolsBySlugId[id])
    .filter(Boolean);
  
  // Node content must be JSX wrapped in a div, cannot be a fragment
  const nodeContent = (
    <div>
      <div className="ab-node-kind">{node.kind}</div>
      <div className="ab-node-label">{node.label || node.slug}</div>
      {node.kind === "step" && (
        <div className="ab-node-tools">
          {toolNames.length ? toolNames.join(", ") : "no tools"}
        </div>
      )}
    </div>
  );
  
  return {
    id: String(node.id),
    position: {
      x: typeof pos.x === "number" ? pos.x : Math.random() * 320,
      y: typeof pos.y === "number" ? pos.y : Math.random() * 240,
    },
    data: {
      raw: node,
      label: nodeContent,
    },
    type: "default",
    className: KIND_CLASS[node.kind] || "ab-node ab-node-step",
  };
}

interface EdgeData {
  raw: Edge;
}

function toFlowEdge(edge: Edge): FlowEdge<EdgeData> {
  return {
    id: String(edge.id),
    source: String(edge.source),
    target: String(edge.target),
    data: { raw: edge },
  };
}

export default function App() {
  const [graphId, setGraphId] = useState<number | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [tools, setTools] = useState<Tool[]>([]);
  const [nodes, setNodes, onNodesChange] = useNodesState<NodeData>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<EdgeData>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [showRunModal, setShowRunModal] = useState<boolean>(false);

  const toolsBySlugId = useMemo(() => {
    const m: Record<number, string> = {};
    tools.forEach((t) => {
      m[t.id] = t.name || t.slug;
    });
    return m;
  }, [tools]);

  const showError = useCallback((e: any) => {
    setError(e && e.message ? e.message : String(e));
  }, []);

  const loadInto = useCallback(
    async (id: number) => {
      const payload = await api.loadGraph(id);
      setGraph(payload.graph);
      setTools(payload.tools || []);
      const byId: Record<number, string> = {};
      (payload.tools || []).forEach((t) => {
        byId[t.id] = t.name || t.slug;
      });
      setNodes((payload.nodes || []).map((n) => toFlowNode(n, byId)));
      setEdges((payload.edges || []).map(toFlowEdge));
    },
    [setNodes, setEdges]
  );

  useEffect(() => {
    (async () => {
      try {
        setLoading(true);
        const graphsResp = await api.listGraphs();
        const list = Array.isArray(graphsResp) ? graphsResp : graphsResp.results || [];
        let first = list[0];
        if (!first) {
          first = await api.createGraph({ name: "Default agent" });
        }
        setGraphId(first.id);
        await loadInto(first.id);
      } catch (e) {
        showError(e);
      } finally {
        setLoading(false);
      }
    })();
  }, [loadInto, showError]);

  const selectedNode = useMemo(() => {
    if (!selectedNodeId) return null;
    const fn = nodes.find((n) => n.id === selectedNodeId);
    return fn ? fn.data.raw : null;
  }, [selectedNodeId, nodes]);

  const refresh = useCallback(async () => {
    if (graphId) await loadInto(graphId);
  }, [graphId, loadInto]);

  const addNode = useCallback(
    async (kind: Node["kind"]) => {
      if (!graphId) return;
      try {
        const slug = `${kind}-${Date.now().toString(36)}`;
        await api.createNode({
          graph: graphId,
          slug,
          kind,
          label: kind === "step" ? "New step" : kind,
          position: { x: 120 + Math.random() * 320, y: 80 + Math.random() * 240 },
        });
        await refresh();
      } catch (e) {
        showError(e);
      }
    },
    [graphId, refresh, showError]
  );

  const onConnect = useCallback(
    async (connection: Connection) => {
      setEdges((eds) => addEdge(connection, eds));
      try {
        await api.createEdge({
          graph: graphId!,
          source: connection.source!,
          target: connection.target!,
        });
        await refresh();
      } catch (e) {
        showError(e);
        await refresh();
      }
    },
    [graphId, refresh, setEdges, showError]
  );

  const persistPosition = useCallback(
    async (node: FlowNode) => {
      try {
        // Use node.id as-is (ULID string), not parseInt
        await api.updateNode(node.id, { position: node.position });
      } catch (e) {
        showError(e);
      }
    },
    [showError]
  );

  const removeNode = useCallback(
    async (node: Node) => {
      // Use node label instead of technical ID for friendlier message
      const nodeName = node.label || node.slug || 'this node';
      if (!confirm(`Delete "${nodeName}"? This cannot be undone.`)) return;
      try {
        await api.deleteNode(node.id);
        setSelectedNodeId(null);
        await refresh();
      } catch (e) {
        showError(e);
      }
    },
    [refresh, showError]
  );

  const toggleNodeTool = useCallback(
    async (node: Node, toolId: number, checked: boolean) => {
      const current = new Set((node.tools || []).map(String));
      if (checked) current.add(String(toolId));
      else current.delete(String(toolId));
      try {
        await api.updateNode(node.id, { tools: Array.from(current).map(Number) });
        await refresh();
      } catch (e) {
        showError(e);
      }
    },
    [refresh, showError]
  );

  const renameNode = useCallback(
    async (node: Node, label: string) => {
      try {
        await api.updateNode(node.id, { label });
        await refresh();
      } catch (e) {
        showError(e);
      }
    },
    [refresh, showError]
  );

  const activate = useCallback(async () => {
    if (!graphId) return;
    try {
      const updated = await api.activateGraph(graphId);
      setGraph(updated);
    } catch (e) {
      showError(e);
    }
  }, [graphId, showError]);

  return (
    <div className="ab-shell">
      <div className="ab-canvas">
        <div className="ab-toolbar">
          <button className="ab-btn" onClick={() => addNode("start")}>
            + Start
          </button>
          <button className="ab-btn" onClick={() => addNode("step")}>
            + Step
          </button>
          <button className="ab-btn" onClick={() => addNode("end")}>
            + End
          </button>
          <button className="ab-btn" onClick={() => addNode("tool")}>
            + Tool
          </button>
          <button className="ab-btn" onClick={() => addNode("condition")}>
            + Condition
          </button>
          <button
            className="ab-btn ab-btn-primary"
            onClick={activate}
            disabled={!graphId || (graph?.is_active ?? false)}
          >
            {graph?.is_active ? "Active" : "Activate"}
          </button>
          <button
            className="ab-btn ab-btn-primary"
            onClick={() => setShowRunModal(true)}
            disabled={!graphId || !graph?.is_active}
          >
            Run
          </button>
        </div>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeDragStop={(_e, node) => persistPosition(node)}
          onNodeClick={(_e, node) => setSelectedNodeId(node.id)}
          onPaneClick={() => setSelectedNodeId(null)}
          onEdgesDelete={(deleted) =>
            deleted.forEach((e) => api.deleteEdge(e.id).catch(showError))
          }
          fitView
        >
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>

      <aside className="ab-sidebar">
        {error && (
          <div className="ab-banner ab-banner-error" role="alert">
            {error}
          </div>
        )}
        {loading && <div className="ab-empty">Loading…</div>}

        {graph && (
          <div className="ab-banner ab-banner-info">
            <strong>{graph.name}</strong>
            <div className="ab-node-kind">
              {graph.is_active ? "active" : "inactive"}
            </div>
          </div>
        )}

        <div className="ab-section-title">Selected node</div>
        {!selectedNode && (
          <div className="ab-empty">Click a node to edit it.</div>
        )}
        {selectedNode && (
          <NodeEditor
            key={selectedNode.id}
            node={selectedNode}
            tools={tools}
            onRename={(label) => renameNode(selectedNode, label)}
            onToggleTool={(toolId, checked) =>
              toggleNodeTool(selectedNode, toolId, checked)
            }
          />
        )}

        <div className="ab-section-title">Tools</div>
        {tools.length === 0 && (
          <div className="ab-empty">
            No tools yet. Run <code>manage.py seed_agents</code> to create the
            built-in tools.
          </div>
        )}
        {tools.map((t) => (
          <div className="ab-tool-row" key={t.id}>
            <input
              type="checkbox"
              checked={Boolean(t.is_enabled)}
              onChange={async (e) => {
                try {
                  await api.updateTool(t.id, { is_enabled: e.target.checked });
                  await refresh();
                } catch (err) {
                  showError(err);
                }
              }}
            />
            <span>
              {t.name || t.slug}
              {!t.is_enabled && " (disabled)"}
            </span>
          </div>
        ))}
      </aside>

      {showRunModal && graphId && graph && (
        <RunModal
          graphId={graphId}
          graph={graph}
          onClose={() => setShowRunModal(false)}
          onError={showError}
        />
      )}
    </div>
  );
}

interface NodeEditorProps {
  node: Node;
  tools: Tool[];
  onRename: (label: string) => void;
  onToggleTool: (toolId: number, checked: boolean) => void;
}

function NodeEditor({ node, tools, onRename, onToggleTool }: NodeEditorProps) {
  const [label, setLabel] = useState(node.label || "");
  const nodeToolIds = new Set((node.tools || []).map(String));
  
  return (
    <div>
      <label className="ab-field">
        <span>Label</span>
        <input
          className="ab-input"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          onBlur={() => label !== node.label && onRename(label)}
        />
      </label>
      <div className="ab-node-kind">
        {node.kind} · {node.slug}
      </div>

      {node.kind === "step" && (
        <>
          <div className="ab-section-title">Node tools</div>
          {tools.length === 0 && (
            <div className="ab-empty">No tools available.</div>
          )}
          {tools.map((t) => (
            <div className="ab-tool-row" key={t.id}>
              <input
                type="checkbox"
                checked={nodeToolIds.has(String(t.id))}
                onChange={(e) => onToggleTool(t.id, e.target.checked)}
              />
              <span>{t.name || t.slug}</span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
