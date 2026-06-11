import { useState } from "react";
import { usePendingTrades, useApprovePendingTrade, useRejectPendingTrade, useClearTodaysSignals } from "../hooks/queries";
import { useLtpStream } from "../hooks/useLtpStream";
import clsx from "clsx";
import { SymbolLink } from "./SymbolLink";
import { formatPriceMovePct, priceMovePct } from "../utils/priceMove";
import { fmt } from "../utils/format";

// Approximate exit-by date for the pending banner: add `days` trading days
// (skipping weekends) to today. Calendar-only — the engine counts real
// trading days incl. holidays, hence the "≈".
function holdLabel(days?: number | null, period?: string | null): { text: string; date: string } | null {
  if ((days == null || Number.isNaN(days)) && !period) return null;
  if (period === "intraday" || (days != null && days <= 0)) {
    return { text: "Intraday", date: "today" };
  }
  const n = days && days > 0 ? days : 1;
  const d = new Date();
  let added = 0;
  while (added < n) {
    d.setDate(d.getDate() + 1);
    const wd = d.getDay();
    if (wd !== 0 && wd !== 6) added++;
  }
  return { text: `${n}d hold`, date: d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }) };
}

interface Override {
  [key: string]: unknown;
  signal_type?: string;
  entry_price?: number;
  target_price?: number;
  stop_loss_price?: number;
  product?: string;
}

