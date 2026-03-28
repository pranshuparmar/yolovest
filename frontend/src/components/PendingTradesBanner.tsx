import { usePendingTrades, useApprovePendingTrade, useRejectPendingTrade } from "../hooks/queries";
import clsx from "clsx";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function PendingTradesBanner() {
  const { data: pending } = usePendingTrades();
  const approve = useApprovePendingTrade();
  const reject = useRejectPendingTrade();

  if (!pending || pending.length === 0) return null;

  return (
    <div className="bg-amber-900/20 border border-amber-800 rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-2">
        <span className="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse" />
        <h3 className="text-sm font-semibold text-amber-400">
          {pending.length} trade{pending.length > 1 ? "s" : ""} awaiting approval
        </h3>
      </div>

      <div className="space-y-2">
        {pending.map((t) => (
          <div
            key={t.id}
            className="flex flex-wrap items-center gap-3 bg-gray-900/50 rounded-lg px-4 py-3"
          >
            <span
              className={clsx(
                "px-1.5 py-0.5 rounded text-xs font-medium",
                t.signal_type === "BUY"
                  ? "bg-emerald-900/40 text-emerald-400"
                  : "bg-red-900/40 text-red-400"
              )}
            >
              {t.signal_type}
            </span>
            <span className="font-medium text-gray-200">{t.symbol}</span>
            <span className="text-sm text-gray-400">@ ₹{fmt(t.entry_price)}</span>
            <span className="text-xs text-gray-500">
              T: ₹{fmt(t.target_price)} | SL: ₹{fmt(t.stop_loss_price)}
            </span>
            <span className="text-xs text-gray-500">
              Conf: {((t.confidence_score || 0) * 100).toFixed(0)}%
            </span>
            <span className="text-xs text-gray-600">
              Qty: {t.position_size} | {t.product}
            </span>

            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={() => approve.mutate(t.id)}
                disabled={approve.isPending}
                className="px-3 py-1.5 rounded text-xs font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
              >
                {approve.isPending ? "..." : "Approve"}
              </button>
              <button
                onClick={() => reject.mutate(t.id)}
                disabled={reject.isPending}
                className="px-3 py-1.5 rounded text-xs font-medium bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 transition-colors"
              >
                {reject.isPending ? "..." : "Reject"}
              </button>
            </div>
          </div>
        ))}
      </div>

      <p className="text-xs text-gray-500">
        Pending trades auto-expire after 30 minutes. Approve via dashboard or Telegram (/approve &lt;id&gt;).
      </p>
    </div>
  );
}
