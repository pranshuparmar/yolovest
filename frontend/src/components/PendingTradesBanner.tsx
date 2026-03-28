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

  const handleApproveAll = () => {
    if (!window.confirm(`Approve all ${pending.length} pending trades?`)) return;
    for (const t of pending) approve.mutate(t.id);
  };

  const handleRejectAll = () => {
    if (!window.confirm(`Reject all ${pending.length} pending trades?`)) return;
    for (const t of pending) reject.mutate(t.id);
  };

  return (
    <div className="bg-amber-900/20 border border-amber-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 flex flex-wrap items-center justify-between gap-2 border-b border-amber-800/50">
        <div className="flex items-center gap-2">
          <span className="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse" />
          <h3 className="text-sm font-semibold text-amber-400">
            {pending.length} trade{pending.length > 1 ? "s" : ""} awaiting approval
          </h3>
          <span className="text-xs text-gray-500">Auto-expire in 30min</span>
        </div>
        {pending.length > 1 && (
          <div className="flex items-center gap-2">
            <button
              onClick={handleApproveAll}
              disabled={approve.isPending}
              className="px-2.5 py-1 rounded text-xs font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
            >
              Approve All
            </button>
            <button
              onClick={handleRejectAll}
              disabled={reject.isPending}
              className="px-2.5 py-1 rounded text-xs font-medium bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 transition-colors"
            >
              Reject All
            </button>
          </div>
        )}
      </div>

      <div className="overflow-x-auto max-h-64 overflow-y-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-amber-800/30 sticky top-0 bg-gray-900/90">
              <th className="py-2 px-3 text-left">Symbol</th>
              <th className="py-2 px-3 text-center">Signal</th>
              <th className="py-2 px-3 text-right">Entry</th>
              <th className="py-2 px-3 text-right">Target</th>
              <th className="py-2 px-3 text-right">SL</th>
              <th className="py-2 px-3 text-right">Conf</th>
              <th className="py-2 px-3 text-right">Qty</th>
              <th className="py-2 px-3 text-center">Actions</th>
            </tr>
          </thead>
          <tbody>
            {pending.map((t) => (
              <tr key={t.id} className="border-b border-gray-800/30 hover:bg-gray-800/20">
                <td className="py-2 px-3 font-medium text-gray-200">{t.symbol}</td>
                <td className="py-2 px-3 text-center">
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
                </td>
                <td className="py-2 px-3 text-right font-mono text-gray-300">{fmt(t.entry_price)}</td>
                <td className="py-2 px-3 text-right font-mono text-emerald-400">{fmt(t.target_price)}</td>
                <td className="py-2 px-3 text-right font-mono text-red-400">{fmt(t.stop_loss_price)}</td>
                <td className="py-2 px-3 text-right text-gray-400">
                  {((t.confidence_score || 0) * 100).toFixed(0)}%
                </td>
                <td className="py-2 px-3 text-right text-gray-400">{t.position_size}</td>
                <td className="py-2 px-3 text-center">
                  <div className="flex items-center justify-center gap-1.5">
                    <button
                      onClick={() => approve.mutate(t.id)}
                      disabled={approve.isPending}
                      className="px-2 py-1 rounded text-xs bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
                    >
                      {approve.isPending ? "..." : "Approve"}
                    </button>
                    <button
                      onClick={() => reject.mutate(t.id)}
                      disabled={reject.isPending}
                      className="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 transition-colors"
                    >
                      {reject.isPending ? "..." : "Reject"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
