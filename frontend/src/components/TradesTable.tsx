import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import type { Trade } from "../types/api";
import { parseUTC, getTimezone } from "../utils/datetime";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function TradesTable({
  trades,
  compact = false,
}: {
  trades: Trade[];
  compact?: boolean;
}) {
  const navigate = useNavigate();

  if (trades.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No trades found</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-4">Symbol</th>
            <th className="pb-2 pr-4">Type</th>
            <th className="pb-2 pr-4">Entry</th>
            {!compact && <th className="pb-2 pr-4">Fill</th>}
            <th className="pb-2 pr-4">Exit</th>
            <th className="pb-2 pr-4">Qty</th>
            {!compact && <th className="pb-2 pr-4">Product</th>}
            <th className="pb-2 pr-4">Status</th>
            <th className="pb-2 pr-4">PnL</th>
            <th className="pb-2">Date & Time</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr
              key={t.trade_id}
              onClick={() => navigate(`/trades/${t.trade_id}`)}
              className="border-b border-gray-800/50 hover:bg-gray-800/30 cursor-pointer"
            >
              <td className="py-2 pr-4 font-medium">{t.symbol}</td>
              <td className="py-2 pr-4">
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
              <td className="py-2 pr-4">{fmt(t.entry_price)}</td>
              {!compact && <td className="py-2 pr-4">{fmt(t.fill_price)}</td>}
              <td className="py-2 pr-4">
                {t.exit_price !== null ? fmt(t.exit_price) : <span className="text-gray-500">—</span>}
              </td>
              <td className="py-2 pr-4">{t.quantity}</td>
              {!compact && <td className="py-2 pr-4 text-gray-400">{t.product}</td>}
              <td className="py-2 pr-4">
                <span className="text-xs text-gray-400">{t.status}</span>
              </td>
              <td
                className={clsx(
                  "py-2 pr-4",
                  t.pnl !== null && t.pnl > 0
                    ? "text-emerald-400"
                    : t.pnl !== null && t.pnl < 0
                      ? "text-red-400"
                      : "text-gray-500"
                )}
              >
                {t.pnl !== null ? (
                  <>
                    <div>₹{fmt(t.pnl)}</div>
                    {t.exit_price !== null && (
                      <div className="text-[10px] text-gray-500 leading-tight">
                        Gross ₹{fmt(
                          (t.signal_type === "BUY"
                            ? (t.exit_price - t.fill_price)
                            : (t.fill_price - t.exit_price)) * t.quantity,
                        )}
                      </div>
                    )}
                  </>
                ) : (
                  "—"
                )}
              </td>
              <td className="py-2 text-gray-500 text-xs whitespace-nowrap">
                {parseUTC(t.created_at).toLocaleDateString("en-IN", {
                  timeZone: getTimezone(),
                  day: "2-digit",
                  month: "short",
                })}{" "}
                {parseUTC(t.created_at).toLocaleTimeString("en-IN", {
                  timeZone: getTimezone(),
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
