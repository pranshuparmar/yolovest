import { useParams, useNavigate } from "react-router-dom";
import { useTradeDetail } from "../hooks/queries";
import clsx from "clsx";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-3">{title}</h3>
      {children}
    </div>
  );
}

export function TradeDetailPage() {
  const { tradeId } = useParams<{ tradeId: string }>();
  const navigate = useNavigate();
  const { data, isLoading, error } = useTradeDetail(tradeId || "");

  if (isLoading) {
    return <div className="h-96 animate-pulse bg-gray-900 rounded-lg" />;
  }

  if (error || !data) {
    return (
      <div className="text-center py-20">
        <p className="text-gray-500">Trade not found</p>
        <button
          onClick={() => navigate("/trades")}
          className="mt-2 text-sm text-emerald-400 hover:underline"
        >
          Back to trades
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate("/trades")}
          className="text-gray-500 hover:text-gray-300 text-sm"
        >
          &larr; Back
        </button>
        <h2 className="text-lg font-semibold">
          {data.symbol}{" "}
          <span
            className={clsx(
              "text-sm px-2 py-0.5 rounded",
              data.signal_type === "BUY"
                ? "bg-emerald-900/40 text-emerald-400"
                : "bg-red-900/40 text-red-400"
            )}
          >
            {data.signal_type}
          </span>
        </h2>
      </div>

      {/* Trade Summary */}
      <Section title="Execution">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div>
            <p className="text-xs text-gray-500">Entry Price</p>
            <p>₹{fmt(data.entry_price)}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Fill Price</p>
            <p>₹{fmt(data.fill_price)}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Quantity</p>
            <p>{data.quantity}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Slippage</p>
            <p>{fmt(data.slippage)}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Stop Loss</p>
            <p className="text-red-400">₹{fmt(data.stop_loss_price)}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Target</p>
            <p className="text-emerald-400">₹{fmt(data.target_price)}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Product</p>
            <p>{data.product}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Status</p>
            <p>{data.status}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">PnL</p>
            <p
              className={clsx(
                data.pnl !== null && data.pnl > 0
                  ? "text-emerald-400"
                  : data.pnl !== null && data.pnl < 0
                    ? "text-red-400"
                    : ""
              )}
            >
              {data.pnl !== null ? `₹${fmt(data.pnl)}` : "—"}
            </p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Mode</p>
            <p>{data.mode}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Created</p>
            <p className="text-xs">{new Date(data.created_at).toLocaleString("en-IN")}</p>
          </div>
          {data.closed_at && (
            <div>
              <p className="text-xs text-gray-500">Closed</p>
              <p className="text-xs">
                {new Date(data.closed_at).toLocaleString("en-IN")}
              </p>
            </div>
          )}
        </div>
      </Section>

      {/* Signal */}
      {data.signal && (
        <Section title="Signal">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <p className="text-xs text-gray-500">Confidence</p>
              <p>{(data.signal.confidence_score * 100).toFixed(1)}%</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Model Version</p>
              <p>{data.signal.model_version}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Position Size</p>
              <p>{data.signal.position_size}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Generated</p>
              <p className="text-xs">
                {new Date(data.signal.created_at).toLocaleString("en-IN")}
              </p>
            </div>
          </div>
        </Section>
      )}

      {/* LLM Review */}
      {data.llm_review && (
        <Section title="LLM Review">
          <div className="mb-3">
            <span
              className={clsx(
                "text-xs px-2 py-0.5 rounded font-medium",
                data.llm_review.decision === "APPROVE"
                  ? "bg-emerald-900/40 text-emerald-400"
                  : data.llm_review.decision === "REJECT"
                    ? "bg-red-900/40 text-red-400"
                    : "bg-amber-900/40 text-amber-400"
              )}
            >
              {data.llm_review.decision}
            </span>
            {data.llm_review.adjusted_size !== null && (
              <span className="ml-2 text-xs text-gray-400">
                Resized to {data.llm_review.adjusted_size}
              </span>
            )}
          </div>
          <p className="text-sm text-gray-300 whitespace-pre-wrap">
            {data.llm_review.reasoning}
          </p>
        </Section>
      )}

      {/* Prediction */}
      {data.prediction && (
        <Section title="Prediction Outcome">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <p className="text-xs text-gray-500">Direction Correct</p>
              <p
                className={
                  data.prediction.direction_correct
                    ? "text-emerald-400"
                    : "text-red-400"
                }
              >
                {data.prediction.direction_correct === null
                  ? "Pending"
                  : data.prediction.direction_correct
                    ? "Yes"
                    : "No"}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Target Hit</p>
              <p>
                {data.prediction.target_hit === null
                  ? "Pending"
                  : data.prediction.target_hit
                    ? "Yes"
                    : "No"}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Actual PnL %</p>
              <p>
                {data.prediction.actual_pnl_pct !== null
                  ? `${data.prediction.actual_pnl_pct.toFixed(2)}%`
                  : "Pending"}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Actual Price</p>
              <p>
                {data.prediction.actual_price !== null
                  ? `₹${fmt(data.prediction.actual_price)}`
                  : "Pending"}
              </p>
            </div>
          </div>
        </Section>
      )}

      {/* Audit Trail */}
      {data.audit_trail.length > 0 && (
        <Section title="Audit Trail">
          <div className="space-y-2">
            {data.audit_trail.map((entry) => (
              <div
                key={entry.id}
                className="flex items-start gap-3 text-xs border-b border-gray-800/50 pb-2"
              >
                <span className="text-gray-500 whitespace-nowrap">
                  {new Date(entry.timestamp_ist).toLocaleTimeString("en-IN")}
                </span>
                <span className="text-gray-400 font-medium">
                  {entry.action_type}
                </span>
                {entry.skill_name && (
                  <span className="text-gray-600">[{entry.skill_name}]</span>
                )}
                {entry.duration_ms !== null && (
                  <span className="text-gray-600">
                    {entry.duration_ms.toFixed(0)}ms
                  </span>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
