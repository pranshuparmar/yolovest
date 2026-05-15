import { useState } from "react";
import type { Report } from "../types/api";
import clsx from "clsx";
import { SymbolLink } from "./SymbolLink";

function fmt(n: number, decimals = 0) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function signed(n: number, decimals = 0) {
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmt(n, decimals)}`;
}

function pnlColor(v: number) {
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-gray-400";
}

function DailyReportContent({ content }: { content: Record<string, unknown> }) {
  const c = content as Record<string, number | string | null>;
  const portfolioValue = Number(c.portfolio_value || 0);
  const availableFunds = Number(c.available_funds || 0);
  const holdingsCurrent = Number(c.holdings_current || 0);
  const holdingsPnl = Number(c.holdings_unrealized_pnl || 0);
  const newEntries = Number(c.new_entries || 0);
  const exits = Number(c.exits || 0);
  const realizedPnl = Number(c.realized_pnl ?? c.total_pnl ?? 0);
  const wins = Number(c.wins || 0);
  const losses = Number(c.losses || 0);
  const winRate = Number(c.win_rate || 0);
  const openPositions = Number(c.open_positions || 0);
  const adoptedPositions = Number(c.adopted_positions || 0);
  const signalsGenerated = Number(c.signals_generated || 0);
  const predAccuracy = c.prediction_accuracy != null ? Number(c.prediction_accuracy) : null;
  const predScored = Number(c.predictions_scored || 0);

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
      {portfolioValue > 0 && (
        <div>
          <span className="text-gray-500">Portfolio</span>
          <p className="text-gray-200 font-medium">{fmt(portfolioValue)}</p>
        </div>
      )}
      {availableFunds > 0 && (
        <div>
          <span className="text-gray-500">Available</span>
          <p className="text-gray-200 font-medium">{fmt(availableFunds)}</p>
        </div>
      )}
      {holdingsCurrent > 0 && (
        <div>
          <span className="text-gray-500">Holdings</span>
          <p className="text-gray-200 font-medium">
            {fmt(holdingsCurrent)}{" "}
            <span className={clsx("text-xs", pnlColor(holdingsPnl))}>
              ({signed(holdingsPnl)})
            </span>
          </p>
        </div>
      )}
      <div>
        <span className="text-gray-500">Entries / Exits</span>
        <p className="text-gray-200 font-medium">
          {newEntries} / {exits}
        </p>
      </div>
      <div>
        <span className="text-gray-500">Open Positions</span>
        <p className="text-gray-200 font-medium">
          {openPositions} system
          {adoptedPositions > 0 && ` + ${adoptedPositions} adopted`}
        </p>
      </div>
      {signalsGenerated > 0 && (
        <div>
          <span className="text-gray-500">Signals</span>
          <p className="text-gray-200 font-medium">{signalsGenerated}</p>
        </div>
      )}
      {exits > 0 && (
        <>
          <div>
            <span className="text-gray-500">Realized PnL</span>
            <p className={clsx("font-medium", pnlColor(realizedPnl))}>
              {signed(realizedPnl, 2)}
            </p>
          </div>
          <div>
            <span className="text-gray-500">Win Rate</span>
            <p className="text-gray-200 font-medium">
              {(winRate * 100).toFixed(0)}% (W:{wins} L:{losses})
            </p>
          </div>
        </>
      )}
      {predAccuracy !== null && (
        <div>
          <span className="text-gray-500">Prediction Accuracy</span>
          <p className="text-gray-200 font-medium">
            {(predAccuracy * 100).toFixed(0)}% ({predScored} scored)
          </p>
        </div>
      )}
    </div>
  );
}

function WeeklyReportContent({ content }: { content: Record<string, unknown> }) {
  const c = content as Record<string, number | string | Record<string, unknown> | null>;
  const totalTrades = Number(c.total_trades || 0);
  const totalPnl = Number(c.total_pnl || 0);
  const winRate = Number(c.win_rate || 0);
  const llmApprovals = Number(c.llm_approvals || 0);
  const llmRejections = Number(c.llm_rejections || 0);
  const best = c.best_trade as Record<string, unknown> | null;
  const worst = c.worst_trade as Record<string, unknown> | null;

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
      <div>
        <span className="text-gray-500">Trades</span>
        <p className="text-gray-200 font-medium">{totalTrades}</p>
      </div>
      <div>
        <span className="text-gray-500">PnL</span>
        <p className={clsx("font-medium", pnlColor(totalPnl))}>
          {signed(totalPnl, 2)}
        </p>
      </div>
      <div>
        <span className="text-gray-500">Win Rate</span>
        <p className="text-gray-200 font-medium">{(winRate * 100).toFixed(0)}%</p>
      </div>
      {(llmApprovals > 0 || llmRejections > 0) && (
        <div>
          <span className="text-gray-500">LLM Reviews</span>
          <p className="text-gray-200 font-medium">
            {llmApprovals} approved, {llmRejections} rejected
          </p>
        </div>
      )}
      {best && (
        <div>
          <span className="text-gray-500">Best Trade</span>
          <p className="text-emerald-400 font-medium">
            <SymbolLink symbol={String(best.symbol)} className="text-emerald-400" />
            {" "}+{fmt(Number(best.pnl || 0), 2)}
          </p>
        </div>
      )}
      {worst && (
        <div>
          <span className="text-gray-500">Worst Trade</span>
          <p className="text-red-400 font-medium">
            <SymbolLink symbol={String(worst.symbol)} className="text-red-400" />
            {" "}{fmt(Number(worst.pnl || 0), 2)}
          </p>
        </div>
      )}
    </div>
  );
}

function ReportSummary({ report }: { report: Report }) {
  const c = report.content;
  if (report.report_type === "daily") {
    const pnl = Number((c as Record<string, number>).realized_pnl ?? (c as Record<string, number>).total_pnl ?? 0);
    const exits = Number((c as Record<string, number>).exits ?? (c as Record<string, number>).total_trades ?? 0);
    return (
      <span className="text-xs text-gray-500">
        {exits > 0 ? (
          <span className={pnlColor(pnl)}>{signed(pnl, 2)}</span>
        ) : (
          "No exits"
        )}
      </span>
    );
  }
  const pnl = Number((c as Record<string, number>).total_pnl ?? 0);
  const trades = Number((c as Record<string, number>).total_trades ?? 0);
  return (
    <span className="text-xs text-gray-500">
      {trades} trades, <span className={pnlColor(pnl)}>{signed(pnl, 2)}</span>
    </span>
  );
}

export function ReportViewer({ reports }: { reports: Report[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (reports.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No reports available</p>;
  }

  return (
    <div className="space-y-2">
      {reports.map((r) => (
        <div
          key={r.id}
          className="bg-gray-900 border border-gray-800 rounded-lg"
        >
          <button
            onClick={() => setExpanded(expanded === r.id ? null : r.id)}
            className="w-full flex items-center justify-between p-3 text-left hover:bg-gray-800/30"
          >
            <div className="flex items-center gap-3">
              <span
                className={clsx(
                  "text-xs px-2 py-0.5 rounded",
                  r.report_type === "daily"
                    ? "bg-blue-900/40 text-blue-400"
                    : "bg-purple-900/40 text-purple-400"
                )}
              >
                {r.report_type}
              </span>
              <span className="text-sm">{r.report_date}</span>
              <ReportSummary report={r} />
            </div>
            <span className="text-gray-500 text-xs">
              {expanded === r.id ? "▼" : "▶"}
            </span>
          </button>
          {expanded === r.id && (
            <div className="px-3 pb-3 border-t border-gray-800">
              <div className="mt-2">
                {r.report_type === "daily" ? (
                  <DailyReportContent content={r.content} />
                ) : (
                  <WeeklyReportContent content={r.content} />
                )}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
