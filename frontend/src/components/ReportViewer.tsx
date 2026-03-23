import { useState } from "react";
import type { Report } from "../types/api";
import clsx from "clsx";

export function ReportViewer({ reports }: { reports: Report[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (reports.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No reports available</p>;
  }

  return (
    <div className="space-y-2">
      {reports.map((r) => (
        <div
          key={r.id}
          className="bg-gray-900 border border-gray-800 rounded-lg"
        >
          <button
            onClick={() => setExpanded(expanded === r.id ? null : r.id)}
            className="w-full flex items-center justify-between p-3 text-left hover:bg-gray-800/30"
          >
            <div className="flex items-center gap-3">
              <span
                className={clsx(
                  "text-xs px-2 py-0.5 rounded",
                  r.report_type === "daily"
                    ? "bg-blue-900/40 text-blue-400"
                    : "bg-purple-900/40 text-purple-400"
                )}
              >
                {r.report_type}
              </span>
              <span className="text-sm">{r.report_date}</span>
            </div>
            <span className="text-gray-500 text-xs">
              {expanded === r.id ? "▼" : "▶"}
            </span>
          </button>
          {expanded === r.id && (
            <div className="px-3 pb-3 border-t border-gray-800">
              <pre className="mt-2 text-xs text-gray-400 overflow-auto max-h-96 whitespace-pre-wrap">
                {JSON.stringify(r.content, null, 2)}
              </pre>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
