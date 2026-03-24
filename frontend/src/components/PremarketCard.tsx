import { usePremarket } from "../hooks/queries";
import clsx from "clsx";

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function PremarketCard() {
  const { data, isLoading } = usePremarket();

  if (isLoading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-24 animate-pulse" />
    );
  }

  if (!data?.date) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-1">Pre-Market</h3>
        <p className="text-gray-500 text-xs">No pre-market data available</p>
      </div>
    );
  }

  const bias = data.market_bias?.toLowerCase() || "neutral";
  const biasColor =
    bias === "bullish"
      ? "text-emerald-400"
      : bias === "bearish"
        ? "text-red-400"
        : "text-gray-400";

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-2">Pre-Market</h3>
      <div className="grid grid-cols-3 gap-3">
        <div>
          <p className="text-xs text-gray-500">GIFT Nifty</p>
          <p
            className={clsx(
              "text-sm font-semibold",
              data.gift_nifty_change_pct != null &&
                data.gift_nifty_change_pct >= 0
                ? "text-emerald-400"
                : "text-red-400"
            )}
          >
            {data.gift_nifty_change_pct != null
              ? `${data.gift_nifty_change_pct >= 0 ? "+" : ""}${fmt(data.gift_nifty_change_pct)}%`
              : "—"}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Bias</p>
          <p className={clsx("text-sm font-semibold capitalize", biasColor)}>
            {data.market_bias || "—"}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Date</p>
          <p className="text-sm text-gray-300">{data.date}</p>
        </div>
      </div>
    </div>
  );
}
