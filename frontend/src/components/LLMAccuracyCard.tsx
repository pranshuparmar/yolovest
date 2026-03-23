import type { LLMAccuracy } from "../types/api";
import clsx from "clsx";

export function LLMAccuracyCard({ data }: { data: LLMAccuracy }) {
  const approvalRate =
    data.total_reviews > 0
      ? ((data.approved_count / data.total_reviews) * 100).toFixed(1)
      : "—";

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-4">
        LLM Review Accuracy
      </h3>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <p className="text-xs text-gray-500">Total Reviews</p>
          <p className="text-lg font-semibold">{data.total_reviews}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Approval Rate</p>
          <p className="text-lg font-semibold">{approvalRate}%</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Approved</p>
          <p className="text-lg font-semibold text-emerald-400">
            {data.approved_count}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Rejected</p>
          <p className="text-lg font-semibold text-red-400">
            {data.rejected_count}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Approval Accuracy</p>
          <p
            className={clsx(
              "text-lg font-semibold",
              data.approval_accuracy !== null && data.approval_accuracy >= 50
                ? "text-emerald-400"
                : "text-red-400"
            )}
          >
            {data.approval_accuracy !== null
              ? `${data.approval_accuracy.toFixed(1)}%`
              : "—"}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Profitable / Losing</p>
          <p className="text-lg font-semibold">
            <span className="text-emerald-400">
              {data.profitable_approvals}
            </span>
            {" / "}
            <span className="text-red-400">{data.losing_approvals}</span>
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Total PnL (Approved)</p>
          <p
            className={clsx(
              "text-lg font-semibold",
              data.approved_total_pnl >= 0
                ? "text-emerald-400"
                : "text-red-400"
            )}
          >
            ₹{data.approved_total_pnl.toFixed(2)}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Avg PnL (Approved)</p>
          <p
            className={clsx(
              "text-lg font-semibold",
              data.approved_avg_pnl >= 0 ? "text-emerald-400" : "text-red-400"
            )}
          >
            ₹{data.approved_avg_pnl.toFixed(2)}
          </p>
        </div>
      </div>
    </div>
  );
}
