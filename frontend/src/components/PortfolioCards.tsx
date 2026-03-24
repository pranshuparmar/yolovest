import { usePortfolio } from "../hooks/queries";
import clsx from "clsx";

function Card({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={clsx("text-xl font-semibold", color || "text-gray-100")}>
        {value}
      </p>
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

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <Card label="Total Capital" value={`₹${fmt(data.total_capital, 0)}`} />
      <Card label="Available Cash" value={`₹${fmt(data.available_cash, 0)}`} />
      <Card
        label="Exposure"
        value={`${fmt(data.exposure_pct * 100, 1)}%`}
      />
      <Card label="Open Positions" value={String(data.open_positions)} />
      <Card
        label="Daily PnL"
        value={`${data.daily_pnl_pct >= 0 ? "+" : ""}${fmt(data.daily_pnl_pct, 2)}%`}
        color={pnlColor(data.daily_pnl_pct)}
      />
      <Card
        label="Weekly PnL"
        value={`${data.weekly_pnl_pct >= 0 ? "+" : ""}${fmt(data.weekly_pnl_pct, 2)}%`}
        color={pnlColor(data.weekly_pnl_pct)}
      />
      <Card label="Trades Today" value={String(data.trades_today)} />
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
      />
    </div>
  );
}