function OverrideRow({
  trade,
  onApprove,
  onCancel,
  isPending,
}: {
  trade: { id: number; symbol: string; signal_type: string; entry_price: number; target_price: number; stop_loss_price: number; product: string; position_size: number; confidence_score: number };
  onApprove: (overrides: Override) => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  const [signalType, setSignalType] = useState(trade.signal_type);
  const [entry, setEntry] = useState(trade.entry_price);
  const [target, setTarget] = useState(trade.target_price);
  const [sl, setSl] = useState(trade.stop_loss_price);
  const [product, setProduct] = useState(trade.product || "MIS");
  const [qty, setQty] = useState(trade.position_size);

  const hasChanges =
    signalType !== trade.signal_type ||
    entry !== trade.entry_price ||
    target !== trade.target_price ||
    sl !== trade.stop_loss_price ||
    product !== trade.product ||
    qty !== trade.position_size;

  const handleApprove = () => {
    const overrides: Override = {};
    if (signalType !== trade.signal_type) overrides.signal_type = signalType;
    if (entry !== trade.entry_price) overrides.entry_price = entry;
    if (target !== trade.target_price) overrides.target_price = target;
    if (sl !== trade.stop_loss_price) overrides.stop_loss_price = sl;
    if (qty !== trade.position_size) (overrides as Record<string, unknown>).position_size = qty;
    if (product !== trade.product) overrides.product = product;
    onApprove(Object.keys(overrides).length > 0 ? overrides : {});
  };

  return (
    <tr className="border-b border-amber-800/30 bg-amber-950/20">
      <td className="py-2 px-3 font-medium text-amber-300">
        <SymbolLink symbol={trade.symbol} className="text-amber-300" />
      </td>
      <td className="py-2 px-3 text-right font-mono text-gray-300">
        {(trade.confidence_score * 100).toFixed(0)}%
      </td>
      <td className="py-2 px-3 text-center">
        <select
          value={signalType}
          onChange={(e) => setSignalType(e.target.value)}
          className={clsx(
            "px-1.5 py-0.5 rounded text-xs font-medium border-0 cursor-pointer",
            signalType === "BUY" ? "bg-emerald-900/40 text-emerald-400" : "bg-red-900/40 text-red-400",
            signalType !== trade.signal_type && "ring-1 ring-amber-400",
          )}
        >
          <option value="BUY">BUY</option>
          <option value="SELL">SELL</option>
        </select>
      </td>
      <td className="py-2 px-3 text-center">
        <select
          value={product}
          onChange={(e) => setProduct(e.target.value)}
          className={clsx(
            "px-1.5 py-0.5 rounded text-xs font-medium border-0 cursor-pointer",
            product === "MIS" ? "bg-blue-900/40 text-blue-400" : "bg-purple-900/40 text-purple-400",
            product !== trade.product && "ring-1 ring-amber-400",
          )}
        >
          <option value="MIS">MIS</option>
          <option value="CNC">CNC</option>
        </select>
      </td>
      <td className="py-2 px-3">
        <input
          type="number"
          step="0.05"
          value={entry}
          onChange={(e) => setEntry(parseFloat(e.target.value) || 0)}
          className={clsx(
            "w-20 bg-gray-800 rounded px-1.5 py-0.5 text-xs text-right text-gray-200 font-mono focus:outline-none focus:border-blue-500 border",
            entry !== trade.entry_price ? "border-amber-500" : "border-gray-700",
          )}
        />
      </td>
      {/* LTP column placeholder — only the read row populates it; edit
          mode keeps the slot for alignment. */}
      <td className="py-2 px-3 text-right text-gray-600 font-mono">—</td>
      <td className="py-2 px-3">
        <input
          type="number"
          step="0.05"
          value={target}
          onChange={(e) => setTarget(parseFloat(e.target.value) || 0)}
          className={clsx(
            "w-20 bg-gray-800 rounded px-1.5 py-0.5 text-xs text-right text-emerald-400 font-mono focus:outline-none focus:border-blue-500 border",
            target !== trade.target_price ? "border-amber-500" : "border-gray-700",
          )}
        />
      </td>
      <td className="py-2 px-3">
        <input
          type="number"
          step="0.05"
          value={sl}
          onChange={(e) => setSl(parseFloat(e.target.value) || 0)}
          className={clsx(
            "w-20 bg-gray-800 rounded px-1.5 py-0.5 text-xs text-right text-red-400 font-mono focus:outline-none focus:border-blue-500 border",
            sl !== trade.stop_loss_price ? "border-amber-500" : "border-gray-700",
          )}
        />
      </td>
      <td className="py-2 px-3">
        <input
          type="number"
          step="1"
          min="1"
          value={qty}
          onChange={(e) => setQty(parseInt(e.target.value, 10) || 1)}
          className={clsx(
            "w-16 bg-gray-800 rounded px-1.5 py-0.5 text-xs text-right text-gray-200 font-mono focus:outline-none focus:border-blue-500 border",
            qty !== trade.position_size ? "border-amber-500" : "border-gray-700",
          )}
        />
      </td>
      <td className="py-2 px-3 text-right font-mono text-gray-300">
        {"₹"}{fmt(entry * qty, 0)}
      </td>
      {/* Exit-by column placeholder — read row populates it. */}
      <td className="py-2 px-3 text-right text-gray-600">—</td>
      <td className="py-2 px-3 text-center">
        <div className="flex items-center justify-center gap-1.5">
          <button
            onClick={handleApprove}
            disabled={isPending}
            className={clsx(
              "px-2 py-1 rounded text-xs font-medium text-white disabled:opacity-50 transition-colors",
              hasChanges ? "bg-amber-600 hover:bg-amber-700" : "bg-emerald-600 hover:bg-emerald-700",
            )}
          >
            {isPending ? "..." : hasChanges ? "Override & Approve" : "Approve"}
          </button>
          <button
            onClick={onCancel}
            className="px-2 py-1 rounded text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
          >
            Cancel
          </button>
        </div>
      </td>
    </tr>
  );
}

export function ClearSignalsButton() {
  const clearSignals = useClearTodaysSignals();
  return (
    <button
      onClick={() => {
        if (!window.confirm("Clear today's signals and pending trades? Next heartbeat will regenerate fresh signals.")) return;
        clearSignals.mutate(undefined, {
          onSuccess: (data) => {
            alert(`Cleared ${data.signals_deleted} signals and ${data.pending_deleted} pending trades. Next heartbeat will regenerate.`);
          },
        });
      }}
      disabled={clearSignals.isPending}
      className="px-2.5 py-1 rounded text-xs font-medium bg-gray-700 hover:bg-gray-600 text-gray-300 disabled:opacity-50 transition-colors"
    >
      {clearSignals.isPending ? "Clearing..." : "Clear & Regenerate Signals"}
    </button>
  );
}

export function PendingTradesBanner() {
  const { data: pending } = usePendingTrades();
  const approve = useApprovePendingTrade();
  const reject = useRejectPendingTrade();
  const [editingId, setEditingId] = useState<number | null>(null);
  const ltps = useLtpStream();

  if (!pending || pending.length === 0) return null;

  const totalQty = pending.reduce((sum, t) => sum + t.position_size, 0);
  const totalInvestment = pending.reduce(
    (sum, t) => sum + t.entry_price * t.position_size,
    0,
  );

  const handleApproveAll = () => {
    if (!window.confirm(`Approve all ${pending.length} pending trades?`)) return;
    for (const t of pending) approve.mutate({ tradeId: t.id });
  };

  const handleRejectAll = () => {
    if (!window.confirm(`Reject all ${pending.length} pending trades?`)) return;
    for (const t of pending) reject.mutate(t.id);
  };

  return (
    <div className="bg-amber-900/20 border border-amber-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 flex flex-wrap items-center justify-between gap-2 border-b border-amber-800/50">
        <div className="flex items-center gap-x-3 gap-y-1 flex-wrap">
          <span className="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse" />
          <h3 className="text-sm font-semibold text-amber-400">
            {pending.length} trade{pending.length > 1 ? "s" : ""} awaiting approval
          </h3>
          <span className="text-xs text-gray-500">·</span>
          <span className="text-xs text-gray-400">
            Total qty <span className="text-gray-200 font-medium">{totalQty}</span>
          </span>
          <span className="text-xs text-gray-500">·</span>
          <span className="text-xs text-gray-400">
            Investment{" "}
            <span className="text-amber-300 font-semibold font-mono">
              {"₹"}{fmt(totalInvestment, 0)}
            </span>
          </span>
          <span className="text-xs text-gray-500 w-full md:w-auto">Click Edit to override before approving</span>
        </div>
        <div className="flex items-center gap-2">
          {pending.length > 1 && (
            <>
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
            </>
          )}
          <ClearSignalsButton />
        </div>
      </div>

      <div className="overflow-x-auto max-h-80 overflow-y-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-amber-800/30 sticky top-0 bg-gray-900/90">
              <th className="py-2 px-3 text-left">Symbol</th>
              <th className="py-2 px-3 text-right">Conf</th>
              <th className="py-2 px-3 text-center">Signal</th>
              <th className="py-2 px-3 text-center">Product</th>
              <th className="py-2 px-3 text-right">Entry</th>
              <th className="py-2 px-3 text-right">LTP</th>
              <th className="py-2 px-3 text-right">Target</th>
              <th className="py-2 px-3 text-right">SL</th>
              <th className="py-2 px-3 text-right">Qty</th>
              <th className="py-2 px-3 text-right">Investment</th>
              <th className="py-2 px-3 text-right">Exit by</th>
              <th className="py-2 px-3 text-center">Actions</th>
            </tr>
          </thead>
          <tbody>
            {pending.map((t) =>
              editingId === t.id ? (
                <OverrideRow
                  key={t.id}
                  trade={t}
                  onApprove={(overrides) => {
                    approve.mutate(
                      { tradeId: t.id, overrides: Object.keys(overrides).length > 0 ? overrides : undefined },
                      { onSuccess: () => setEditingId(null) },
                    );
                  }}
                  onCancel={() => setEditingId(null)}
                  isPending={approve.isPending}
                />
              ) : (
                <tr key={t.id} className="border-b border-gray-800/30 hover:bg-gray-800/20">
                  <td className="py-2 px-3 font-medium text-gray-200">
                    <SymbolLink symbol={t.symbol} className="text-gray-200" />
                  </td>
                  <td className="py-2 px-3 text-right font-mono text-gray-300">
                    {(t.confidence_score * 100).toFixed(0)}%
                  </td>
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
                  <td className="py-2 px-3 text-center">
                    <span
                      className={clsx(
                        "px-1.5 py-0.5 rounded text-xs font-medium",
                        t.product === "MIS"
                          ? "bg-blue-900/40 text-blue-400"
                          : "bg-purple-900/40 text-purple-400"
                      )}
                    >
                      {t.product || "MIS"}
                    </span>
                  </td>
                  <td className="py-2 px-3 text-right font-mono text-gray-300">{fmt(t.entry_price)}</td>
                  <td className="py-2 px-3 text-right font-mono">
                    {(() => {
                      const ltp = ltps.get(t.symbol);
                      if (!ltp) return <span className="text-gray-600">—</span>;
                      const drift = ((ltp - t.entry_price) / t.entry_price) * 100;
                      // Favorable direction is signed: a BUY is happy
                      // when price rises, a SELL when it drops.
                      const favorable = t.signal_type === "BUY" ? drift > 0 : drift < 0;
                      const cls =
                        Math.abs(drift) < 0.25 ? "text-gray-300"
                        : favorable ? "text-emerald-400" : "text-red-400";
                      return (
                        <span className={cls} title={`${drift >= 0 ? "+" : ""}${drift.toFixed(2)}% vs entry`}>
                          {fmt(ltp)}
                        </span>
                      );
                    })()}
                  </td>
                  <td className="py-2 px-3 text-right font-mono text-emerald-400">
                    {fmt(t.target_price)}
                    <span className="ml-1 text-[10px] text-emerald-400/70">
                      {formatPriceMovePct(priceMovePct(t.entry_price, t.target_price, t.signal_type))}
                    </span>
                  </td>
                  <td className="py-2 px-3 text-right font-mono text-red-400">
                    {fmt(t.stop_loss_price)}
                    <span className="ml-1 text-[10px] text-red-400/70">
                      {formatPriceMovePct(priceMovePct(t.entry_price, t.stop_loss_price, t.signal_type))}
                    </span>
                  </td>
                  <td className="py-2 px-3 text-right text-gray-400">{t.position_size}</td>
                  <td className="py-2 px-3 text-right font-mono text-gray-300">
                    {"₹"}{fmt(t.entry_price * t.position_size, 0)}
                  </td>
                  <td className="py-2 px-3 text-right whitespace-nowrap">
                    {(() => {
                      const h = holdLabel(t.expected_holding_days, t.expected_holding_period);
                      if (!h) return <span className="text-gray-600">—</span>;
                      return (
                        <div>
                          <div className="text-gray-300 text-xs">{h.text}</div>
                          <div className="text-[10px] text-gray-500">≈ {h.date}</div>
                        </div>
                      );
                    })()}
                  </td>
                  <td className="py-2 px-3 text-center">
                    <div className="flex items-center justify-center gap-1.5">
                      <button
                        onClick={() => approve.mutate({ tradeId: t.id })}
                        disabled={approve.isPending}
                        className="px-2 py-1 rounded text-xs bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => setEditingId(t.id)}
                        className="px-2 py-1 rounded text-xs bg-amber-700 hover:bg-amber-600 text-white transition-colors"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => reject.mutate(t.id)}
                        disabled={reject.isPending}
                        className="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 transition-colors"
                      >
                        Reject
                      </button>
                    </div>
                  </td>
                </tr>
              )
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
