import { useState } from "react";
import clsx from "clsx";
import { useRecommendations } from "../hooks/queries";
import type { Recommendation, SignalDisposition } from "../types/api";

const DISPOSITION_LABELS: Record<SignalDisposition, string> = {
  pending: "Pending",
  risk_rejected: "Risk Blocked",
  llm_rejected: "LLM Rejected",
  awaiting_approval: "Awaiting Approval",
  executed: "Executed",
  recently_rejected_dedup: "Cooldown",
};

const DISPOSITION_STYLES: Record<SignalDisposition, string> = {
  pending: "bg-gray-800 text-gray-300",
  risk_rejected: "bg-orange-900/50 text-orange-300",
  llm_rejected: "bg-red-900/50 text-red-300",
  awaiting_approval: "bg-blue-900/50 text-blue-300",
  executed: "bg-emerald-900/50 text-emerald-300",
  recently_rejected_dedup: "bg-gray-700 text-gray-400",
};

function fmt(n: number, decimals = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function timeAgo(iso: string) {
  const ts = new Date(iso).getTime();
  const ageSec = Math.max(0, (Date.now() - ts) / 1000);
  if (ageSec < 60) return `${Math.floor(ageSec)}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`;
  return `${Math.floor(ageSec / 86400)}d ago`;
}

function RecommendationRow({ r }: { r: Recommendation }) {
  const [expanded, setExpanded] = useState(false);
  const sigColor = r.signal_type === "BUY" ? "text-emerald-400" : "text-red-400";
  const dispKey: SignalDisposition = (r.disposition || "pending") as SignalDisposition;
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between p-3 text-left hover:bg-gray-800/30"
      >
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <span className={clsx("text-xs font-bold w-10 shrink-0", sigColor)}>
            {r.signal_type}
          </span>
          <span className="font-medium text-gray-100 truncate">{r.symbol}</span>
          <span className="text-xs text-gray-500">
            ₹{fmt(r.entry_price)} × {r.position_size}
          </span>
          <span className="text-xs text-gray-500 hidden md:inline">
            conf {(r.confidence_score * 100).toFixed(0)}%
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span
            className={clsx(
              "text-[10px] px-2 py-0.5 rounded uppercase tracking-wide",
              DISPOSITION_STYLES[dispKey] ?? DISPOSITION_STYLES.pending
            )}
          >
            {DISPOSITION_LABELS[dispKey] ?? dispKey}
          </span>
          <span className="text-[10px] text-gray-600 w-14 text-right">
            {timeAgo(r.created_at)}
          </span>
        </div>
      </button>
      {expanded && (
        <div className="px-3 pb-3 border-t border-gray-800 mt-1 pt-2 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
          <div>
            <div className="text-gray-500">Entry</div>
            <div className="text-gray-200">₹{fmt(r.entry_price)}</div>
          </div>
          <div>
            <div className="text-gray-500">Target</div>
            <div className="text-emerald-400">₹{fmt(r.target_price)}</div>
          </div>
          <div>
            <div className="text-gray-500">Stop Loss</div>
            <div className="text-red-400">₹{fmt(r.stop_loss_price)}</div>
          </div>
          <div>
            <div className="text-gray-500">Confidence</div>
            <div className="text-gray-200">
              {(r.confidence_score * 100).toFixed(0)}%
            </div>
          </div>
          {r.disposition_reason && (
            <div className="col-span-2 md:col-span-4">
              <div className="text-gray-500">Reason</div>
              <div className="text-gray-300">{r.disposition_reason}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function RecommendationsPanel() {
  const { data, isLoading } = useRecommendations();
  const [filter, setFilter] = useState<SignalDisposition | "all">("all");

  if (isLoading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-32 animate-pulse" />
    );
  }

  const items = data || [];
  if (items.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-2">
          Today's Recommendations
        </h3>
        <p className="text-gray-500 text-sm">
          No signals generated yet today.
        </p>
      </div>
    );
  }

  const counts = items.reduce<Record<string, number>>((acc, r) => {
    const k = r.disposition || "pending";
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});

  const filtered = filter === "all" ? items : items.filter((r) => r.disposition === filter);
  const filterButtons: Array<{ key: SignalDisposition | "all"; label: string }> = [
    { key: "all", label: `All (${items.length})` },
    { key: "executed", label: `Executed (${counts.executed || 0})` },
    { key: "awaiting_approval", label: `Pending (${counts.awaiting_approval || 0})` },
    { key: "risk_rejected", label: `Risk Blocked (${counts.risk_rejected || 0})` },
    { key: "llm_rejected", label: `LLM Rejected (${counts.llm_rejected || 0})` },
  ];

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
        <h3 className="text-sm font-medium text-gray-400">
          Today's Recommendations
        </h3>
        <div className="flex flex-wrap gap-1">
          {filterButtons.map((b) => (
            <button
              key={b.key}
              onClick={() => setFilter(b.key)}
              className={clsx(
                "text-xs px-2 py-1 rounded",
                filter === b.key
                  ? "bg-gray-700 text-gray-100"
                  : "bg-gray-800/50 text-gray-400 hover:bg-gray-800"
              )}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>
      {filtered.length === 0 ? (
        <p className="text-gray-500 text-sm py-2">No items match the filter.</p>
      ) : (
        <div className="space-y-2">
          {filtered.map((r) => (
            <RecommendationRow key={r.id} r={r} />
          ))}
        </div>
      )}
    </div>
  );
}
