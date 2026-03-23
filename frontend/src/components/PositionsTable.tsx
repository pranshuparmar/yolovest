import clsx from "clsx";
import type { Trade } from "../types/api";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function PositionsTable({ positions }: { positions: Trade[] }) {
  if (positions.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No open positions</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-4">Symbol</th>
            <th className="pb-2 pr-4">Type</th>
            <th className="pb-2 pr-4">Entry</th>
            <th className="pb-2 pr-4">Fill</th>
            <th className="pb-2 pr-4">Qty</th>
            <th className="pb-2 pr-4">SL</th>
            <th className="pb-2 pr-4">Target</th>
            <th className="pb-2 pr-4">Product</th>
            <th className="pb-2 pr-4">Slippage</th>
            <th className="pb-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr
              key={p.trade_id}
              className="border-b border-gray-800/50 hover:bg-gray-800/30"
            >
              <td className="py-2 pr-4 font-medium">{p.symbol}</td>
              <td className="py-2 pr-4">
                <span
                  className={clsx(
                    "px-1.5 py-0.5 rounded text-xs font-medium",
                    p.signal_type === "BUY"
                      ? "bg-emerald-900/40 text-emerald-400"
                      : "bg-red-900/40 text-red-400"
                  )}
                >
                  {p.signal_type}
                </span>
              </td>
              <td className="py-2 pr-4">{fmt(p.entry_price)}</td>
              <td className="py-2 pr-4">{fmt(p.fill_price)}</td>
              <td className="py-2 pr-4">{p.quantity}</td>
              <td className="py-2 pr-4 text-red-400">{fmt(p.stop_loss_price)}</td>
              <td className="py-2 pr-4 text-emerald-400">{fmt(p.target_price)}</td>
              <td className="py-2 pr-4 text-gray-400">{p.product}</td>
              <td className="py-2 pr-4 text-gray-400">{fmt(p.slippage)}</td>
              <td className="py-2 text-xs text-gray-400">{p.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
