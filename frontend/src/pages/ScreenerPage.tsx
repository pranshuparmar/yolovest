import { useState } from "react";
import clsx from "clsx";

import { SymbolLink } from "../components/SymbolLink";
import {
  useAddUserWatchlistSymbol,
  useCreateAlert,
  useReviewHoldings,
} from "../hooks/queries";
import { fmt } from "../utils/format";

const ACTION_CLS: Record<string, string> = {
  BUY: "bg-emerald-900/40 text-emerald-400",
  BUY_MORE: "bg-emerald-900/40 text-emerald-400",
  SELL: "bg-red-900/40 text-red-400",
  SHORT: "bg-red-900/40 text-red-400",
  TIGHTEN_SL: "bg-amber-900/40 text-amber-400",
  HOLD: "bg-gray-700 text-gray-300",
};

type Reco = {
  symbol: string;
  action: string;
  confidence: number;
  reasoning: string;
  last_price: number;
  target_price?: number;
  stop_loss_price?: number;
  target_pct?: number | null;
  sl_pct?: number | null;
  day_change_pct?: number | null;
  week_change_pct?: number | null;
  vol_ratio?: number | null;
};

function pct(v: number | null | undefined, color = false) {
  if (v == null) return <span className="text-gray-600">—</span>;
  return (
    <span className={clsx(color && (v >= 0 ? "text-emerald-400" : "text-red-400"))}>
      {v >= 0 ? "+" : ""}
      {v.toFixed(1)}%
    </span>
  );
}

const actionBtn =
  "text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-300 hover:bg-gray-700 disabled:opacity-60 whitespace-nowrap";

// Each row owns its own Watch / Alert mutation state, so success on one symbol
// doesn't light up the others.
function ScreenerRow({ r }: { r: Reco }) {
  const addWatchlist = useAddUserWatchlistSymbol();
  const createAlert = useCreateAlert();
  const alertPrice = r.target_price ?? r.last_price;

  return (
    <tr className="border-b border-gray-800/50 hover:bg-gray-800/30 align-top">
      <td className="py-2 px-3 font-medium">
        <SymbolLink symbol={r.symbol} className="text-gray-200" />
      </td>
      <td className="py-2 px-3 text-center">
        <span
          className={clsx(
            "px-1.5 py-0.5 rounded text-[10px] font-medium",
            ACTION_CLS[r.action] ?? "bg-gray-700 text-gray-300",
          )}
        >
          {r.action.replace("_", " ")}
        </span>
      </td>
      <td className="py-2 px-3 text-right">
        <span
          className={clsx(
            "font-medium",
            r.confidence >= 0.7
              ? "text-emerald-400"
              : r.confidence >= 0.5
                ? "text-amber-400"
                : "text-gray-400",
          )}
        >
          {(r.confidence * 100).toFixed(0)}%
        </span>
      </td>
      <td className="py-2 px-3 text-right font-mono text-xs">
        <div className="text-gray-300">₹{fmt(r.last_price)}</div>
        <div>{pct(r.day_change_pct, true)}</div>
      </td>
      <td className="py-2 px-3 text-right font-mono text-xs">
        {r.target_price != null ? (
          <span className="text-emerald-400">
            ₹{fmt(r.target_price)}{" "}
            <span className="text-[10px] text-emerald-400/70">{pct(r.target_pct)}</span>
          </span>
        ) : (
          <span className="text-gray-600">—</span>
        )}
      </td>
      <td className="py-2 px-3 text-right font-mono text-xs">
        {r.stop_loss_price != null ? (
          <span className="text-red-400">
            ₹{fmt(r.stop_loss_price)}{" "}
            <span className="text-[10px] text-red-400/70">{pct(r.sl_pct)}</span>
          </span>
        ) : (
          <span className="text-gray-600">—</span>
        )}
      </td>
      <td className="py-2 px-3 text-right font-mono text-xs">
        {pct(r.week_change_pct, true)}
      </td>
      <td className="py-2 px-3 text-right font-mono text-xs text-gray-300">
        {r.vol_ratio != null ? `${r.vol_ratio.toFixed(1)}×` : "—"}
      </td>
      <td className="py-2 px-3 text-xs text-gray-400 max-w-[16rem]">{r.reasoning}</td>
      <td className="py-2 px-3 text-center whitespace-nowrap">
        <div className="flex items-center justify-center gap-1">
          <button
            type="button"
            onClick={() => addWatchlist.mutate({ symbol: r.symbol })}
            disabled={addWatchlist.isPending || addWatchlist.isSuccess}
            className={actionBtn}
            title="Add to your watchlist"
          >
            {addWatchlist.isSuccess ? "★" : "☆ Watch"}
          </button>
          <button
            type="button"
            onClick={() => {
              if (!alertPrice) return;
              const direction = alertPrice >= r.last_price ? "above" : "below";
              createAlert.mutate({ symbol: r.symbol, target_price: alertPrice, direction });
            }}
            disabled={createAlert.isPending || createAlert.isSuccess || !alertPrice}
            className={actionBtn}
            title={
              r.target_price != null
                ? `Alert at target ₹${fmt(r.target_price)}`
                : "Alert at the current price"
            }
          >
            {createAlert.isSuccess ? "🔔 set" : "🔔 Alert"}
          </button>
        </div>
      </td>
    </tr>
  );
}

export function ScreenerPage() {
  const [input, setInput] = useState("");
  const review = useReviewHoldings();

  const scan = () => {
    const symbols = Array.from(
      new Set(
        input
          .split(/[\s,]+/)
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
      ),
    );
    if (symbols.length) review.mutate(symbols);
  };

  const recos = (review.data?.recommendations ?? []) as Reco[];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold text-gray-100">Screener</h2>
        <p className="text-sm text-gray-500 mt-1">
          Run the ML review across a list of symbols and compare them side by
          side. Works for any NSE symbol — not just your tracked universe (those
          are fetched on demand, so a large list may take a few seconds).
        </p>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-3">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") scan();
          }}
          placeholder="Paste symbols separated by spaces, commas, or new lines — e.g. RELIANCE, TCS, INFY, HDFCBANK"
          rows={3}
          className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono focus:outline-none focus:border-emerald-500"
        />
        <div className="flex items-center gap-3">
          <button
            onClick={scan}
            disabled={review.isPending || !input.trim()}
            className="px-4 py-2 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
          >
            {review.isPending ? "Scanning…" : "Scan"}
          </button>
          <span className="text-xs text-gray-500">⌘/Ctrl + Enter to scan</span>
        </div>
      </div>

      {review.isError && (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-sm text-red-400">
          Scan failed — try again.
        </div>
      )}

      {recos.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-800 text-sm font-semibold text-gray-300">
            {recos.length} symbol{recos.length > 1 ? "s" : ""} — ranked by signal,
            then confidence
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-center">Action</th>
                  <th className="py-2 px-3 text-right">Conf.</th>
                  <th className="py-2 px-3 text-right">Last · Day</th>
                  <th className="py-2 px-3 text-right">Target</th>
                  <th className="py-2 px-3 text-right">SL</th>
                  <th className="py-2 px-3 text-right">7d</th>
                  <th className="py-2 px-3 text-right">Vol</th>
                  <th className="py-2 px-3 text-left">Why</th>
                  <th className="py-2 px-3 text-center">Actions</th>
                </tr>
              </thead>
              <tbody>
                {recos.map((r) => (
                  <ScreenerRow key={r.symbol} r={r} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
