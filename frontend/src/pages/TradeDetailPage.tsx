import { useParams, useNavigate, Link } from "react-router-dom";
import { useTradeDetail, useDeleteTrade } from "../hooks/queries";
import clsx from "clsx";
import { parseUTC, getTimezone } from "../utils/datetime";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-3">{title}</h3>
      {children}
    </div>
  );
}

// Visual reasoning chain timeline
function ReasoningTimeline({ data }: { data: NonNullable<ReturnType<typeof useTradeDetail>["data"]> }) {
  const steps: { label: string; status: "success" | "warning" | "error" | "pending" | "info"; detail: string; time?: string }[] = [];

  // Signal generation
  if (data.signal) {
    steps.push({
      label: "Signal Generated",
      status: "success",
      detail: `${data.signal.signal_type} @ ₹${fmt(data.signal.entry_price)} — Confidence: ${(data.signal.confidence_score * 100).toFixed(1)}% — Model: ${data.signal.model_version}`,
      time: data.signal.created_at,
    });
  }

  // Risk check (inferred from audit trail)
  const riskAudit = data.audit_trail.find((a) => a.action_type?.includes("risk") || a.skill_name === "risk-check");
  steps.push({
    label: "Risk Check",
    status: riskAudit ? "success" : "info",
    detail: riskAudit?.output_summary
      ? String(riskAudit.output_summary).slice(0, 120)
      : `SL: ₹${fmt(data.stop_loss_price)} — Target: ₹${fmt(data.target_price)} — Product: ${data.product}`,
    time: riskAudit?.timestamp_ist,
  });

  // LLM Review
  if (data.llm_review) {
    const dec = data.llm_review.decision;
    steps.push({
      label: `LLM Review: ${dec}`,
      status: dec === "APPROVE" ? "success" : dec === "REJECT" ? "error" : "warning",
      detail: data.llm_review.reasoning.slice(0, 200) + (data.llm_review.reasoning.length > 200 ? "..." : ""),
      time: data.llm_review.created_at,
    });
  } else {
    steps.push({ label: "LLM Review", status: "info", detail: "Auto-approved (LLM disabled or unavailable)" });
  }

  // Trade execution
  steps.push({
    label: "Trade Executed",
    status: data.fill_price > 0 ? "success" : "pending",
    detail: `Fill: ₹${fmt(data.fill_price)} — Qty: ${data.quantity} — Slippage: ${fmt(data.slippage)} — Mode: ${data.mode}`,
    time: data.created_at,
  });

  // Prediction
  if (data.prediction) {
    const p = data.prediction;
    const scored = p.direction_correct !== null;
    steps.push({
      label: scored ? "Prediction Scored" : "Prediction Pending",
      status: scored ? (p.direction_correct ? "success" : "error") : "pending",
      detail: scored
        ? `Direction: ${p.direction_correct ? "Correct" : "Wrong"} — Target: ${p.target_hit ? "Hit" : "Missed"} — PnL: ${p.actual_pnl_pct != null ? fmt(p.actual_pnl_pct) + "%" : "—"}`
        : `Awaiting scoring — End: ${p.prediction_end_time ? parseUTC(p.prediction_end_time).toLocaleString("en-IN", { timeZone: getTimezone() }) : "—"}`,
    });
  }

  // Outcome
  if (data.pnl !== null) {
    steps.push({
      label: "Trade Closed",
      status: data.pnl >= 0 ? "success" : "error",
      detail: `PnL: ₹${fmt(data.pnl)} — Exit: ${data.exit_price != null ? "₹" + fmt(data.exit_price) : "—"}`,
      time: data.closed_at || undefined,
    });
  }

  const statusColors = {
    success: "bg-emerald-500",
    warning: "bg-amber-500",
    error: "bg-red-500",
    pending: "bg-gray-500",
    info: "bg-blue-500",
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-4">Reasoning Chain</h3>
      <div className="space-y-0">
        {steps.map((step, i) => (
          <div key={i} className="flex gap-3">
            {/* Timeline line + dot */}
            <div className="flex flex-col items-center">
              <div className={clsx("w-3 h-3 rounded-full shrink-0 mt-1", statusColors[step.status])} />
              {i < steps.length - 1 && <div className="w-0.5 flex-1 bg-gray-800 my-1" />}
            </div>
            {/* Content */}
            <div className="pb-4 flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-gray-200">{step.label}</span>
                {step.time && (
                  <span className="text-xs text-gray-500">
                    {parseUTC(step.time).toLocaleTimeString("en-IN", { timeZone: getTimezone(), hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                  </span>
                )}
              </div>
              <p className="text-xs text-gray-400 mt-0.5 whitespace-pre-wrap">{step.detail}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function TradeDetailPage() {
  const { tradeId } = useParams<{ tradeId: string }>();
  const navigate = useNavigate();
  const { data, isLoading, error } = useTradeDetail(tradeId || "");
  const deleteTrade = useDeleteTrade();

  if (isLoading) return <div className="h-96 animate-pulse bg-gray-900 rounded-lg" />;

  if (error || !data) {
    return (
      <div className="text-center py-20">
        <p className="text-gray-500">Trade not found</p>
        <button onClick={() => navigate("/trades")} className="mt-2 text-sm text-emerald-400 hover:underline">
          Back to trades
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
      <div className="flex items-center gap-3">
        <button onClick={() => navigate("/trades")} className="text-gray-500 hover:text-gray-300 text-sm">&larr; Back</button>
        <h2 className="text-lg font-semibold">
          <Link to={`/symbol/${data.symbol}`} className="hover:text-blue-400 transition-colors">{data.symbol}</Link>
          {" "}
          <span className={clsx("text-sm px-2 py-0.5 rounded",
            data.signal_type === "BUY" ? "bg-emerald-900/40 text-emerald-400" : "bg-red-900/40 text-red-400"
          )}>{data.signal_type}</span>
          {data.origin === "adopted" && (
            <span className="ml-2 text-xs px-2 py-0.5 rounded bg-blue-900/40 text-blue-400">
              adopted
            </span>
          )}
        </h2>
      </div>
      <button
        onClick={() => {
          if (!window.confirm(`Delete trade ${data.trade_id} (${data.symbol})? This cannot be undone.`)) return;
          deleteTrade.mutate(data.trade_id, { onSuccess: () => navigate("/trades") });
        }}
        disabled={deleteTrade.isPending}
        className="px-2.5 py-1 rounded text-xs bg-red-900/60 hover:bg-red-800 text-red-400 disabled:opacity-50 transition-colors"
      >
        {deleteTrade.isPending ? "Deleting..." : "Delete Trade"}
      </button>
      </div>

      {/* Visual Reasoning Chain */}
      <ReasoningTimeline data={data} />

      {/* Trade Summary */}
      <Section title="Execution">
        {(() => {
          const invested = data.fill_price * data.quantity;
          const grossPnl =
            data.exit_price != null
              ? (data.signal_type === "BUY"
                  ? (data.exit_price - data.fill_price) * data.quantity
                  : (data.fill_price - data.exit_price) * data.quantity)
              : null;
          const pnlPct = data.pnl != null && invested > 0 ? (data.pnl / invested) * 100 : null;
          const pnlClass = (n: number | null) =>
            n != null && n > 0 ? "text-emerald-400" : n != null && n < 0 ? "text-red-400" : "";
          return (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div><p className="text-xs text-gray-500">Entry Price</p><p>₹{fmt(data.entry_price)}</p></div>
              <div><p className="text-xs text-gray-500">Fill Price</p><p>₹{fmt(data.fill_price)}</p></div>
              <div>
                <p className="text-xs text-gray-500">Exit Price</p>
                <p>{data.exit_price != null ? `₹${fmt(data.exit_price)}` : <span className="text-gray-500">—</span>}</p>
              </div>
              <div><p className="text-xs text-gray-500">Quantity</p><p>{data.quantity}</p></div>
              <div><p className="text-xs text-gray-500">Invested</p><p>₹{fmt(invested)}</p></div>
              <div><p className="text-xs text-gray-500">Slippage</p><p>{fmt(data.slippage)}</p></div>
              {data.estimated_costs != null && (
                <div><p className="text-xs text-gray-500">Est. Costs</p><p className="text-amber-400">₹{fmt(data.estimated_costs)}</p></div>
              )}
              <div><p className="text-xs text-gray-500">Stop Loss</p><p className="text-red-400">₹{fmt(data.stop_loss_price)}</p></div>
              <div><p className="text-xs text-gray-500">Target</p><p className="text-emerald-400">₹{fmt(data.target_price)}</p></div>
              <div><p className="text-xs text-gray-500">Product</p><p>{data.product}</p></div>
              <div><p className="text-xs text-gray-500">Status</p><p>{data.status}</p></div>
              <div>
                <p className="text-xs text-gray-500">Gross PnL</p>
                <p className={clsx(pnlClass(grossPnl))}>
                  {grossPnl != null ? `₹${fmt(grossPnl)}` : "—"}
                </p>
              </div>
              <div>
                <p className="text-xs text-gray-500">Net PnL (after costs)</p>
                <p className={clsx(pnlClass(data.pnl))}>
                  {data.pnl !== null ? `₹${fmt(data.pnl)}` : "—"}
                </p>
              </div>
              <div>
                <p className="text-xs text-gray-500">Net PnL %</p>
                <p className={clsx(pnlClass(pnlPct))}>
                  {pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${fmt(pnlPct)}%` : "—"}
                </p>
              </div>
              <div><p className="text-xs text-gray-500">Mode</p><p>{data.mode}</p></div>
              <div><p className="text-xs text-gray-500">Created</p><p className="text-xs">{parseUTC(data.created_at).toLocaleString("en-IN", { timeZone: getTimezone() })}</p></div>
              {data.closed_at && <div><p className="text-xs text-gray-500">Closed</p><p className="text-xs">{parseUTC(data.closed_at).toLocaleString("en-IN", { timeZone: getTimezone() })}</p></div>}
            </div>
          );
        })()}
      </Section>

      {/* Order IDs — for cross-reference with Zerodha. Always rendered so
          missing IDs (e.g. on rows inserted manually rather than by trade-
          execute) are explicit rather than silently hidden. */}
      <Section title="Broker Order IDs">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div>
            <p className="text-xs text-gray-500">Entry Order</p>
            <p className="font-mono text-xs break-all">
              {data.order_id || <span className="text-gray-600">—</span>}
            </p>
          </div>
          <div>
            <p className="text-xs text-gray-500">Stop-Loss Order</p>
            <p className="font-mono text-xs break-all">
              {data.sl_order_id || <span className="text-gray-600">—</span>}
            </p>
          </div>
          <div>
            <p className="text-xs text-gray-500">GTT (OCO)</p>
            {data.gtt_id ? (
              <div className="flex items-center gap-2">
                <p className="font-mono text-xs">{data.gtt_id}</p>
                {(() => {
                  const s = (data.gtt_status || "").toLowerCase();
                  const cls =
                    s === "active" || s === "scheduled"
                      ? "bg-emerald-900/40 text-emerald-400"
                      : s === "triggered"
                        ? "bg-blue-900/40 text-blue-400"
                        : s === "rejected" || s === "missing"
                          ? "bg-red-900/40 text-red-400"
                          : s === "cancelled" || s === "expired" || s === "deleted" || s === "disabled"
                            ? "bg-amber-900/40 text-amber-400"
                            : "bg-gray-700/50 text-gray-400";
                  return (
                    <span className={clsx("text-[10px] px-1.5 py-0.5 rounded font-medium", cls)}>
                      {s || "unknown"}
                    </span>
                  );
                })()}
              </div>
            ) : (
              <p className="font-mono text-xs"><span className="text-gray-600">—</span></p>
            )}
          </div>
        </div>
      </Section>

      {/* Transaction Cost Breakdown */}
      {data.cost_breakdown && (() => {
        const src = data.cost_breakdown.source;
        const sourceLabel =
          src === "broker" ? { text: "Broker actuals", cls: "bg-emerald-900/40 text-emerald-400" }
          : src === "contract_note" ? { text: "Contract note", cls: "bg-emerald-900/40 text-emerald-400" }
          : { text: "Estimate", cls: "bg-amber-900/40 text-amber-400" };
        const sttLabel = src === "estimate"
          ? `STT (${data.product === "MIS" ? "0.025%" : "0.1%"})`
          : "STT";
        return (
          <Section title="Transaction Costs">
            <div className="mb-3">
              <span className={clsx("text-xs px-2 py-0.5 rounded font-medium", sourceLabel.cls)}>
                {sourceLabel.text}
              </span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div><p className="text-xs text-gray-500">Brokerage</p><p>₹{fmt(data.cost_breakdown.brokerage)}</p></div>
              <div><p className="text-xs text-gray-500">{sttLabel}</p><p>₹{fmt(data.cost_breakdown.stt)}</p></div>
              <div><p className="text-xs text-gray-500">Other (Stamp + GST + Exchange)</p><p>₹{fmt(data.cost_breakdown.other_charges)}</p></div>
              <div><p className="text-xs text-gray-500">Total Charges</p><p className="text-amber-400 font-medium">₹{fmt(data.cost_breakdown.total)}</p></div>
            </div>
          </Section>
        );
      })()}

      {/* LLM Review full reasoning */}
      {data.llm_review && (
        <Section title="LLM Review — Full Reasoning">
          <div className="mb-3">
            <span className={clsx("text-xs px-2 py-0.5 rounded font-medium",
              data.llm_review.decision === "APPROVE" ? "bg-emerald-900/40 text-emerald-400"
                : data.llm_review.decision === "REJECT" ? "bg-red-900/40 text-red-400"
                  : "bg-amber-900/40 text-amber-400"
            )}>{data.llm_review.decision}</span>
            {data.llm_review.adjusted_size !== null && (
              <span className="ml-2 text-xs text-gray-400">Resized to {data.llm_review.adjusted_size}</span>
            )}
          </div>
          <p className="text-sm text-gray-300 whitespace-pre-wrap">{data.llm_review.reasoning}</p>
        </Section>
      )}

      {/* Audit Trail */}
      {data.audit_trail.length > 0 && (
        <Section title="Audit Trail">
          <div className="space-y-2">
            {data.audit_trail.map((entry) => (
              <div key={entry.id} className="flex items-start gap-3 text-xs border-b border-gray-800/50 pb-2">
                <span className="text-gray-500 whitespace-nowrap">
                  {parseUTC(entry.timestamp_ist).toLocaleTimeString("en-IN", { timeZone: getTimezone() })}
                </span>
                <span className="text-gray-400 font-medium">{entry.action_type}</span>
                {entry.skill_name && <span className="text-gray-600">[{entry.skill_name}]</span>}
                {entry.duration_ms !== null && <span className="text-gray-600">{(entry.duration_ms / 1000).toFixed(1)}s</span>}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
