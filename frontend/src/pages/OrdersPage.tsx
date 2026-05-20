import { useMemo, useState } from "react";
import clsx from "clsx";
import {
  useBrokerOrders,
  useCancelBrokerOrder,
  useModifyBrokerOrder,
  useCancelBrokerGtt,
} from "../hooks/queries";
import { SymbolLink } from "../components/SymbolLink";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

const TERMINAL_STATUSES = new Set(["COMPLETE", "CANCELLED", "REJECTED"]);

function statusColor(s: string): string {
  if (s === "COMPLETE") return "bg-emerald-900/40 text-emerald-400";
  if (s === "REJECTED") return "bg-red-900/40 text-red-400";
  if (s === "CANCELLED") return "bg-gray-700 text-gray-400";
  if (s === "OPEN") return "bg-blue-900/40 text-blue-400";
  if (s === "TRIGGER PENDING") return "bg-amber-900/40 text-amber-400";
  return "bg-gray-800 text-gray-400";
}

type Order = Record<string, unknown> & {
  order_id?: string;
  tradingsymbol?: string;
  transaction_type?: string;
  order_type?: string;
  product?: string;
  quantity?: number;
  filled_quantity?: number;
  price?: number;
  trigger_price?: number;
  average_price?: number;
  status?: string;
  order_timestamp?: string;
};

type Gtt = Record<string, unknown> & {
  id?: number;
  status?: string;
  condition?: {
    tradingsymbol?: string;
    last_price?: number;
    trigger_values?: number[];
  };
  orders?: Array<{
    transaction_type?: string;
    quantity?: number;
    price?: number;
    product?: string;
    order_type?: string;
  }>;
};

