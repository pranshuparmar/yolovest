import { useState } from "react";
import { useHoldings, useLockHolding, useUnlockHolding, useBulkLockHoldings, useReviewHoldings, useTightenSl, useClosePosition } from "../hooks/queries";
import { useLtpStream } from "../hooks/useLtpStream";
import clsx from "clsx";
import { OrderForm } from "../components/OrderForm";
import { SymbolLink } from "../components/SymbolLink";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function fmtInr(n: number) {
  return "\u20B9" + fmt(n);
}


export function HoldingsPage() {
  const { data: response, isLoading, isError, error, refetch, isFetching } = useHoldings();
  const ltps = useLtpStream();
  const [orderForm, setOrderForm] = useState<{
    symbol?: string;
    side?: "BUY" | "SELL";
    locked?: boolean;
  } | null>(null);

  const lockHolding = useLockHolding();
  const unlockHolding = useUnlockHolding();
  const bulkLock = useBulkLockHoldings();
  const review = useReviewHoldings();
  const tightenSl = useTightenSl();
  const closePosition = useClosePosition();

  // Inline dialog state: which recommendation row is currently editing
  // its SL, what value the user has typed, and any in-flight error.
  // Keyed by symbol because the recommendations panel is symbol-keyed.
  const [tightenTarget, setTightenTarget] = useState<{
    symbol: string;
    tradeId: string;
    currentSl: number;
    ltp: number;
    entry: number;
    direction: "BUY" | "SELL";
    inputValue: string;
  } | null>(null);
  const [tightenError, setTightenError] = useState<string | null>(null);

  // Partial-close dialog state. Default suggested qty is 50% of
  // current position, floored at 1. User can edit before applying.
  const [partialCloseTarget, setPartialCloseTarget] = useState<{
    symbol: string;
    tradeId: string;
    fullQty: number;
    ltp: number;
    entry: number;
    direction: "BUY" | "SELL";
    inputValue: string;
  } | null>(null);
  const [partialCloseError, setPartialCloseError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  type Rec = { symbol: string; held: boolean; quantity: number; average_price: number; last_price: number; pnl_pct: number; action: string; confidence: number; signal_type: string; reasoning: string; target_price?: number; stop_loss_price?: number; trade_id?: string | null; current_sl?: number; trade_signal_type?: string | null; entry_price?: number };

  const toggleSelect = (sym: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sym)) next.delete(sym); else next.add(sym);
      return next;
    });
  };
  const toggleAll = () => {
    if (!holdings) return;
    if (selected.size === holdings.length) setSelected(new Set());
    else setSelected(new Set(holdings.map((h) => h.tradingsymbol)));
  };

  const holdings = response?.holdings;
  const brokerAuthenticated = response?.broker_authenticated ?? true;
  const loginUrl = response?.login_url;

  const totalInvestment = holdings?.reduce(
    (sum, h) => sum + h.quantity * h.average_price,
    0
  ) ?? 0;
  const totalCurrent = holdings?.reduce(
    (sum, h) => sum + h.quantity * h.last_price,
    0
  ) ?? 0;
  const totalPnl = totalCurrent - totalInvestment;
  const totalPnlPct = totalInvestment > 0 ? (totalPnl / totalInvestment) * 100 : 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Holdings</h2>
          <p className="text-sm text-gray-500 mt-0.5">
            Your Zerodha portfolio holdings (CNC/delivery)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="px-3 py-1.5 rounded text-sm font-medium bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors disabled:opacity-50"
          >
            {isFetching ? "Refreshing..." : "Refresh"}
          </button>
          <button
            onClick={() => review.mutate(selected.size > 0 ? [...selected] : undefined)}
            disabled={review.isPending}
            className="px-3 py-1.5 rounded text-sm font-medium bg-blue-600 hover:bg-blue-700 text-white transition-colors disabled:opacity-50"
          >
            {review.isPending ? "Reviewing..." : selected.size > 0 ? `Review ${selected.size} Selected` : "Review All"}
          </button>
          <button
            onClick={() => setOrderForm({})}
            className="px-3 py-1.5 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white transition-colors"
          >
            New Order
          </button>
        </div>
      </div>

      {/* Order form */}
      {orderForm && (
        <OrderForm
          defaultSymbol={orderForm.symbol}
          defaultSide={orderForm.side}
          isLocked={orderForm.locked}
          onClose={() => setOrderForm(null)}
        />
      )}

      {/* Tighten-SL modal — applies via the right execution path
          (GTT modify for CNC, sl_order modify for MIS, DB-only for
          legacy client-side) and refuses to widen the SL. */}
      {tightenTarget && (
        <div
          className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
          onClick={() => !tightenSl.isPending && setTightenTarget(null)}
        >
          <div
            className="bg-gray-900 border border-amber-800/50 rounded-lg max-w-sm w-full p-5 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold text-amber-400">
                Tighten Stop Loss — {tightenTarget.symbol}
              </h3>
              <button
                onClick={() => setTightenTarget(null)}
                disabled={tightenSl.isPending}
                className="text-gray-500 hover:text-gray-300 disabled:opacity-50"
              >×</button>
            </div>

            <div className="grid grid-cols-3 gap-3 text-xs">
              <div>
                <div className="text-gray-500">Entry</div>
                <div className="font-mono text-gray-200 mt-0.5">
                  {tightenTarget.entry > 0 ? fmt(tightenTarget.entry) : "—"}
                </div>
              </div>
              <div>
                <div className="text-gray-500">LTP</div>
                <div className="font-mono text-gray-200 mt-0.5">
                  {tightenTarget.ltp > 0 ? fmt(tightenTarget.ltp) : "—"}
                </div>
              </div>
              <div>
                <div className="text-gray-500">Current SL</div>
                <div className="font-mono text-amber-400 mt-0.5">
                  {fmt(tightenTarget.currentSl)}
                </div>
              </div>
            </div>

            <div>
              <label className="block text-xs text-gray-500 mb-1">
                New SL ({tightenTarget.direction === "BUY"
                  ? "must be > current"
                  : "must be < current"})
              </label>
              <input
                type="number"
                step="0.05"
                value={tightenTarget.inputValue}
                onChange={(e) => {
                  setTightenTarget({ ...tightenTarget, inputValue: e.target.value });
                  setTightenError(null);
                }}
                disabled={tightenSl.isPending}
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono focus:outline-none focus:border-amber-500 disabled:opacity-50"
                autoFocus
              />
              <p className="text-[11px] text-gray-600 mt-1">
                {tightenTarget.direction === "BUY"
                  ? "Raises the SL closer to LTP, locking in more of the unrealised gain."
                  : "Lowers the SL closer to LTP, locking in more of the unrealised gain."}
              </p>
            </div>

            {tightenError && (
              <div className="text-xs text-red-400 bg-red-900/20 border border-red-800/50 rounded px-3 py-2">
                {tightenError}
              </div>
            )}

            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={() => setTightenTarget(null)}
                disabled={tightenSl.isPending}
                className="px-3 py-1.5 rounded text-xs bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-50"
              >Cancel</button>
              <button
                onClick={async () => {
                  const newSl = parseFloat(tightenTarget.inputValue);
                  if (!Number.isFinite(newSl) || newSl <= 0) {
                    setTightenError("Enter a valid positive price");
                    return;
                  }
                  if (tightenTarget.direction === "BUY" && newSl <= tightenTarget.currentSl) {
                    setTightenError(`New SL must be above ${fmt(tightenTarget.currentSl)} to tighten`);
                    return;
                  }
                  if (tightenTarget.direction === "SELL" && newSl >= tightenTarget.currentSl) {
                    setTightenError(`New SL must be below ${fmt(tightenTarget.currentSl)} to tighten`);
                    return;
                  }
                  try {
                    await tightenSl.mutateAsync({ tradeId: tightenTarget.tradeId, newSl });
                    setTightenTarget(null);
                    review.reset();  // hide stale recommendation
                  } catch (e) {
                    setTightenError(e instanceof Error ? e.message : String(e));
                  }
                }}
                disabled={tightenSl.isPending}
                className="px-3 py-1.5 rounded text-xs bg-amber-700 text-white hover:bg-amber-600 disabled:opacity-50"
              >{tightenSl.isPending ? "Applying…" : "Apply"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Partial-close modal — books a portion of the position at market.
          Backend resizes the broker-side GTT / SL to the remaining qty. */}
      {partialCloseTarget && (
        <div
          className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
          onClick={() => !closePosition.isPending && setPartialCloseTarget(null)}
        >
          <div
            className="bg-gray-900 border border-emerald-800/50 rounded-lg max-w-sm w-full p-5 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold text-emerald-400">
                Book Partial Profit — {partialCloseTarget.symbol}
              </h3>
              <button
                onClick={() => setPartialCloseTarget(null)}
                disabled={closePosition.isPending}
                className="text-gray-500 hover:text-gray-300 disabled:opacity-50"
              >×</button>
            </div>

            <div className="grid grid-cols-3 gap-3 text-xs">
              <div>
                <div className="text-gray-500">Entry</div>
                <div className="font-mono text-gray-200 mt-0.5">
                  {partialCloseTarget.entry > 0 ? fmt(partialCloseTarget.entry) : "—"}
                </div>
              </div>
              <div>
                <div className="text-gray-500">LTP</div>
                <div className="font-mono text-gray-200 mt-0.5">
                  {partialCloseTarget.ltp > 0 ? fmt(partialCloseTarget.ltp) : "—"}
                </div>
              </div>
              <div>
                <div className="text-gray-500">Holding</div>
                <div className="font-mono text-gray-200 mt-0.5">
                  {partialCloseTarget.fullQty}
                </div>
              </div>
            </div>

            <div>
              <label className="block text-xs text-gray-500 mb-1">
                Close quantity (1–{partialCloseTarget.fullQty - 1})
              </label>
              <input
                type="number"
                min={1}
                max={partialCloseTarget.fullQty - 1}
                step={1}
                value={partialCloseTarget.inputValue}
                onChange={(e) => {
                  setPartialCloseTarget({ ...partialCloseTarget, inputValue: e.target.value });
                  setPartialCloseError(null);
                }}
                disabled={closePosition.isPending}
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono focus:outline-none focus:border-emerald-500 disabled:opacity-50"
                autoFocus
              />
              {(() => {
                const exitQty = parseInt(partialCloseTarget.inputValue, 10);
                if (!Number.isFinite(exitQty) || exitQty <= 0) return null;
                const ltp = partialCloseTarget.ltp;
                const entry = partialCloseTarget.entry;
                if (ltp <= 0 || entry <= 0) return null;
                const estGross = partialCloseTarget.direction === "BUY"
                  ? (ltp - entry) * exitQty
                  : (entry - ltp) * exitQty;
                const remaining = partialCloseTarget.fullQty - exitQty;
                return (
                  <p className="text-[11px] text-gray-500 mt-1.5">
                    Est. realised PnL @ LTP: <span className={clsx("font-mono", estGross >= 0 ? "text-emerald-400" : "text-red-400")}>
                      {estGross >= 0 ? "+" : ""}₹{fmt(estGross)}
                    </span> (before costs). {remaining} share{remaining === 1 ? "" : "s"} stay open.
                  </p>
                );
              })()}
            </div>

            {partialCloseError && (
              <div className="text-xs text-red-400 bg-red-900/20 border border-red-800/50 rounded px-3 py-2">
                {partialCloseError}
              </div>
            )}

            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={() => setPartialCloseTarget(null)}
                disabled={closePosition.isPending}
                className="px-3 py-1.5 rounded text-xs bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-50"
              >Cancel</button>
              <button
                onClick={async () => {
                  const exitQty = parseInt(partialCloseTarget.inputValue, 10);
                  if (!Number.isFinite(exitQty) || exitQty < 1) {
                    setPartialCloseError("Quantity must be a positive integer");
                    return;
                  }
                  if (exitQty >= partialCloseTarget.fullQty) {
                    setPartialCloseError(
                      `For a full exit use the Close button on the Positions page; ` +
                      `partial close requires qty < ${partialCloseTarget.fullQty}`,
                    );
                    return;
                  }
                  try {
                    const r = await closePosition.mutateAsync({
                      tradeId: partialCloseTarget.tradeId,
                      qty: exitQty,
                    });
                    setPartialCloseTarget(null);
                    review.reset();  // hide stale recommendation
                    window.alert(
                      `Booked ${r.exit_qty}/${partialCloseTarget.fullQty} ` +
                      `${partialCloseTarget.symbol} @ ₹${(r.exit_price ?? 0).toFixed(2)} — ` +
                      `realised ₹${(r.partial_pnl ?? 0).toLocaleString("en-IN")}. ` +
                      `${r.remaining_qty} shares still open.`,
                    );
                  } catch (e) {
                    const detail = (e as { detail?: unknown })?.detail;
                    if (
                      detail &&
                      typeof detail === "object" &&
                      (detail as { error_type?: string }).error_type === "cdsl_tpin_required"
                    ) {
                      const d = detail as { auth_url?: string; hint?: string; error?: string };
                      setPartialCloseError(
                        `${d.hint ?? d.error ?? "CDSL TPIN authorisation required."} ` +
                        `Open ${d.auth_url ?? "Kite"} in a new tab, authorise, then retry.`,
                      );
                      if (d.auth_url) {
                        window.open(d.auth_url, "_blank", "noopener,noreferrer");
                      }
                      return;
                    }
                    setPartialCloseError(e instanceof Error ? e.message : String(e));
                  }
                }}
                disabled={closePosition.isPending}
                className="px-3 py-1.5 rounded text-xs bg-emerald-700 text-white hover:bg-emerald-600 disabled:opacity-50"
              >{closePosition.isPending ? "Booking…" : "Book"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Recommendations panel */}
      {review.data && review.data.recommendations.length > 0 && (
        <div className="bg-gray-900 border border-blue-800/50 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-blue-800/30 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-blue-400">ML Recommendations</h3>
            <button onClick={() => review.reset()} className="text-xs text-gray-500 hover:text-gray-300">Dismiss</button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-center">Action</th>
                  <th className="py-2 px-3 text-right">Confidence</th>
                  <th className="py-2 px-3 text-right">P&L</th>
                  <th className="py-2 px-3 text-left">Reasoning</th>
                  <th className="py-2 px-3 text-center">Act</th>
                </tr>
              </thead>
              <tbody>
                {review.data.recommendations.map((r: Rec) => (
                  <tr key={r.symbol} className="border-b border-gray-800/50 hover:bg-gray-800/20">
                    <td className="py-2 px-3 font-medium text-gray-200">
                      <SymbolLink symbol={r.symbol} className="text-gray-200" />
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={clsx("px-1.5 py-0.5 rounded text-xs font-medium", {
                        "bg-red-900/40 text-red-400": r.action === "SELL" || r.action === "SHORT",
                        "bg-emerald-900/40 text-emerald-400": r.action === "BUY_MORE" || r.action === "BUY",
                        "bg-amber-900/40 text-amber-400": r.action === "TIGHTEN_SL",
                        "bg-gray-800 text-gray-400": r.action === "HOLD",
                      })}>{r.action.replace("_", " ")}</span>
                      {!r.held && <span className="text-[10px] text-gray-600 ml-1">not held</span>}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">{(r.confidence * 100).toFixed(0)}%</td>
                    <td className={clsx("py-2 px-3 text-right font-mono", r.pnl_pct >= 0 ? "text-emerald-400" : "text-red-400")}>
                      {r.pnl_pct >= 0 ? "+" : ""}{r.pnl_pct.toFixed(1)}%
                    </td>
                    <td className="py-2 px-3 text-gray-400 text-xs max-w-xs">{r.reasoning}</td>
                    <td className="py-2 px-3 text-center">
                      {(r.action === "SELL" || r.action === "SHORT") && (
                        <button
                          onClick={() => setOrderForm({ symbol: r.symbol, side: "SELL" })}
                          className="px-2 py-0.5 rounded text-xs bg-red-900/40 text-red-400 hover:bg-red-800/50"
                        >Sell</button>
                      )}
                      {(r.action === "BUY_MORE" || r.action === "BUY") && (
                        <button
                          onClick={() => setOrderForm({ symbol: r.symbol, side: "BUY" })}
                          className="px-2 py-0.5 rounded text-xs bg-emerald-900/40 text-emerald-400 hover:bg-emerald-800/50"
                        >Buy</button>
                      )}
                      {r.action === "TIGHTEN_SL" && (!r.trade_id || !r.current_sl || r.current_sl <= 0) && (
                        <span className="text-[11px] text-gray-600" title="No system-tracked trade for this symbol — adopt it via positions or set an SL manually at Kite.">
                          not tracked
                        </span>
                      )}
                      {r.action === "TIGHTEN_SL" && r.trade_id && r.current_sl && r.current_sl > 0 && (
                        <div className="inline-flex gap-1.5">
                          <button
                            onClick={() => {
                              const direction = (r.trade_signal_type === "SELL" ? "SELL" : "BUY") as "BUY" | "SELL";
                              const ltp = r.last_price || 0;
                              const currentSl = r.current_sl ?? 0;
                              // Suggested new SL = midpoint of LTP and current SL,
                              // floored at entry for BUY (lock in at least breakeven)
                              // and capped at entry for SELL.
                              const entry = r.entry_price ?? 0;
                              let suggested: number;
                              if (direction === "BUY") {
                                suggested = Math.max(entry || currentSl, (ltp + currentSl) / 2);
                              } else {
                                suggested = Math.min(entry || currentSl, (ltp + currentSl) / 2);
                              }
                              setTightenTarget({
                                symbol: r.symbol,
                                tradeId: r.trade_id!,
                                currentSl,
                                ltp,
                                entry,
                                direction,
                                inputValue: suggested.toFixed(2),
                              });
                              setTightenError(null);
                            }}
                            className="px-2 py-0.5 rounded text-xs bg-amber-900/40 text-amber-400 hover:bg-amber-800/50"
                          >Tighten SL</button>
                          {r.quantity > 1 && (
                            <button
                              onClick={() => {
                                const direction = (r.trade_signal_type === "SELL" ? "SELL" : "BUY") as "BUY" | "SELL";
                                // Default: half the position (floored to 1).
                                const suggestedQty = Math.max(1, Math.floor(r.quantity / 2));
                                setPartialCloseTarget({
                                  symbol: r.symbol,
                                  tradeId: r.trade_id!,
                                  fullQty: r.quantity,
                                  ltp: r.last_price || 0,
                                  entry: r.entry_price ?? r.average_price ?? 0,
                                  direction,
                                  inputValue: String(suggestedQty),
                                });
                                setPartialCloseError(null);
                              }}
                              className="px-2 py-0.5 rounded text-xs bg-emerald-900/40 text-emerald-400 hover:bg-emerald-800/50"
                            >Book Partial</button>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Broker auth expired banner */}
      {!brokerAuthenticated && (
        <div className="bg-amber-900/30 border border-amber-700/50 rounded-lg p-4 flex items-start gap-3">
          <span className="text-amber-400 text-lg shrink-0">!</span>
          <div>
            <p className="text-amber-300 font-medium text-sm">
              Kite session expired
            </p>
            <p className="text-gray-400 text-xs mt-1">
              Your Zerodha token has expired. Re-authenticate to view holdings and enable trading.
            </p>
            <div className="flex gap-2 mt-2">
              <a
                href="/integrations"
                className="px-3 py-1 rounded text-xs font-medium bg-amber-700/50 hover:bg-amber-700 text-amber-200 transition-colors"
              >
                Go to Settings
              </a>
              {loginUrl && (
                <a
                  href={loginUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-3 py-1 rounded text-xs font-medium bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
                >
                  Kite Login
                </a>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Summary cards */}
      {holdings && holdings.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">Holdings</p>
            <p className="text-lg font-bold text-gray-100">{holdings.length}</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">Invested</p>
            <p className="text-lg font-bold text-gray-100">{fmtInr(totalInvestment)}</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">Current</p>
            <p className="text-lg font-bold text-gray-100">{fmtInr(totalCurrent)}</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">P&L</p>
            <p
              className={clsx(
                "text-lg font-bold",
                totalPnl >= 0 ? "text-emerald-400" : "text-red-400"
              )}
            >
              {totalPnl >= 0 ? "+" : ""}
              {fmtInr(totalPnl)}{" "}
              <span className="text-sm">({totalPnlPct >= 0 ? "+" : ""}{fmt(totalPnlPct)}%)</span>
            </p>
          </div>
        </div>
      )}

      {/* Holdings table */}
      {isError ? (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-4 text-sm text-red-400">
          Failed to fetch holdings{error instanceof Error ? `: ${error.message}` : ""}. Zerodha token may be expired — re-authenticate on the{" "}
          <a href="/integrations" className="underline text-red-300 hover:text-red-200">Settings</a> page.
        </div>
      ) : isLoading ? (
        <div className="h-48 animate-pulse bg-gray-900 rounded-lg" />
      ) : !holdings || holdings.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-500 text-sm">
            No holdings found. Authenticate with Zerodha on the Integrations page to see your portfolio.
          </p>
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          {selected.size > 0 && (
            <div className="px-4 py-2 bg-blue-900/20 border-b border-blue-800/50 flex items-center gap-3">
              <span className="text-xs text-blue-300">{selected.size} selected</span>
              <button
                onClick={() => {
                  bulkLock.mutate({ symbols: [...selected], action: "lock" }, { onSuccess: () => setSelected(new Set()) });
                }}
                disabled={bulkLock.isPending}
                className="px-2 py-0.5 rounded text-xs bg-amber-900/50 text-amber-400 hover:bg-amber-800 disabled:opacity-50"
              >Lock Selected</button>
              <button
                onClick={() => {
                  bulkLock.mutate({ symbols: [...selected], action: "unlock" }, { onSuccess: () => setSelected(new Set()) });
                }}
                disabled={bulkLock.isPending}
                className="px-2 py-0.5 rounded text-xs bg-gray-700 text-gray-300 hover:bg-gray-600 disabled:opacity-50"
              >Unlock Selected</button>
              <button
                onClick={() => setSelected(new Set())}
                className="px-2 py-0.5 rounded text-xs text-gray-500 hover:text-gray-300"
              >Clear</button>
            </div>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-2 text-center w-8">
                    <input type="checkbox" checked={holdings.length > 0 && selected.size === holdings.length} onChange={toggleAll} className="rounded bg-gray-800 border-gray-600" />
                  </th>
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-right">Qty</th>
                  <th className="py-2 px-3 text-right">Avg Price</th>
                  <th className="py-2 px-3 text-right">LTP</th>
                  <th className="py-2 px-3 text-right">P&L</th>
                  <th className="py-2 px-3 text-right">Day Change</th>
                  <th className="py-2 px-3 text-center">Lock</th>
                  <th className="py-2 px-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {holdings.map((h) => {
                  // Prefer live WebSocket LTP; fall back to last_price
                  // from the REST snapshot. PnL recalculates as ticks
                  // arrive so the column stays current without a refetch.
                  const ltp = ltps.get(h.tradingsymbol) ?? h.last_price;
                  const pnl = (ltp - h.average_price) * h.quantity;
                  const pnlPct =
                    h.average_price > 0
                      ? ((ltp - h.average_price) / h.average_price) * 100
                      : 0;

                  return (
                    <tr
                      key={h.tradingsymbol}
                      className="border-b border-gray-800/50 hover:bg-gray-800/30"
                    >
                      <td className="py-2.5 px-2 text-center w-8">
                        <input type="checkbox" checked={selected.has(h.tradingsymbol)} onChange={() => toggleSelect(h.tradingsymbol)} className="rounded bg-gray-800 border-gray-600" />
                      </td>
                      <td className="py-2.5 px-3">
                        <span className="font-medium">
                          <SymbolLink symbol={h.tradingsymbol} className="text-gray-200" />
                        </span>
                        <span className="text-xs text-gray-600 ml-1">
                          {h.exchange}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono text-gray-300">
                        {h.quantity}
                      </td>
                      <td className="py-2.5 px-3 text-right text-gray-400">
                        {fmtInr(h.average_price)}
                      </td>
                      <td className="py-2.5 px-3 text-right text-gray-300 font-mono">
                        {fmtInr(ltp)}
                      </td>
                      <td
                        className={clsx(
                          "py-2.5 px-3 text-right font-mono",
                          pnl >= 0 ? "text-emerald-400" : "text-red-400"
                        )}
                      >
                        {pnl >= 0 ? "+" : ""}
                        {fmtInr(pnl)}
                        <span className="text-xs ml-1 opacity-70">
                          ({pnlPct >= 0 ? "+" : ""}{fmt(pnlPct, 1)}%)
                        </span>
                      </td>
                      <td
                        className={clsx(
                          "py-2.5 px-3 text-right text-xs",
                          h.day_change_percentage >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        )}
                      >
                        {h.day_change_percentage >= 0 ? "+" : ""}
                        {fmt(h.day_change_percentage, 1)}%
                      </td>
                      <td className="py-2.5 px-3 text-center">
                        <button
                          onClick={() =>
                            h.locked
                              ? unlockHolding.mutate(h.tradingsymbol)
                              : lockHolding.mutate(h.tradingsymbol)
                          }
                          disabled={lockHolding.isPending || unlockHolding.isPending}
                          title={h.locked ? "Unlock — allow YoloVest to sell" : "Lock — prevent YoloVest from selling"}
                          className={clsx(
                            "px-2 py-0.5 rounded text-xs font-medium transition-colors disabled:opacity-50",
                            h.locked
                              ? "bg-amber-900/40 text-amber-400 hover:bg-amber-800/50"
                              : "bg-gray-800 text-gray-500 hover:bg-gray-700 hover:text-gray-300"
                          )}
                        >
                          {h.locked ? "Locked" : "Lock"}
                        </button>
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            onClick={() =>
                              setOrderForm({
                                symbol: h.tradingsymbol,
                                side: "BUY",
                              })
                            }
                            className="px-2 py-0.5 rounded text-xs font-medium bg-emerald-900/30 text-emerald-400 hover:bg-emerald-800/50 transition-colors"
                          >
                            Buy
                          </button>
                          <button
                            onClick={() =>
                              setOrderForm({
                                symbol: h.tradingsymbol,
                                side: "SELL",
                                locked: h.locked,
                              })
                            }
                            className="px-2 py-0.5 rounded text-xs font-medium bg-red-900/30 text-red-400 hover:bg-red-800/50 transition-colors"
                          >
                            Sell
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
