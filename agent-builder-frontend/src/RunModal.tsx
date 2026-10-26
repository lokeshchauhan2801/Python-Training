import React, { useState } from "react";
import { api, Graph } from "./api";

interface RunModalProps {
  graphId: number;
  graph: Graph;
  onClose: () => void;
  onError: (err: any) => void;
}

export default function RunModal({ graphId, graph, onClose, onError }: RunModalProps) {
  const [prompt, setPrompt] = useState<string>("");
  const [response, setResponse] = useState<string>("");
  const [citations, setCitations] = useState<any[]>([]);
  const [chartData, setChartData] = useState<any>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string>("");

  const handleRun = async () => {
    if (!prompt.trim()) {
      setError("Please enter a prompt");
      return;
    }

    setLoading(true);
    setError("");
    setResponse("");
    setCitations([]);
    setChartData(null);

    try {
      // Step 1: Activate graph (ensure it's active)
      await api.activateGraph(graphId);

      // Step 2: Create a new chat session
      const session = await api.createChatSession(graph.company);

      // Step 3: Send the message
      const result = await api.sendChatMessage(session.id, prompt);

      // Step 4: Display the response
      setResponse(result.assistant.content || "");
      setCitations(result.assistant.citations || []);
      setChartData(result.assistant.chart_data || null);
    } catch (err: any) {
      let errorMessage = "Failed to run agent";
      
      if (err.status === 400) {
        errorMessage = err.message || "Bad request";
      } else if (err.status === 502) {
        errorMessage = "LLM failed to respond";
      } else if (err.status === 503) {
        errorMessage = "LLM not configured";
      } else if (err.message) {
        errorMessage = err.message;
      }
      
      setError(errorMessage);
      onError(err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-200 dark:border-slate-700">
          <h2 className="text-xl font-semibold text-slate-900 dark:text-white">
            Run Agent: {graph.name}
          </h2>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {/* Error banner */}
          {error && (
            <div className="ab-banner ab-banner-error" role="alert">
              {error}
            </div>
          )}

          {/* Prompt input */}
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
              Prompt
            </label>
            <textarea
              className="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-900 text-slate-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-violet-500"
              rows={4}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Enter your prompt here..."
              disabled={loading}
            />
          </div>

          {/* Response */}
          {response && (
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                Response
              </label>
              <div className="px-4 py-3 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg">
                <p className="text-sm text-slate-900 dark:text-white whitespace-pre-wrap">
                  {response}
                </p>
              </div>
            </div>
          )}

          {/* Citations */}
          {citations.length > 0 && (
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                Citations
              </label>
              <ul className="space-y-2">
                {citations.map((citation, idx) => (
                  <li
                    key={idx}
                    className="text-sm text-slate-600 dark:text-slate-400 px-4 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded"
                  >
                    {typeof citation === "string" ? citation : JSON.stringify(citation)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Chart data */}
          {chartData && (
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                Chart Data
              </label>
              <pre className="text-xs text-slate-900 dark:text-white bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg p-4 overflow-x-auto">
                {JSON.stringify(chartData, null, 2)}
              </pre>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex justify-end gap-3">
          <button
            className="ab-btn"
            onClick={onClose}
            disabled={loading}
          >
            Close
          </button>
          <button
            className="ab-btn ab-btn-primary"
            onClick={handleRun}
            disabled={loading || !prompt.trim()}
          >
            {loading ? "Running..." : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}