export function OrdersPage() {
  const { data, isLoading, refetch, isFetching } = useBrokerOrders();
  const cancelOrder = useCancelBrokerOrder();
  const modifyOrder = useModifyBrokerOrder();
  const cancelGtt = useCancelBrokerGtt();

  // Modify dialog state. Single source per row.
  const [modifyTarget, setModifyTarget] = useState<{
    order: Order;
    price: string;
    quantity: string;
    trigger_price: string;
  } | null>(null);
  const [modifyError, setModifyError] = useState<string | null>(null);

  const { openOrders, executed } = useMemo(() => {
    const orders = (data?.orders ?? []) as Order[];
    const openOrders: Order[] = [];
    const executed: Order[] = [];
    for (const o of orders) {
      if (TERMINAL_STATUSES.has(String(o.status || "").toUpperCase())) {
        executed.push(o);
      } else {
        openOrders.push(o);
      }
    }
    return { openOrders, executed };
  }, [data]);

  const gtts = (data?.gtts ?? []) as Gtt[];

  if (isLoading) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Orders</h2>
        <div className="h-40 animate-pulse bg-gray-800 rounded" />
      </div>
    );
  }

  if (!data?.authenticated) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Orders</h2>
        <div className="bg-amber-900/30 border border-amber-700/50 rounded-lg p-4 text-sm text-amber-300">
          Kite is not authenticated. Re-authenticate via Integrations to view the order book.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Orders</h2>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="px-3 py-1.5 rounded text-xs bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-50"
        >
          {isFetching ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {data.error && (
        <div className="bg-red-900/30 border border-red-700/50 rounded-lg p-3 text-xs text-red-300">
          {data.error}
        </div>
      )}

      {/* GTT block */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-gray-200">
            GTT Orders ({gtts.length})
          </h3>
        </div>
        {gtts.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-gray-500">
            No active GTT orders
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-center">Status</th>
                  <th className="py-2 px-3 text-right">LTP</th>
                  <th className="py-2 px-3 text-right">Trigger(s)</th>
                  <th className="py-2 px-3 text-right">Qty</th>
                  <th className="py-2 px-3 text-center">Act</th>
                </tr>
              </thead>
              <tbody>
                {gtts.map((g) => (
                  <tr key={g.id} className="border-b border-gray-800/50">
                    <td className="py-2 px-3 font-medium text-gray-200">
                      <SymbolLink symbol={g.condition?.tradingsymbol || "—"} className="text-gray-200" />
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={clsx("px-1.5 py-0.5 rounded text-xs font-medium", statusColor(String(g.status || "").toUpperCase()))}>
                        {String(g.status || "—")}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {g.condition?.last_price ? fmt(g.condition.last_price) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {(g.condition?.trigger_values ?? []).map((v) => fmt(v)).join(" / ") || "—"}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {(g.orders ?? []).map((o) => o.quantity).join(" / ") || "—"}
                    </td>
                    <td className="py-2 px-3 text-center">
                      <button
                        onClick={() => {
                          if (!g.id) return;
                          if (!window.confirm(`Delete GTT #${g.id}? Position-monitor will fall back to client-side detection.`)) return;
                          cancelGtt.mutate(g.id);
                        }}
                        disabled={cancelGtt.isPending}
                        className="px-2 py-0.5 rounded text-xs bg-red-900/40 text-red-400 hover:bg-red-800/50 disabled:opacity-50"
                      >Cancel</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Open orders block */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-gray-200">
            Open Orders ({openOrders.length})
          </h3>
        </div>
        {openOrders.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-gray-500">
            No open orders
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-3 text-left">Time</th>
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-center">Side</th>
                  <th className="py-2 px-3 text-left">Type</th>
                  <th className="py-2 px-3 text-left">Product</th>
                  <th className="py-2 px-3 text-right">Qty</th>
                  <th className="py-2 px-3 text-right">Price</th>
                  <th className="py-2 px-3 text-right">Trigger</th>
                  <th className="py-2 px-3 text-center">Status</th>
                  <th className="py-2 px-3 text-center">Act</th>
                </tr>
              </thead>
              <tbody>
                {openOrders.map((o) => (
                  <tr key={o.order_id} className="border-b border-gray-800/50 hover:bg-gray-800/20">
                    <td className="py-2 px-3 text-xs text-gray-500 font-mono">
                      {String(o.order_timestamp || "").slice(11, 19) || "—"}
                    </td>
                    <td className="py-2 px-3 font-medium text-gray-200">
                      <SymbolLink symbol={o.tradingsymbol || ""} className="text-gray-200" />
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={clsx(
                        "px-1.5 py-0.5 rounded text-xs font-medium",
                        o.transaction_type === "BUY"
                          ? "bg-emerald-900/40 text-emerald-400"
                          : "bg-red-900/40 text-red-400",
                      )}>{o.transaction_type}</span>
                    </td>
                    <td className="py-2 px-3 text-xs text-gray-400">{o.order_type}</td>
                    <td className="py-2 px-3 text-xs text-gray-400">{o.product}</td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {o.filled_quantity ?? 0} / {o.quantity ?? 0}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {o.price ? fmt(o.price) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {o.trigger_price ? fmt(o.trigger_price) : "—"}
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={clsx("px-1.5 py-0.5 rounded text-xs font-medium", statusColor(String(o.status || "").toUpperCase()))}>
                        {String(o.status || "—")}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-center">
                      <div className="inline-flex gap-1.5">
                        <button
                          onClick={() => {
                            setModifyTarget({
                              order: o,
                              price: o.price ? String(o.price) : "",
                              quantity: o.quantity ? String(o.quantity) : "",
                              trigger_price: o.trigger_price ? String(o.trigger_price) : "",
                            });
                            setModifyError(null);
                          }}
                          className="px-2 py-0.5 rounded text-xs bg-blue-900/40 text-blue-400 hover:bg-blue-800/50"
                        >Modify</button>
                        <button
                          onClick={() => {
                            if (!o.order_id) return;
                            if (!window.confirm(`Cancel order ${o.order_id}?`)) return;
                            cancelOrder.mutate(o.order_id);
                          }}
                          disabled={cancelOrder.isPending}
                          className="px-2 py-0.5 rounded text-xs bg-red-900/40 text-red-400 hover:bg-red-800/50 disabled:opacity-50"
                        >Cancel</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Today's executed/cancelled — readonly */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-gray-200">
            Today's Activity ({executed.length})
          </h3>
        </div>
        {executed.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-gray-500">
            No orders yet today
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-3 text-left">Time</th>
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-center">Side</th>
                  <th className="py-2 px-3 text-left">Type</th>
                  <th className="py-2 px-3 text-right">Qty</th>
                  <th className="py-2 px-3 text-right">Avg Price</th>
                  <th className="py-2 px-3 text-center">Status</th>
                </tr>
              </thead>
              <tbody>
                {executed.map((o) => (
                  <tr key={o.order_id} className="border-b border-gray-800/50">
                    <td className="py-2 px-3 text-xs text-gray-500 font-mono">
                      {String(o.order_timestamp || "").slice(11, 19) || "—"}
                    </td>
                    <td className="py-2 px-3 font-medium text-gray-200">
                      <SymbolLink symbol={o.tradingsymbol || ""} className="text-gray-200" />
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={clsx(
                        "px-1.5 py-0.5 rounded text-xs font-medium",
                        o.transaction_type === "BUY"
                          ? "bg-emerald-900/40 text-emerald-400"
                          : "bg-red-900/40 text-red-400",
                      )}>{o.transaction_type}</span>
                    </td>
                    <td className="py-2 px-3 text-xs text-gray-400">{o.order_type}</td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {o.filled_quantity ?? 0} / {o.quantity ?? 0}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">
                      {o.average_price ? fmt(o.average_price) : "—"}
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={clsx("px-1.5 py-0.5 rounded text-xs font-medium", statusColor(String(o.status || "").toUpperCase()))}>
                        {String(o.status || "—")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Modify dialog */}
      {modifyTarget && (
        <div
          className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
          onClick={() => !modifyOrder.isPending && setModifyTarget(null)}
        >
          <div
            className="bg-gray-900 border border-blue-800/50 rounded-lg max-w-sm w-full p-5 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold text-blue-400">
                Modify {modifyTarget.order.tradingsymbol} order
              </h3>
              <button
                onClick={() => setModifyTarget(null)}
                disabled={modifyOrder.isPending}
                className="text-gray-500 hover:text-gray-300 disabled:opacity-50"
              >×</button>
            </div>

            <div className="text-xs text-gray-500 space-y-0.5">
              <p>Order ID: <span className="font-mono text-gray-300">{modifyTarget.order.order_id}</span></p>
              <p>Type: <span className="text-gray-300">{modifyTarget.order.order_type}</span> · Product: <span className="text-gray-300">{modifyTarget.order.product}</span></p>
              <p>Status: <span className="text-gray-300">{String(modifyTarget.order.status || "")}</span></p>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Price</label>
                <input
                  type="number"
                  step="0.05"
                  value={modifyTarget.price}
                  onChange={(e) => setModifyTarget({ ...modifyTarget, price: e.target.value })}
                  disabled={modifyOrder.isPending}
                  className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 font-mono focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Quantity</label>
                <input
                  type="number"
                  min={1}
                  value={modifyTarget.quantity}
                  onChange={(e) => setModifyTarget({ ...modifyTarget, quantity: e.target.value })}
                  disabled={modifyOrder.isPending}
                  className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 font-mono focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Trigger</label>
                <input
                  type="number"
                  step="0.05"
                  value={modifyTarget.trigger_price}
                  onChange={(e) => setModifyTarget({ ...modifyTarget, trigger_price: e.target.value })}
                  disabled={modifyOrder.isPending}
                  className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 font-mono focus:outline-none focus:border-blue-500"
                />
              </div>
            </div>

            <p className="text-[11px] text-gray-600">
              Leave a field blank or unchanged to skip it. Tick rounding is applied
              automatically at the broker.
            </p>

            {modifyError && (
              <div className="text-xs text-red-400 bg-red-900/20 border border-red-800/50 rounded px-3 py-2">
                {modifyError}
              </div>
            )}

            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={() => setModifyTarget(null)}
                disabled={modifyOrder.isPending}
                className="px-3 py-1.5 rounded text-xs bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-50"
              >Cancel</button>
              <button
                onClick={async () => {
                  const body: { price?: number; quantity?: number; trigger_price?: number } = {};
                  const orig = modifyTarget.order;
                  const newPrice = parseFloat(modifyTarget.price);
                  const newQty = parseInt(modifyTarget.quantity, 10);
                  const newTrig = parseFloat(modifyTarget.trigger_price);
                  if (Number.isFinite(newPrice) && newPrice !== (orig.price ?? 0)) {
                    body.price = newPrice;
                  }
                  if (Number.isFinite(newQty) && newQty !== (orig.quantity ?? 0)) {
                    body.quantity = newQty;
                  }
                  if (Number.isFinite(newTrig) && newTrig !== (orig.trigger_price ?? 0)) {
                    body.trigger_price = newTrig;
                  }
                  if (Object.keys(body).length === 0) {
                    setModifyError("No changes to apply");
                    return;
                  }
                  if (!orig.order_id) return;
                  try {
                    await modifyOrder.mutateAsync({ orderId: orig.order_id, ...body });
                    setModifyTarget(null);
                  } catch (e) {
                    setModifyError(e instanceof Error ? e.message : String(e));
                  }
                }}
                disabled={modifyOrder.isPending}
                className="px-3 py-1.5 rounded text-xs bg-blue-700 text-white hover:bg-blue-600 disabled:opacity-50"
              >{modifyOrder.isPending ? "Applying…" : "Apply"}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
