import type { WatchlistItem } from "../types/api";
import { parseUTC, getTimezone } from "../utils/datetime";

function score(v: number | null) {
  if (v === null) return "—";
  return v.toFixed(2);
}

export function WatchlistTable({ items }: { items: WatchlistItem[] }) {
  if (items.length === 0) {
    return <p className="text-gray-500 text-sm py-4">Watchlist is empty</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-4">Symbol</th>
            <th className="pb-2 pr-4">Composite</th>
            <th className="pb-2 pr-4">Technical</th>
            <th className="pb-2 pr-4">Volume</th>
            <th className="pb-2 pr-4">Sentiment</th>
            <th className="pb-2 pr-4">Fundamental</th>
            <th className="pb-2 pr-4">Sector</th>
            <th className="pb-2">Updated</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr
              key={item.symbol}
              className="border-b border-gray-800/50 hover:bg-gray-800/30"
            >
              <td className="py-2 pr-4 font-medium">{item.symbol}</td>
              <td className="py-2 pr-4 text-emerald-400 font-medium">
                {score(item.composite_score)}
              </td>
              <td className="py-2 pr-4">{score(item.technical_score)}</td>
              <td className="py-2 pr-4">{score(item.volume_momentum_score)}</td>
              <td className="py-2 pr-4">{score(item.news_sentiment_score)}</td>
              <td className="py-2 pr-4">{score(item.fundamental_score)}</td>
              <td className="py-2 pr-4 text-gray-400">{item.sector || "—"}</td>
              <td className="py-2 text-xs text-gray-500">
                {parseUTC(item.updated_at).toLocaleDateString("en-IN", { timeZone: getTimezone() })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
