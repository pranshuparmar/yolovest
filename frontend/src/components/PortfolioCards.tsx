import { usePortfolio } from "../hooks/queries";
import clsx from "clsx";

function Card({
  label,
  value,
  color,
  subtitle,
  subtitleColor,
}: {
  label: string;
  value: string;
  color?: string;
  subtitle?: string;
  subtitleColor?: string;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={clsx("text-xl font-semibold", color || "text-gray-100")}>
        {value}
      </p>
      {subtitle && (
        <p className={clsx("text-[10px] mt-0.5", subtitleColor || "text-gray-500")}>
          {subtitle}
        </p>
      )}
    </div>
  );
}

function pnlColor(v: number) {
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-gray-400";
}

function fmt(n: number, decimals = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function signed(n: number, decimals = 0) {
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmt(n, decimals)}`;
}

export function PortfolioCards() {
  const { data, isLoading } = usePortfolio();

  if (isLoading || !data) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-20 animate-pulse"
          />
        ))}
      </div>
    );
  }

  // Use broker-synced total when available, fall back to legacy total_capital.
  const portfolioValue =
    data.total_portfolio_value > 0 ? data.total_portfolio_value : data.total_capital;

  // Build "Locked" subtitle from broker margin + pending trade values.
  const lockedSubtitleParts: string[] = [];
  if (data.utilised_margin > 0)
    lockedSubtitleParts.push(`₹${fmt(data.utilised_margin, 0)} open trades`);
  if (data.pending_trade_value > 0)
    lockedSubtitleParts.push(`₹${fmt(data.pending_trade_value, 0)} pending`);

  const holdingsPnlColor =
    data.holdings_unrealized_pnl > 0
      ? "text-emerald-400"
      : data.holdings_unrealized_pnl < 0
      ? "text-red-400"
      : "text-gray-500";

  return (
    <div className="space-y-4">
      {/* Top row: capital breakdown */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card
          label="Available Funds"
          value={`₹${fmt(data.available_funds, 0)}`}
          color="text-emerald-400"
          subtitle="Free cash, ready to trade"
        />
        <Card
          label="Locked in Trades"
          value={`₹${fmt(data.locked_total, 0)}`}
          subtitle={lockedSubtitleParts.length > 0 ? lockedSubtitleParts.join(" · ") : "Nothing locked"}
        />
        <Card
          label="Holdings"
          value={`₹${fmt(data.holdings_current, 0)}`}
          subtitle={
            data.holdings_invested > 0
              ? `Invested ₹${fmt(data.holdings_invested, 0)} · ${signed(
                  data.holdings_unrealized_pnl,
                  0,
                )} (${signed(data.holdings_unrealized_pnl_pct * 100, 2)}%)`
              : "No holdings"
          }
          subtitleColor={data.holdings_invested > 0 ? holdingsPnlColor : undefined}
        />
        <Card
          label="Total Portfolio"
          value={`₹${fmt(portfolioValue, 0)}`}
          subtitle={`Capital baseline ₹${fmt(data.total_capital, 0)}`}
        />
      </div>

      {/* Second row: PnL + activity */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card
          label="Today's PnL"
          value={`${signed(data.daily_pnl, 0)}`}
          color={pnlColor(data.daily_pnl)}
          subtitle={
            data.daily_charges > 0
              ? `${signed(data.daily_pnl_pct * 100, 2)}% · Gross ${signed(
                  data.daily_pnl + data.daily_charges,
                  0,
                )}`
              : `${signed(data.daily_pnl_pct * 100, 2)}%`
          }
        />
        <Card
          label="Total PnL"
          value={`${signed(data.total_pnl, 0)}`}
          color={pnlColor(data.total_pnl)}
          subtitle={
            data.all_time_charges > 0
              ? `Realized ${signed(data.all_time_realized_pnl, 0)} (gross ${signed(
                  data.all_time_realized_pnl + data.all_time_charges,
                  0,
                )}) · Unrealized ${signed(data.holdings_unrealized_pnl, 0)}`
              : `Realized ${signed(data.all_time_realized_pnl, 0)} · Unrealized ${signed(
                  data.holdings_unrealized_pnl,
                  0,
                )}`
          }
        />
        <Card
          label="Open Positions"
          value={data.system_positions > 0 ? `${data.system_positions}` : "0"}
          subtitle={
            data.adopted_positions > 0
              ? `+${data.adopted_positions} adopted holdings`
              : `${data.trades_today} trades today`
          }
        />
        <Card
          label="Since Last Loss"
          value={
            data.minutes_since_last_loss >= 999
              ? "No losses"
              : data.minutes_since_last_loss >= 60
                ? `${fmt(data.minutes_since_last_loss / 60, 1)} hrs`
                : `${fmt(data.minutes_since_last_loss, 0)} min`
          }
          color={data.minutes_since_last_loss >= 999 ? "text-emerald-400" : undefined}
          subtitle={`Weekly ${signed(data.weekly_pnl_pct * 100, 2)}%`}
        />
      </div>
    </div>
  );
}
