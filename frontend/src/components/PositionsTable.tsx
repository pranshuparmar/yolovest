import { Link } from "react-router-dom";
import clsx from "clsx";
import type { Trade } from "../types/api";
import { useClosePosition } from "../hooks/queries";
import { useLtpStream } from "../hooks/useLtpStream";
import { formatPriceMovePct, priceMovePct } from "../utils/priceMove";
import { fmt } from "../utils/format";

export function PositionsTable({ positions }: { positions: Trade[] }) {
  const close = useClosePosition();
  // Subscribing here re-renders the table on every tick frame for any
  // symbol — but the backend throttles to ≤1 per symbol per second so
  // total render rate stays bounded.
  const ltps = useLtpStream();

  if (positions.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No open positions</p>;
  }

  const handleClose = (p: Trade) => {
    if (
      !window.confirm(
        `Close ${p.signal_type} ${p.symbol} x${p.quantity} (${p.product}) at market?`,
      )
    )
      return;
    close.mutate({ tradeId: p.trade_id }, {
      onSuccess: (r) => {
        const exitPx = r.exit_price ?? 0;
        const pnl = r.pnl ?? 0;
        window.alert(
          `Closed ${p.symbol} at ₹${exitPx.toFixed(2)} — PnL ₹${pnl.toLocaleString("en-IN")}`,
        );
      },
      onError: (err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        // CDSL TPIN required (FastAPI 412 wraps the auth response in
        // `detail`). Walk the user through the authorisation flow
        // instead of dumping a raw error string.
        const detail = (err as { detail?: unknown })?.detail;
        if (
          detail &&
          typeof detail === "object" &&
          (detail as { error_type?: string }).error_type === "cdsl_tpin_required"
        ) {
          const d = detail as {
            auth_url?: string;
            ddpi_help_url?: string;
            hint?: string;
            error?: string;
          };
          const openAuth = window.confirm(
            `${d.hint ?? d.error ?? msg}\n\n` +
            `Click OK to open the CDSL TPIN auth page in a new tab. ` +
            `After authorising, click Close again to retry.`,
          );
          if (openAuth && d.auth_url) {
            window.open(d.auth_url, "_blank", "noopener,noreferrer");
          }
          return;
        }
        window.alert(`Close failed: ${msg}`);
      },
    });
  };

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-4">Symbol</th>
            <th className="pb-2 pr-4">Type</th>
            <th className="pb-2 pr-4">Entry</th>
            <th className="pb-2 pr-4">Fill</th>
            <th className="pb-2 pr-4">LTP</th>
            <th className="pb-2 pr-4">Move %</th>
            <th className="pb-2 pr-4">Qty</th>
            <th className="pb-2 pr-4">SL</th>
            <th className="pb-2 pr-4">Target</th>
            <th className="pb-2 pr-4">Product</th>
            <th className="pb-2 pr-4">Slippage</th>
            <th className="pb-2 pr-4">Status</th>
            <th className="pb-2">Actions</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr
              key={p.trade_id}
              className="border-b border-gray-800/50 hover:bg-gray-800/30"
            >
              <td className="py-2 pr-4 font-medium">
                <Link to={`/symbol/${p.symbol}`} className="text-emerald-400 hover:underline">{p.symbol}</Link>
              </td>
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
              {(() => {
                const ltp = ltps.get(p.symbol);
                if (!ltp || !p.fill_price) {
                  return (
                    <>
                      <td className="py-2 pr-4 text-gray-600">—</td>
                      <td className="py-2 pr-4 text-gray-600">—</td>
                    </>
                  );
                }
                const move =
                  p.signal_type === "BUY"
                    ? (ltp - p.fill_price) / p.fill_price * 100
                    : (p.fill_price - ltp) / p.fill_price * 100;
                const moveCls =
                  move > 0 ? "text-emerald-400" : move < 0 ? "text-red-400" : "text-gray-400";
                return (
                  <>
                    <td className="py-2 pr-4 font-mono">{fmt(ltp)}</td>
                    <td className={clsx("py-2 pr-4", moveCls)}>
                      {move >= 0 ? "+" : ""}{fmt(move)}%
                    </td>
                  </>
                );
              })()}
              <td className="py-2 pr-4">{p.quantity}</td>
              <td className="py-2 pr-4 text-red-400">
                {fmt(p.stop_loss_price)}
                <span className="ml-1 text-xs text-red-400/70">
                  {formatPriceMovePct(priceMovePct(p.entry_price, p.stop_loss_price, p.signal_type))}
                </span>
              </td>
              <td className="py-2 pr-4 text-emerald-400">
                {fmt(p.target_price)}
                <span className="ml-1 text-xs text-emerald-400/70">
                  {formatPriceMovePct(priceMovePct(p.entry_price, p.target_price, p.signal_type))}
                </span>
              </td>
              <td className="py-2 pr-4 text-gray-400">{p.product}</td>
              <td className="py-2 pr-4 text-gray-400">{fmt(p.slippage)}</td>
              <td className="py-2 pr-4 text-xs text-gray-400">{p.status}</td>
              <td className="py-2">
                <button
                  onClick={() => handleClose(p)}
                  disabled={close.isPending}
                  className="px-2 py-1 rounded text-xs font-medium bg-red-700 hover:bg-red-600 text-white disabled:opacity-40 transition-colors"
                  title="Exit this position at market"
                >
                  {close.isPending ? "..." : "Close"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
