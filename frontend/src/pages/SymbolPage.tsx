import { useState, useMemo } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import {
  useSymbolOHLCV,
  useSymbolTrades,
  useSymbolPredictions,
  useSentiment,
  useNews,
  useSymbolContext,
} from "../hooks/queries";
import {
  ComposedChart, Area, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  BarChart, Bar, CartesianGrid, Scatter, Cell,
} from "recharts";
import clsx from "clsx";
import { parseUTC, getTimezone } from "../utils/datetime";
import { newsSourceColorClass, newsSourceLabel } from "../utils/newsSource";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";
import { useLtpStream } from "../hooks/useLtpStream";
import { OrderForm } from "../components/OrderForm";

/** Trailing simple moving average over the last `period` values of
 * the named field. Returns null for indices that don't have enough
 * history yet (so recharts hides the line until the window fills). */
function trailingSMA<T extends object>(
  rows: T[], field: keyof T, period: number,
): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  const window: number[] = [];
  for (const row of rows) {
    const v = Number((row as Record<string, unknown>)[field as string] ?? 0);
    window.push(v);
    sum += v;
    if (window.length > period) sum -= window.shift()!;
    out.push(window.length === period ? sum / period : null);
  }
  return out;
}

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function SymbolPage() {
  const { symbol } = useParams<{ symbol: string }>();
  const navigate = useNavigate();
  const sym = symbol?.toUpperCase() || "";
  // Period selector. `Today` = current-day intraday 5-minute bars; the
  // rest are calendar-day windows of daily bars.
  const PERIODS: { label: string; days: number; interval: "daily" | "5minute" }[] = [
    { label: "Today", days: 1, interval: "5minute" },
    { label: "7d", days: 7, interval: "daily" },
    { label: "15d", days: 15, interval: "daily" },
    { label: "30d", days: 30, interval: "daily" },
    { label: "60d", days: 60, interval: "daily" },
    { label: "90d", days: 90, interval: "daily" },
    { label: "180d", days: 180, interval: "daily" },
    { label: "365d", days: 365, interval: "daily" },
  ];
  const [periodIdx, setPeriodIdx] = useState(1); // default 7d
  const period = PERIODS[periodIdx];

  // Inline order form trigger — populated when the user clicks Buy/Sell
  // in the header. Symbol pre-fills from the URL param so the user
  // doesn't have to retype it.
  const [orderForm, setOrderForm] = useState<{ side: "BUY" | "SELL" } | null>(null);

  const { data: ohlcv, isLoading: ohlcvLoading } = useSymbolOHLCV(sym, {
    days: period.days,
    interval: period.interval,
  });
  const { data: trades, isLoading: tradesLoading } = useSymbolTrades(sym);
  const { data: predictions } = useSymbolPredictions(sym);
  const { data: sentiment } = useSentiment(sym);
  const { data: news } = useNews({ symbol: sym, limit: 10 });
  const { data: ctxData } = useSymbolContext(sym);
  const ct = useChartTheme();
  const tooltipStyle = useTooltipStyle();
  // Live LTP — falls back to the latest chart close when no tick has
  // arrived yet (e.g. symbol not subscribed by the ticker yet).
  const ltps = useLtpStream();

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

    // Intraday bars (5min) get a HH:MM tick label; daily bars get a
    // "MMM D" label so the X-axis stays readable.
    const intraday = period.interval === "5minute";
    const base = (ohlcv || []).map((b) => {
      const d = parseUTC(b.timestamp);
      const dateKey = d.toLocaleDateString("en-IN", { timeZone: getTimezone(), month: "short", day: "numeric" });
      const date = intraday
        ? d.toLocaleTimeString("en-IN", { timeZone: getTimezone(), hour: "2-digit", minute: "2-digit" })
        : dateKey;
      // Suppress per-bar entry/exit markers on intraday charts —
      // multiple bars share the same calendar day so the marker would
      // stamp every bar of that day instead of the single fill bar.
      const entry = intraday ? undefined : entryMap.get(dateKey);
      const exit = intraday ? undefined : exitMap.get(dateKey);
      return {
        date,
        open: b.open,
        close: b.close,
        volume: b.volume,
        high: b.high,
        low: b.low,
        deliveryPct: b.delivery_pct ?? null,
        // green when close > open, red otherwise — used by the volume bar
        // colour to telegraph buying vs selling pressure intraday.
        volumeColor: b.close >= b.open ? "#3fb950" : "#f85149",
        entryPrice: entry?.price ?? null,
        entryType: entry?.type ?? null,
        exitPrice: exit?.price ?? null,
        exitPnl: exit?.pnl ?? null,
      };
    });
    // Trailing SMAs as EMA proxy. Real EMAs need recursive smoothing
    // which is fine but trailing SMA is enough at the daily-bar
    // resolution and is cheaper/simpler in client code.
    const sma9 = trailingSMA(base, "close", 9);
    const sma21 = trailingSMA(base, "close", 21);
    const sma50 = trailingSMA(base, "close", 50);
    return base.map((row, i) => ({
      ...row,
      sma9: sma9[i],
      sma21: sma21[i],
      sma50: sma50[i],
    }));
  }, [ohlcv, trades, period.interval]);

  const chartClose = chartData.length > 0 ? chartData[chartData.length - 1].close : null;
  const liveLtp = ltps.get(sym);
  const lastPrice = liveLtp ?? chartClose;
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
        <div className="ml-auto flex gap-2">
          <button
            onClick={() => setOrderForm({ side: "BUY" })}
            className="px-3 py-1 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white"
          >Buy</button>
          <button
            onClick={() => setOrderForm({ side: "SELL" })}
            className="px-3 py-1 rounded text-sm font-medium bg-red-600 hover:bg-red-700 text-white"
          >Sell</button>
        </div>
      </div>

      {orderForm && (
        <OrderForm
          defaultSymbol={sym}
          defaultSide={orderForm.side}
          onClose={() => setOrderForm(null)}
        />
      )}

      {/* Period selector */}
      <div className="flex gap-2 flex-wrap">
        {PERIODS.map((p, i) => (
          <button key={p.label} onClick={() => setPeriodIdx(i)}
            className={clsx("px-3 py-1 text-xs rounded", periodIdx === i ? "bg-emerald-900/40 text-emerald-400" : "bg-gray-800 text-gray-400 hover:bg-gray-700")}
          >{p.label}</button>
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
              {/* SMA overlays — match conventional 9/21/50 colors. Lines
                  start drawing only when their window fills, so the
                  short-term line appears first. */}
              <Line type="monotone" dataKey="sma9" stroke="#fbbf24" strokeWidth={1} dot={false} isAnimationActive={false} connectNulls={false} name="SMA9" />
              <Line type="monotone" dataKey="sma21" stroke="#60a5fa" strokeWidth={1} dot={false} isAnimationActive={false} connectNulls={false} name="SMA21" />
              <Line type="monotone" dataKey="sma50" stroke="#a78bfa" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls={false} name="SMA50" />
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
          {/* Legend for chart elements (trade markers + SMA lines) */}
          <div className="flex items-center gap-4 flex-wrap mt-2 text-[10px] text-gray-500">
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-0.5 bg-amber-400" />
              SMA 9
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-0.5 bg-blue-400" />
              SMA 21
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-0.5 bg-violet-400" />
              SMA 50
            </span>
            {(trades?.length ?? 0) > 0 && (
              <>
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
              </>
            )}
          </div>
          </>
        )}
      </div>

      {/* Volume chart — bars coloured by close-vs-open so a glance
          tells you whether the day was net buying (green) or net
          selling (red). */}
      {chartData.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Volume</h3>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chartData}>
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis tick={{ fontSize: 10, fill: ct.tick }} />
              <Tooltip contentStyle={tooltipStyle} />
              <Bar dataKey="volume" opacity={0.7} isAnimationActive={false}>
                {chartData.map((d, i) => (
                  <Cell key={i} fill={d.volumeColor} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Delivery % chart — from migration 038's ohlcv.delivery_pct
          column. High = strong-hand accumulation (institutional
          buying), low = intraday churn. NULL bars are skipped. */}
      {chartData.some((d) => d.deliveryPct != null) && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-1">Delivery %</h3>
          <p className="text-xs text-gray-500 mb-3">
            Fraction of volume that took delivery (strong-hand accumulation).
            NSE source — only populated on days the ingest priority loop
            covers this symbol.
          </p>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chartData}>
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis tick={{ fontSize: 10, fill: ct.tick }} domain={[0, 100]} />
              <Tooltip contentStyle={tooltipStyle} />
              <Bar dataKey="deliveryPct" fill="#0ea5e9" opacity={0.7} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Quarantine banner. Surfaces when ingest-data has tripped the
          3-strike quarantine on this symbol — explains why signals
          might be missing for it. */}
      {ctxData?.quarantine && (
        <div className="bg-amber-900/20 border border-amber-700/60 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-amber-400 mb-1">
            Quarantined ({ctxData.quarantine.consecutive_failures} fetch failures)
          </h3>
          <p className="text-xs text-amber-200/80">
            {ctxData.quarantine.last_error || "Reason not recorded."}
          </p>
          {ctxData.quarantine.replacement_symbol && (
            <p className="text-xs text-amber-200 mt-2">
              Routed to replacement{" "}
              <Link
                to={`/symbol/${ctxData.quarantine.replacement_symbol}`}
                className="font-medium underline"
              >
                {ctxData.quarantine.replacement_symbol}
              </Link>
              .
            </p>
          )}
        </div>
      )}

      {/* Latest signal + top-5 TreeSHAP attribution — what the model
          last said about this symbol and why. Mirrors the /symbol
          Telegram snapshot so the UI is in parity for the
          "click-and-find-out" use case. Per-trade attribution lives on
          the TradeDetail page; this is the per-symbol view. */}
      {ctxData?.latest_signal && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="flex items-baseline justify-between mb-3">
            <h3 className="text-sm font-medium text-gray-400">
              Latest Model Signal
            </h3>
            <span className="text-xs text-gray-600">
              {ctxData.latest_signal.created_at?.slice(0, 16).replace("T", " ")} IST
            </span>
          </div>
          <div className="flex items-center gap-3 mb-3">
            <span
              className={clsx(
                "px-2 py-0.5 rounded text-xs font-semibold",
                ctxData.latest_signal.signal_type === "BUY"
                  ? "bg-emerald-900/40 text-emerald-400"
                  : "bg-red-900/40 text-red-400",
              )}
            >
              {ctxData.latest_signal.signal_type}
            </span>
            <span className="text-xs text-gray-400">
              Confidence{" "}
              <span className="text-gray-200 font-mono">
                {ctxData.latest_signal.confidence_score != null
                  ? `${(ctxData.latest_signal.confidence_score * 100).toFixed(0)}%`
                  : "—"}
              </span>
            </span>
            {ctxData.latest_signal.disposition && (
              <span className="text-xs text-gray-500">
                disposition:{" "}
                <span className="text-gray-300">
                  {ctxData.latest_signal.disposition}
                </span>
              </span>
            )}
          </div>
          {ctxData.latest_signal.attribution.length > 0 ? (
            <div>
              <h4 className="text-xs text-gray-500 uppercase tracking-wide mb-2">
                Why (top 5 features)
              </h4>
              <ul className="space-y-1">
                {ctxData.latest_signal.attribution.map((a, i) => {
                  const positive = a.contribution >= 0;
                  return (
                    <li
                      key={i}
                      className="flex items-center justify-between text-xs"
                    >
                      <span className="text-gray-300 font-mono truncate mr-3">
                        {positive ? "↑" : "↓"} {a.feature}
                      </span>
                      <span
                        className={clsx(
                          "font-mono",
                          positive ? "text-emerald-400" : "text-red-400",
                        )}
                      >
                        {positive ? "+" : ""}
                        {a.contribution.toFixed(3)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : (
            <p className="text-xs text-gray-500 italic">
              No attribution stored for this signal (older signals predate the
              attribution column, or model didn't expose TreeSHAP).
            </p>
          )}
        </div>
      )}

      {/* Recent bulk / block deals on this symbol — institutional
          activity feeds the institutional_flow risk-check multiplier
          and the bulk_deal_* ML features. */}
      {ctxData?.recent_bulk_deals && ctxData.recent_bulk_deals.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="flex items-baseline justify-between mb-3">
            <h3 className="text-sm font-medium text-gray-400">
              Recent Bulk / Block Deals
            </h3>
            <span className="text-xs text-gray-600">
              avg delivery % last 5d:{" "}
              <span className="text-gray-300 font-mono">
                {ctxData.delivery_pct_avg_5d != null
                  ? `${ctxData.delivery_pct_avg_5d.toFixed(1)}%`
                  : "—"}
              </span>
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-1.5 pr-3 text-left">Date</th>
                  <th className="py-1.5 pr-3 text-left">Type</th>
                  <th className="py-1.5 pr-3 text-left">Client</th>
                  <th className="py-1.5 pr-3 text-left">B/S</th>
                  <th className="py-1.5 pr-3 text-right">Qty</th>
                  <th className="py-1.5 pr-3 text-right">Price</th>
                </tr>
              </thead>
              <tbody>
                {ctxData.recent_bulk_deals.map((d, i) => (
                  <tr key={i} className="border-b border-gray-800/50">
                    <td className="py-1.5 pr-3 text-gray-400">{d.deal_date}</td>
                    <td className="py-1.5 pr-3 text-gray-500">{d.deal_type}</td>
                    <td className="py-1.5 pr-3 text-gray-400 max-w-[18rem] truncate" title={d.client_name ?? ""}>
                      {d.client_name || "—"}
                    </td>
                    <td className="py-1.5 pr-3">
                      <span className={clsx(
                        "text-[10px] px-1.5 py-0.5 rounded font-medium",
                        (d.buy_sell ?? "").toUpperCase() === "BUY"
                          ? "bg-emerald-900/40 text-emerald-400"
                          : (d.buy_sell ?? "").toUpperCase() === "SELL"
                            ? "bg-red-900/40 text-red-400"
                            : "bg-gray-700/50 text-gray-300",
                      )}>
                        {d.buy_sell || "—"}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 text-right text-gray-300 font-mono">
                      {d.quantity?.toLocaleString("en-IN") ?? "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-gray-300 font-mono">
                      {d.trade_price != null ? `₹${d.trade_price.toFixed(2)}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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

      {/* News — same chip style as the News Feed page for consistency. */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Recent News</h3>
        {!news || news.length === 0 ? (
          <p className="text-gray-500 text-sm">No news for {sym}</p>
        ) : (
          <div className="space-y-2">
            {news.map((a) => (
              <div
                key={a.content_hash}
                className="bg-gray-900 border border-gray-800 rounded-lg p-3 hover:border-gray-700 transition-colors"
              >
                <p className="text-sm text-gray-200 line-clamp-2">{a.headline}</p>
                <div className="flex items-center gap-2 mt-2 flex-wrap">
                  <span
                    className={clsx(
                      "px-2 py-0.5 rounded text-xs font-medium",
                      newsSourceColorClass(a.source),
                    )}
                  >
                    {newsSourceLabel(a.source)}
                  </span>
                  {a.published_at && (
                    <span className="text-xs text-gray-500">
                      {new Date(a.published_at).toLocaleTimeString("en-IN", {
                        timeZone: getTimezone(),
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  )}
                  {a.url && (
                    <a
                      href={a.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-gray-500 hover:text-blue-400 transition-colors"
                      title="Open original article"
                    >
                      &#8599;
                    </a>
                  )}
                </div>
              </div>
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
