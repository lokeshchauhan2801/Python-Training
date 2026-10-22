import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useEdgesState,
  useNodesState,
} from "reactflow";
import { api } from "./api.js";

const KIND_CLASS = {
  start: "ab-node ab-node-start",
  step: "ab-node ab-node-step",
  end: "ab-node ab-node-end",
};


function toFlowNode(node, toolsBySlugId) {
  const pos = node.position && typeof node.position === "object" ? node.position : {};
  const toolNames = (node.tools || [])
    .map((id) => toolsBySlugId[id])
    .filter(Boolean);
  return {
    id: String(node.id),
    position: {
      x: typeof pos.x === "number" ? pos.x : Math.random() * 320,
      y: typeof pos.y === "number" ? pos.y : Math.random() * 240,
    },
    data: {
      raw: node,
      label: (
        <div className={KIND_CLASS[node.kind] || "ab-node ab-node-step"}>
          <div className="ab-node-kind">{node.kind}</div>
          <div className="ab-node-label">{node.label || node.slug}</div>
          {node.kind === "step" && (
            <div className="ab-node-tools">
              {toolNames.length ? toolNames.join(", ") : "no tools"}
            </div>
          )}
        </div>
      ),
    },

    type: "default",
  };
}

function toFlowEdge(edge) {
  return {
    id: String(edge.id),
    source: String(edge.source),
    target: String(edge.target),
    data: { raw: edge },
  };
}

export default function App() {
  const [graphId, setGraphId] = useState(null);
  const [graph, setGraph] = useState(null);
  const [tools, setTools] = useState([]);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const toolsBySlugId = useMemo(() => {
    const m = {};
    tools.forEach((t) => {
      m[t.id] = t.name || t.slug;
    });
    return m;
  }, [tools]);

  const showError = useCallback((e) => {
    setError(e && e.message ? e.message : String(e));
  }, []);


  const loadInto = useCallback(
    async (id) => {
      const payload = await api.loadGraph(id);
      setGraph(payload.graph);
      setTools(payload.tools || []);
      const byId = {};
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
        const graphs = await api.listGraphs();
        const list = Array.isArray(graphs) ? graphs : graphs.results || [];
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
    async (kind) => {
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
    async (connection) => {

      setEdges((eds) => addEdge(connection, eds));
      try {
        await api.createEdge({
          graph: graphId,
          source: connection.source,
          target: connection.target,
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
    async (node) => {
      try {
        await api.updateNode(node.id, { position: node.position });
      } catch (e) {
        showError(e);
      }
    },
    [showError]
  );

  const removeNode = useCallback(
    async (id) => {
      try {
        await api.deleteNode(id);
        setSelectedNodeId(null);
        await refresh();
      } catch (e) {
        showError(e);
      }
    },
    [refresh, showError]
  );

  const toggleNodeTool = useCallback(
    async (node, toolId, checked) => {
      const current = new Set((node.tools || []).map(String));
      if (checked) current.add(String(toolId));
      else current.delete(String(toolId));
      try {
        await api.updateNode(node.id, { tools: Array.from(current) });
        await refresh();
      } catch (e) {
        showError(e);
      }
    },
    [refresh, showError]
  );

  const renameNode = useCallback(
    async (node, label) => {
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
          <button
            className="ab-btn ab-btn-primary"
            onClick={activate}
            disabled={!graphId || (graph && graph.is_active)}
          >
            {graph && graph.is_active ? "Active" : "Activate graph"}
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
            onDelete={() => removeNode(selectedNode.id)}
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
    </div>
  );
}

function NodeEditor({ node, tools, onRename, onToggleTool, onDelete }) {
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

      <button
        className="ab-btn"
        style={{ marginTop: 12 }}
        onClick={onDelete}
      >
        Delete node
      </button>
    </div>
  );
}
