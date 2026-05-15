import { useState, useRef, useEffect } from "react";
import { useAudit, useServerLogs } from "../hooks/queries";
import { CSVExportButton } from "../components/CSVExportButton";
import { Pagination } from "../components/Pagination";
import { parseUTC, getTimezone } from "../utils/datetime";
import clsx from "clsx";

function AuditTab() {
  const [actionType, setActionType] = useState<string | undefined>(undefined);
  const [limit, setLimit] = useState(50);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  const pageSize = 20;

  const { data, isLoading } = useAudit({
    limit,
    action_type: actionType,
  });
  const totalRows = data?.length ?? 0;
  const paged = data?.slice(page * pageSize, (page + 1) * pageSize) ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Action Type
          </label>
          <input
            type="text"
            value={actionType || ""}
            onChange={(e) => { setActionType(e.target.value || undefined); setPage(0); }}
            placeholder="Filter..."
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 w-full sm:w-40"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Limit</label>
          <select
            value={limit}
            onChange={(e) => { setLimit(Number(e.target.value)); setPage(0); }}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
            <option value={500}>500</option>
          </select>
        </div>
        <CSVExportButton
          data={(data || []) as unknown as Record<string, unknown>[]}
          filename={`audit-${new Date().toISOString().split("T")[0]}`}
        />
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        {isLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : !data || data.length === 0 ? (
          <p className="text-gray-500 text-sm py-4">No audit entries</p>
        ) : (
          <div className="space-y-1">
            {paged.map((entry) => (
              <div key={entry.id} className="border-b border-gray-800/50">
                <button
                  onClick={() =>
                    setExpanded(expanded === entry.id ? null : entry.id)
                  }
                  className="w-full flex flex-wrap items-center gap-2 sm:gap-3 py-2 text-left text-sm hover:bg-gray-800/30"
                >
                  <span className="text-gray-500 text-xs whitespace-nowrap w-16 sm:w-20">
                    {parseUTC(entry.timestamp_ist).toLocaleTimeString("en-IN", {
                      timeZone: getTimezone(),
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}
                  </span>
                  <span className="text-gray-300 font-medium w-28 sm:w-40 truncate">
                    {entry.action_type}
                  </span>
                  {entry.skill_name && (
                    <span className="text-gray-500 text-xs">
                      [{entry.skill_name}]
                    </span>
                  )}
                  {entry.duration_ms !== null && (
                    <span className="text-gray-600 text-xs ml-auto">
                      {(entry.duration_ms / 1000).toFixed(1)}s
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
        <Pagination
          total={totalRows}
          page={page}
          pageSize={pageSize}
          onPageChange={setPage}
        />
      </div>
    </div>
  );
}

function ServerLogsTab() {
  const [lines, setLines] = useState(50);
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState("");
  const [copied, setCopied] = useState(false);
  const scrollRef = useRef<HTMLPreElement>(null);

  const { data, isLoading } = useServerLogs(lines);

  const logLines = data?.lines ?? [];
  const filtered = filter
    ? logLines.filter((l) => l.toLowerCase().includes(filter.toLowerCase()))
    : logLines;

  // Auto-scroll to bottom on new data
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filtered, autoScroll]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Filter</label>
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search logs..."
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 w-full sm:w-48"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Lines</label>
          <select
            value={lines}
            onChange={(e) => setLines(Number(e.target.value))}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={200}>200</option>
            <option value={500}>500</option>
          </select>
        </div>
        <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
            className="rounded"
          />
          Auto-scroll
        </label>
        <span className="text-xs text-gray-600">
          {filtered.length} lines | refreshing every 5s
        </span>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden relative">
        {/* Copy button */}
        {filtered.length > 0 && (
          <button
            onClick={() => {
              navigator.clipboard.writeText(filtered.join("\n"));
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            }}
            className="absolute top-2 right-2 z-10 px-2 py-1 rounded text-xs bg-gray-800 border border-gray-700 text-gray-400 hover:text-gray-200 hover:bg-gray-700 transition-colors"
            title="Copy logs to clipboard"
          >
            {copied ? "Copied!" : (
              <svg className="w-3.5 h-3.5 inline-block" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
            )}
          </button>
        )}
        {isLoading ? (
          <div className="h-96 animate-pulse bg-gray-800" />
        ) : (
          <pre
            ref={scrollRef}
            className="p-3 text-xs font-mono text-gray-400 overflow-auto max-h-[70vh] leading-relaxed"
          >
            {filtered.length === 0 ? (
              <span className="text-gray-600">No log lines{filter ? " matching filter" : ""}</span>
            ) : (
              filtered.map((line, i) => {
                const isWarning = line.includes("[WARNING]");
                const isError = line.includes("[ERROR]") || line.includes("[CRITICAL]");
                return (
                  <div
                    key={i}
                    className={clsx(
                      "py-0.5 hover:bg-gray-800/30",
                      isError && "text-red-400",
                      isWarning && !isError && "text-amber-400",
                    )}
                  >
                    {line}
                  </div>
                );
              })
            )}
          </pre>
        )}
      </div>
    </div>
  );
}

export function AuditPage() {
  const [tab, setTab] = useState<"audit" | "logs">("audit");

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h2 className="text-lg font-semibold">Audit & Logs</h2>
        <div className="flex gap-1 bg-gray-900 rounded-lg p-0.5">
          <button
            onClick={() => setTab("audit")}
            className={clsx(
              "px-3 py-1 rounded text-sm font-medium transition-colors",
              tab === "audit"
                ? "bg-gray-700 text-gray-100"
                : "text-gray-500 hover:text-gray-300"
            )}
          >
            Audit Log
          </button>
          <button
            onClick={() => setTab("logs")}
            className={clsx(
              "px-3 py-1 rounded text-sm font-medium transition-colors",
              tab === "logs"
                ? "bg-gray-700 text-gray-100"
                : "text-gray-500 hover:text-gray-300"
            )}
          >
            Server Logs
          </button>
        </div>
      </div>

      {tab === "audit" ? <AuditTab /> : <ServerLogsTab />}
    </div>
  );
}
