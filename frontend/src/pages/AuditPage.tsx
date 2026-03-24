import { useState } from "react";
import { useAudit } from "../hooks/queries";
import { CSVExportButton } from "../components/CSVExportButton";

export function AuditPage() {
  const [actionType, setActionType] = useState<string | undefined>(undefined);
  const [limit, setLimit] = useState(50);
  const [expanded, setExpanded] = useState<number | null>(null);

  const { data, isLoading } = useAudit({
    limit,
    action_type: actionType,
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Audit Log</h2>
        <CSVExportButton
          data={(data || []) as unknown as Record<string, unknown>[]}
          filename={`audit-${new Date().toISOString().split("T")[0]}`}
        />
      </div>

      <div className="flex gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Action Type
          </label>
          <input
            type="text"
            value={actionType || ""}
            onChange={(e) => setActionType(e.target.value || undefined)}
            placeholder="Filter..."
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 w-40"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Limit</label>
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
            <option value={500}>500</option>
          </select>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        {isLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : !data || data.length === 0 ? (
          <p className="text-gray-500 text-sm py-4">No audit entries</p>
        ) : (
          <div className="space-y-1">
            {data.map((entry) => (
              <div key={entry.id} className="border-b border-gray-800/50">
                <button
                  onClick={() =>
                    setExpanded(expanded === entry.id ? null : entry.id)
                  }
                  className="w-full flex items-center gap-3 py-2 text-left text-sm hover:bg-gray-800/30"
                >
                  <span className="text-gray-500 text-xs whitespace-nowrap w-20">
                    {new Date(entry.timestamp_ist).toLocaleTimeString("en-IN", {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}
                  </span>
                  <span className="text-gray-300 font-medium w-40 truncate">
                    {entry.action_type}
                  </span>
                  {entry.skill_name && (
                    <span className="text-gray-500 text-xs">
                      [{entry.skill_name}]
                    </span>
                  )}
                  {entry.duration_ms !== null && (
                    <span className="text-gray-600 text-xs ml-auto">
                      {entry.duration_ms.toFixed(0)}ms
                    </span>
                  )}
                  <span className="text-gray-600 text-xs">
                    {expanded === entry.id ? "▼" : "▶"}
                  </span>
                </button>
                {expanded === entry.id && (
                  <div className="pb-2 pl-24 space-y-1">
                    {entry.input_summary && (
                      <div>
                        <p className="text-xs text-gray-500">Input:</p>
                        <pre className="text-xs text-gray-400 whitespace-pre-wrap max-h-40 overflow-auto">
                          {typeof entry.input_summary === "string"
                            ? entry.input_summary
                            : JSON.stringify(entry.input_summary, null, 2)}
                        </pre>
                      </div>
                    )}
                    {entry.output_summary && (
                      <div>
                        <p className="text-xs text-gray-500">Output:</p>
                        <pre className="text-xs text-gray-400 whitespace-pre-wrap max-h-40 overflow-auto">
                          {typeof entry.output_summary === "string"
                            ? entry.output_summary
                            : JSON.stringify(entry.output_summary, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
