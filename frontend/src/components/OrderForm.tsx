import { useState } from "react";
import clsx from "clsx";
import { usePlaceOrder } from "../hooks/queries";
import type { ManualOrder } from "../types/api";

/**
 * Inline order form. Lifted out of HoldingsPage so SymbolPage can
 * reuse the same UI when the user wants to act on whatever they're
 * looking at without bouncing through Holdings or Kite.
 */
export function OrderForm({
  defaultSymbol,
  defaultSide,
  isLocked,
  onClose,
}: {
  defaultSymbol?: string;
  defaultSide?: "BUY" | "SELL";
  isLocked?: boolean;
  onClose: () => void;
}) {
  const placeOrder = usePlaceOrder();
  const [symbol, setSymbol] = useState(defaultSymbol || "");
  const [side, setSide] = useState<"BUY" | "SELL">(defaultSide || "BUY");
  const [quantity, setQuantity] = useState("");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [product, setProduct] = useState<"CNC" | "MIS">("CNC");
  const [price, setPrice] = useState("");
  const [result, setResult] = useState<string | null>(null);

  const handleSubmit = () => {
    const order: ManualOrder = {
      symbol: symbol.toUpperCase(),
      side,
      quantity: Number(quantity),
      order_type: orderType,
      product,
    };
    if (orderType === "LIMIT" && price) {
      order.price = Number(price);
    }
    placeOrder.mutate(order, {
      onSuccess: (res) => {
        if (res.success) {
          setResult(`Order placed: ${res.order_id}`);
          setTimeout(onClose, 2000);
        } else {
          setResult(`Failed: ${res.error}`);
        }
      },
    });
  };

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-gray-200">Place Order</h3>
        <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-sm">
          Cancel
        </button>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Symbol</label>
          <input
            type="text"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Side</label>
          <div className="flex gap-1">
            <button
              onClick={() => setSide("BUY")}
              className={clsx(
                "flex-1 py-1.5 rounded text-sm font-medium transition-colors",
                side === "BUY"
                  ? "bg-emerald-600 text-white"
                  : "bg-gray-900 text-gray-400 hover:text-gray-200"
              )}
            >
              BUY
            </button>
            <button
              onClick={() => setSide("SELL")}
              className={clsx(
                "flex-1 py-1.5 rounded text-sm font-medium transition-colors",
                side === "SELL"
                  ? "bg-red-600 text-white"
                  : "bg-gray-900 text-gray-400 hover:text-gray-200"
              )}
            >
              SELL
            </button>
          </div>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Quantity</label>
          <input
            type="number"
            min={1}
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Type</label>
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as "MARKET" | "LIMIT")}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none"
          >
            <option value="MARKET">Market</option>
            <option value="LIMIT">Limit</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Product</label>
          <select
            value={product}
            onChange={(e) => setProduct(e.target.value as "CNC" | "MIS")}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none"
          >
            <option value="CNC">CNC (Delivery)</option>
            <option value="MIS">MIS (Intraday)</option>
          </select>
        </div>
        {orderType === "LIMIT" && (
          <div>
            <label className="block text-xs text-gray-500 mb-1">Price</label>
            <input
              type="number"
              step="0.05"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none"
            />
          </div>
        )}
      </div>

      {isLocked && side === "SELL" && (
        <div className="bg-amber-900/20 border border-amber-800 rounded px-3 py-2 text-xs text-amber-400">
          This holding is locked. Automated selling is disabled, but you can still place a manual sell order.
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={handleSubmit}
          disabled={!symbol || !quantity || Number(quantity) <= 0 || placeOrder.isPending}
          className={clsx(
            "px-4 py-1.5 rounded text-sm font-medium disabled:opacity-40 transition-colors",
            side === "BUY"
              ? "bg-emerald-600 hover:bg-emerald-700 text-white"
              : "bg-red-600 hover:bg-red-700 text-white"
          )}
        >
          {placeOrder.isPending
            ? "Placing..."
            : `${side} ${symbol || "..."} x${quantity || 0}`}
        </button>
        {result && (
          <span
            className={clsx(
              "text-xs",
              result.startsWith("Order") ? "text-emerald-400" : "text-red-400"
            )}
          >
            {result}
          </span>
        )}
      </div>
    </div>
  );
}
