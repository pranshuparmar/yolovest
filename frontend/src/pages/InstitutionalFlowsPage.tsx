import { useMemo, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend, ReferenceLine,
} from "recharts";
import clsx from "clsx";
import { useInstitutionalFlows } from "../hooks/queries";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";
import { Pagination } from "../components/Pagination";
import { SymbolLink } from "../components/SymbolLink";
import { parseUTC, getTimezone } from "../utils/datetime";

const selectCls =
  "bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-emerald-500";

function fmtCr(v: number | null | undefined): string {
  if (v == null) return "—";
  const sign = v >= 0 ? "+" : "−";
  return `${sign}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
}

function fmtQty(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toLocaleString("en-IN");
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₹${v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function InstitutionalFlowsPage() {
  const [days, setDays] = useState(30);
  const [symbolFilter, setSymbolFilter] = useState("");
  const [appliedSymbol, setAppliedSymbol] = useState("");
  const [page, setPage] = useState(0);
  const pageSize = 25;
  const chartTheme = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  const { data, isLoading } = useInstitutionalFlows({
    days,
    bulk_limit: 500,
    symbol: appliedSymbol || undefined,
  });

  const fiiSeries = useMemo(
    () => (data?.fii_dii_timeline ?? []).map((d) => ({
      date: d.date,
      FII: d.fii_net,
      DII: d.dii_net,
    })),
    [data],
  );

  const summary = data?.fii_dii_summary;
  const pagedDeals = (data?.bulk_deals ?? []).slice(
    page * pageSize, (page + 1) * pageSize,
  );

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-lg font-bold text-gray-100">Institutional Flows</h2>
          <p className="text-sm text-gray-500 mt-1">
            FII / DII daily activity and recent bulk / block deals. Same data
            the institutional-flow risk-check multiplier reads at signal time.
          </p>
        </div>
        <div className="flex items-end gap-2">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Window</label>
            <select
              value={days}
              onChange={(e) => { setDays(Number(e.target.value)); setPage(0); }}
              className={selectCls}
            >
              <option value={7}>7 days</option>
              <option value={30}>30 days</option>
              <option value={90}>90 days</option>
              <option value={180}>180 days</option>
            </select>
          </div>
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <SummaryCard
          label="FII net (today)"
          value={fmtCr(summary?.fii_net_today ?? null)}
          color={(summary?.fii_net_today ?? 0) >= 0 ? "emerald" : "red"}
        />
        <SummaryCard
          label="DII net (today)"
          value={fmtCr(summary?.dii_net_today ?? null)}
          color={(summary?.dii_net_today ?? 0) >= 0 ? "emerald" : "red"}
        />
        <SummaryCard
          label={`FII net (${days}d sum)`}
          value={fmtCr(summary?.fii_net_total)}
          color={(summary?.fii_net_total ?? 0) >= 0 ? "emerald" : "red"}
        />
        <SummaryCard
          label={`DII net (${days}d sum)`}
          value={fmtCr(summary?.dii_net_total)}
          color={(summary?.dii_net_total ?? 0) >= 0 ? "emerald" : "red"}
        />
      </div>

      {/* FII/DII chart */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">
          FII / DII Net Flow (₹ Cr)
        </h3>
        {isLoading ? (
          <div className="h-72 animate-pulse bg-gray-800 rounded" />
        ) : fiiSeries.length === 0 ? (
          <p className="text-sm text-gray-500 py-12 text-center">
            No FII/DII data persisted yet — runs on every ingest-data heartbeat
            from NSE. First samples appear after the next market-hours cycle.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={320}>
            <BarChart data={fiiSeries}>
              <CartesianGrid strokeDasharray="3 3" stroke={chartTheme.grid} />
              <XAxis dataKey="date" tick={{ fill: chartTheme.tick, fontSize: 11 }} />
              <YAxis tick={{ fill: chartTheme.tick, fontSize: 11 }} />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <ReferenceLine y={0} stroke={chartTheme.grid} />
              <Bar dataKey="FII" fill="#3b82f6" />
              <Bar dataKey="DII" fill="#f59e0b" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Bulk / Block deals table */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-gray-300">Bulk / Block Deals</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Each row is one client's transaction on a symbol. BUY rows indicate
              institutional accumulation; SELL rows indicate distribution.
            </p>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setAppliedSymbol(symbolFilter.trim().toUpperCase());
              setPage(0);
            }}
            className="flex items-center gap-2"
          >
            <input
              type="text"
              value={symbolFilter}
              onChange={(e) => setSymbolFilter(e.target.value.toUpperCase())}
              placeholder="Filter by symbol"
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 w-36 focus:outline-none focus:border-emerald-500"
            />
            <button
              type="submit"
              className="px-2 py-1.5 rounded text-xs bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
            >
              Apply
            </button>
            {appliedSymbol && (
              <button
                type="button"
                onClick={() => {
                  setSymbolFilter("");
                  setAppliedSymbol("");
                  setPage(0);
                }}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Clear
              </button>
            )}
          </form>
        </div>
        {isLoading ? (
          <div className="h-40 m-4 animate-pulse bg-gray-800 rounded" />
        ) : !data?.bulk_deals.length ? (
          <p className="text-sm text-gray-500 py-12 text-center">
            No bulk / block deals in this window
            {appliedSymbol ? ` for ${appliedSymbol}` : ""}.
          </p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                    <th className="py-2 px-4 text-left">Date</th>
                    <th className="py-2 px-4 text-left">Symbol</th>
                    <th className="py-2 px-4 text-left">Type</th>
                    <th className="py-2 px-4 text-left">Client</th>
                    <th className="py-2 px-4 text-left">B / S</th>
                    <th className="py-2 px-4 text-right">Quantity</th>
                    <th className="py-2 px-4 text-right">Price</th>
                    <th className="py-2 px-4 text-right">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedDeals.map((d, i) => {
                    const value = (d.quantity ?? 0) * (d.trade_price ?? 0);
                    const dateLocal = (() => {
                      try {
                        return parseUTC(d.deal_date + "T00:00:00Z").toLocaleDateString("en-IN", {
                          timeZone: getTimezone(),
                          day: "2-digit", month: "short", year: "2-digit",
                        });
                      } catch { return d.deal_date; }
                    })();
                    return (
                      <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                        <td className="py-2 px-4 text-gray-400 whitespace-nowrap">
                          {dateLocal}
                        </td>
                        <td className="py-2 px-4 font-medium text-gray-200">
                          <SymbolLink symbol={d.symbol} className="text-gray-200" />
                        </td>
                        <td className="py-2 px-4">
                          <span className={clsx(
                            "text-[10px] px-1.5 py-0.5 rounded font-medium",
                            d.deal_type === "block"
                              ? "bg-blue-900/40 text-blue-400"
                              : "bg-gray-700/50 text-gray-300",
                          )}>
                            {d.deal_type}
                          </span>
                        </td>
                        <td className="py-2 px-4 text-gray-400 text-xs max-w-xs truncate" title={d.client_name ?? ""}>
                          {d.client_name || "—"}
                        </td>
                        <td className="py-2 px-4">
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
                        <td className="py-2 px-4 text-right text-gray-300 font-mono">
                          {fmtQty(d.quantity)}
                        </td>
                        <td className="py-2 px-4 text-right text-gray-300 font-mono">
                          {fmtPrice(d.trade_price)}
                        </td>
                        <td className="py-2 px-4 text-right text-gray-300 font-mono">
                          {value > 0
                            ? `₹${(value / 10_000_000).toLocaleString("en-IN", { maximumFractionDigits: 2 })} Cr`
                            : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <Pagination
              total={data.bulk_deals.length}
              page={page}
              pageSize={pageSize}
              onPageChange={setPage}
            />
          </>
        )}
      </div>
    </div>
  );
}

function SummaryCard({
  label, value, color,
}: { label: string; value: string; color: "emerald" | "red" | "gray" }) {
  const colorCls = {
    emerald: "text-emerald-400",
    red: "text-red-400",
    gray: "text-gray-300",
  }[color];
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={clsx("text-xl font-bold mt-1 font-mono", colorCls)}>
        {value}
      </div>
    </div>
  );
}
