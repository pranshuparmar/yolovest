import { useState, useMemo } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import {
  useSymbolOHLCV,
  useSymbolTrades,
  useSymbolPredictions,
  useSentiment,
  useNews,
} from "../hooks/queries";
import {
  ComposedChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  BarChart, Bar, CartesianGrid, Scatter, Cell,
} from "recharts";
import clsx from "clsx";
import { parseUTC, getTimezone } from "../utils/datetime";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function SymbolPage() {
  const { symbol } = useParams<{ symbol: string }>();
  const navigate = useNavigate();
  const sym = symbol?.toUpperCase() || "";
  const [days, setDays] = useState(60);

  const { data: ohlcv, isLoading: ohlcvLoading } = useSymbolOHLCV(sym, { days });
  const { data: trades, isLoading: tradesLoading } = useSymbolTrades(sym);
  const { data: predictions } = useSymbolPredictions(sym);
  const { data: sentiment } = useSentiment(sym);
  const { data: news } = useNews({ symbol: sym, limit: 10 });
  const ct = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  // Build chart data with trade entry/exit overlays
  const chartData = useMemo(() => {
    // Build trade lookup by date
    const entryMap = new Map<string, { price: number; type: string }>();
    const exitMap = new Map<string, { price: number; type: string; pnl: number | null }>();
    for (const t of trades || []) {
      const entryDate = parseUTC(t.created_at).toLocaleDateString("en-IN", { timeZone: getTimezone(), month: "short", day: "numeric" });
      entryMap.set(entryDate, { price: t.fill_price, type: t.signal_type });
      if (t.closed_at && t.exit_price != null) {
        const exitDate = parseUTC(t.closed_at).toLocaleDateString("en-IN", { timeZone: getTimezone(), month: "short", day: "numeric" });
        exitMap.set(exitDate, { price: t.exit_price, type: t.signal_type, pnl: t.pnl });
      }
    }

    return (ohlcv || []).map((b) => {
      const date = parseUTC(b.timestamp).toLocaleDateString("en-IN", { timeZone: getTimezone(), month: "short", day: "numeric" });
      const entry = entryMap.get(date);
      const exit = exitMap.get(date);
      return {
        date,
        close: b.close,
        volume: b.volume,
        high: b.high,
        low: b.low,
        entryPrice: entry?.price ?? null,
        entryType: entry?.type ?? null,
        exitPrice: exit?.price ?? null,
        exitPnl: exit?.pnl ?? null,
      };
    });
  }, [ohlcv, trades]);

  const lastPrice = chartData.length > 0 ? chartData[chartData.length - 1].close : null;
  const firstPrice = chartData.length > 0 ? chartData[0].close : null;
  const changePct = firstPrice && lastPrice ? ((lastPrice - firstPrice) / firstPrice) * 100 : 0;

  const sentColor = sentiment?.sentiment === "bullish" ? "text-emerald-400" : sentiment?.sentiment === "bearish" ? "text-red-400" : "text-gray-400";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <button onClick={() => navigate(-1)} className="text-gray-500 hover:text-gray-300 text-sm">&larr; Back</button>
        <h2 className="text-lg font-semibold">{sym}</h2>
        {lastPrice != null && (
          <span className="text-lg font-semibold">₹{fmt(lastPrice)}</span>
        )}
        {changePct !== 0 && (
          <span className={clsx("text-sm font-medium", changePct >= 0 ? "text-emerald-400" : "text-red-400")}>
            {changePct >= 0 ? "+" : ""}{fmt(changePct, 1)}%
          </span>
        )}
        {sentiment && (
          <span className={clsx("text-xs font-medium ml-2", sentColor)}>
            {sentiment.sentiment} ({Math.round(sentiment.confidence * 100)}%)
          </span>
        )}
      </div>

      {/* Period selector */}
      <div className="flex gap-2">
        {[30, 60, 90, 180, 365].map((d) => (
          <button key={d} onClick={() => setDays(d)}
            className={clsx("px-3 py-1 text-xs rounded", days === d ? "bg-emerald-900/40 text-emerald-400" : "bg-gray-800 text-gray-400 hover:bg-gray-700")}
          >{d}d</button>
        ))}
      </div>

      {/* Price chart */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Price Chart</h3>
        {ohlcvLoading ? (
          <div className="h-64 animate-pulse bg-gray-800 rounded" />
        ) : chartData.length === 0 ? (
          <p className="text-gray-500 text-sm py-8 text-center">No OHLCV data available</p>
        ) : (
          <>
          <ResponsiveContainer width="100%" height={280}>
            <ComposedChart data={chartData}>
              <defs>
                <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3fb950" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#3fb950" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={ct.grid} />
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis domain={["auto", "auto"]} tick={{ fontSize: 10, fill: ct.tick }} />
              <Tooltip
                contentStyle={tooltipStyle}
                formatter={(value: number, name: string) => {
                  if (name === "entryPrice") return [`₹${fmt(value)}`, "Entry"];
                  if (name === "exitPrice") return [`₹${fmt(value)}`, "Exit"];
                  if (name === "close") return [`₹${fmt(value)}`, "Close"];
                  return [value, name];
                }}
              />
              <Area type="monotone" dataKey="close" stroke="#3fb950" fill="url(#priceGrad)" strokeWidth={2} />
              {/* Trade entry points */}
              <Scatter dataKey="entryPrice" name="entryPrice" shape="triangle" isAnimationActive={false}>
                {chartData.map((d, i) => (
                  <Cell
                    key={i}
                    fill={d.entryType === "BUY" ? "#3fb950" : "#f85149"}
                    stroke={d.entryType === "BUY" ? "#3fb950" : "#f85149"}
                    r={d.entryPrice != null ? 6 : 0}
                  />
                ))}
              </Scatter>
              {/* Trade exit points */}
              <Scatter dataKey="exitPrice" name="exitPrice" shape="diamond" isAnimationActive={false}>
                {chartData.map((d, i) => (
                  <Cell
                    key={i}
                    fill={d.exitPnl != null && d.exitPnl >= 0 ? "#3fb950" : "#f85149"}
                    stroke="#ffffff"
                    strokeWidth={1}
                    r={d.exitPrice != null ? 6 : 0}
                  />
                ))}
              </Scatter>
            </ComposedChart>
          </ResponsiveContainer>
          {/* Legend for trade markers */}
          {(trades?.length ?? 0) > 0 && (
            <div className="flex items-center gap-4 mt-2 text-[10px] text-gray-500">
              <span className="flex items-center gap-1">
                <span className="inline-block w-0 h-0 border-l-[4px] border-r-[4px] border-b-[6px] border-l-transparent border-r-transparent border-b-emerald-400" />
                BUY Entry
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-0 h-0 border-l-[4px] border-r-[4px] border-b-[6px] border-l-transparent border-r-transparent border-b-red-400" />
                SELL Entry
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-2.5 h-2.5 rotate-45 bg-emerald-400 border border-white" />
                Profitable Exit
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-2.5 h-2.5 rotate-45 bg-red-400 border border-white" />
                Loss Exit
              </span>
            </div>
          )}
          </>
        )}
      </div>

      {/* Volume chart */}
      {chartData.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Volume</h3>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chartData}>
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis tick={{ fontSize: 10, fill: ct.tick }} />
              <Bar dataKey="volume" fill="#58a6ff" opacity={0.6} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Trades */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Trade History</h3>
          {tradesLoading ? (
            <div className="h-32 animate-pulse bg-gray-800 rounded" />
          ) : !trades || trades.length === 0 ? (
            <p className="text-gray-500 text-sm">No trades for {sym}</p>
          ) : (
            <div className="space-y-1 max-h-64 overflow-y-auto">
              {trades.map((t) => (
                <Link key={t.trade_id} to={`/trades/${t.trade_id}`}
                  className="flex items-center justify-between py-1.5 px-2 rounded hover:bg-gray-800/50 text-sm">
                  <div className="flex items-center gap-2">
                    <span className={clsx("text-xs px-1 rounded", t.signal_type === "BUY" ? "bg-emerald-900/40 text-emerald-400" : "bg-red-900/40 text-red-400")}>{t.signal_type}</span>
                    <span className="text-gray-400 text-xs">{parseUTC(t.created_at).toLocaleDateString("en-IN", { timeZone: getTimezone() })}</span>
                  </div>
                  <span className={clsx("text-sm", t.pnl != null && t.pnl >= 0 ? "text-emerald-400" : "text-red-400")}>
                    {t.pnl != null ? `₹${fmt(t.pnl)}` : "Open"}
                  </span>
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Predictions */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Predictions</h3>
          {!predictions || predictions.length === 0 ? (
            <p className="text-gray-500 text-sm">No predictions for {sym}</p>
          ) : (
            <div className="space-y-1 max-h-64 overflow-y-auto">
              {predictions.map((p) => (
                <div key={p.prediction_id} className="flex items-center justify-between py-1.5 px-2 text-sm">
                  <div className="flex items-center gap-2">
                    <span className="text-gray-400 text-xs">{parseUTC(p.created_at).toLocaleDateString("en-IN", { timeZone: getTimezone() })}</span>
                    {p.direction_correct != null && (
                      <span className={p.direction_correct ? "text-emerald-400 text-xs" : "text-red-400 text-xs"}>
                        {p.direction_correct ? "Correct" : "Wrong"}
                      </span>
                    )}
                  </div>
                  {p.actual_pnl_pct != null && (
                    <span className={clsx("text-sm", p.actual_pnl_pct >= 0 ? "text-emerald-400" : "text-red-400")}>
                      {p.actual_pnl_pct >= 0 ? "+" : ""}{fmt(p.actual_pnl_pct)}%
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* News */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Recent News</h3>
        {!news || news.length === 0 ? (
          <p className="text-gray-500 text-sm">No news for {sym}</p>
        ) : (
          <div className="space-y-2">
            {news.map((a) => (
              <a key={a.content_hash} href={a.url} target="_blank" rel="noopener noreferrer"
                className="block text-sm text-gray-300 hover:text-blue-400 py-1 border-b border-gray-800/50 last:border-0">
                {a.headline}
                <span className="text-xs text-gray-500 ml-2">{a.source}</span>
              </a>
            ))}
          </div>
        )}
      </div>

      {/* Sentiment drivers */}
      {sentiment && sentiment.key_drivers.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Sentiment Drivers</h3>
          <ul className="space-y-1 text-sm text-gray-300">
            {sentiment.key_drivers.map((d, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="text-blue-400 mt-0.5">•</span>
                {d}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
