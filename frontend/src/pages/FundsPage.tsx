import { useFunds, useFundsHistory } from "../hooks/queries";
import clsx from "clsx";
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from "recharts";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function inr(n: number, d = 2) {
  const sign = n < 0 ? "-" : "";
  return `${sign}₹${fmt(Math.abs(n), d)}`;
}

function pnlColor(v: number) {
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-gray-300";
}

function Row({
  label,
  value,
  hint,
  color,
}: {
  label: string;
  value: string;
  hint?: string;
  color?: string;
}) {
  return (
    <div className="flex items-baseline justify-between py-2 border-b border-gray-800/60 last:border-b-0">
      <div>
        <p className="text-sm text-gray-300">{label}</p>
        {hint && <p className="text-[11px] text-gray-600 mt-0.5">{hint}</p>}
      </div>
      <p className={clsx("font-mono text-sm tabular-nums", color || "text-gray-100")}>
        {value}
      </p>
    </div>
  );
}

function FundsMovementChart() {
  const { data: hist, isLoading } = useFundsHistory(90);
  const theme = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  if (isLoading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="h-40 animate-pulse bg-gray-800 rounded" />
      </div>
    );
  }

  const snapshots = (hist?.snapshots ?? []).slice().reverse();
  // Chronological for the chart; DB returns newest-first.
  const chartData = snapshots.map((s) => ({
    date: s.snapshot_date,
    cash: Math.round(s.available_cash),
    used: Math.round(s.utilised_margin),
    holdings: Math.round(s.holdings_current),
  }));

  return (
    <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-200">Funds Movement (last 90 days)</h3>
        <p className="text-[11px] text-gray-600">Captured daily 16:05 IST by `funds-snapshot`</p>
      </div>
      {chartData.length === 0 ? (
        <p className="text-sm text-gray-500 py-8 text-center">
          No snapshots yet. Run <code className="text-xs bg-gray-800 px-1.5 py-0.5 rounded">/run funds-snapshot</code> from
          Skills or wait for tomorrow's 16:05 IST cron fire.
        </p>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke={theme.grid} strokeDasharray="3 3" />
            <XAxis dataKey="date" stroke={theme.tick} tick={{ fontSize: 10 }} />
            <YAxis stroke={theme.tick} tick={{ fontSize: 10 }} />
            <Tooltip
              contentStyle={tooltipStyle}
              formatter={(value: number) => `₹${value.toLocaleString("en-IN")}`}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 6 }} />
            <Area
              type="monotone" dataKey="cash" name="Available Cash"
              stroke="#10b981" fill="#10b981" fillOpacity={0.15} stackId="1"
            />
            <Area
              type="monotone" dataKey="used" name="Used Margin"
              stroke="#f59e0b" fill="#f59e0b" fillOpacity={0.15} stackId="1"
            />
            <Area
              type="monotone" dataKey="holdings" name="Holdings"
              stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.15} stackId="1"
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

export function FundsPage() {
  const { data, isLoading, refetch, isFetching } = useFunds();

  if (isLoading) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Funds</h2>
        <div className="h-40 animate-pulse bg-gray-800 rounded" />
      </div>
    );
  }

  if (!data?.authenticated) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Funds</h2>
        <div className="bg-amber-900/30 border border-amber-700/50 rounded-lg p-4 text-sm text-amber-300">
          Kite is not authenticated. Re-authenticate via the Integrations page to view live funds.
        </div>
      </div>
    );
  }

  const s = data.summary;

  // Total deployed capital = utilised + holdings approximations from
  // the broker's payload. We derive a single "deployed" number for the
  // headline so the page reads like Kite's Funds summary: cash + used.
  const deployed = s.utilised_margin;
  const total = s.available_cash + deployed;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Funds</h2>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="px-3 py-1.5 rounded text-xs bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-50"
        >
          {isFetching ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {/* Headline cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 mb-1">Available Cash</p>
          <p className="text-2xl font-semibold text-emerald-400">{inr(s.available_cash, 0)}</p>
          <p className="text-[11px] text-gray-600 mt-1">Free margin, ready to deploy</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 mb-1">Used Margin</p>
          <p className="text-2xl font-semibold text-gray-100">{inr(deployed, 0)}</p>
          <p className="text-[11px] text-gray-600 mt-1">
            Locked against open positions
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 mb-1">Account Total (equity segment)</p>
          <p className="text-2xl font-semibold text-gray-100">{inr(total, 0)}</p>
          <p className="text-[11px] text-gray-600 mt-1">
            Cash + utilised. Net broker value: {inr(s.net, 0)}
          </p>
        </div>
      </div>

      {/* Detail tables — split into Available and Utilised the same
          way Kite does on the Funds page. Helps users reconcile
          line-by-line with what they see at Zerodha. */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-gray-200 mb-2">Available</h3>
          <Row
            label="Cash"
            value={inr(s.available_cash)}
            hint="Settled cash balance"
          />
          <Row
            label="Opening Balance"
            value={inr(s.opening_balance)}
            hint="Margin at the start of the day"
          />
          <Row
            label="Live Balance"
            value={inr(s.live_balance)}
            hint="Real-time margin (after M2M)"
          />
          {s.adhoc_margin !== undefined && s.adhoc_margin > 0 && (
            <Row
              label="Adhoc Margin"
              value={inr(s.adhoc_margin)}
              hint="Manually-added margin by broker"
            />
          )}
          {s.intraday_payin !== undefined && s.intraday_payin > 0 && (
            <Row
              label="Intraday Payin"
              value={inr(s.intraday_payin)}
              hint="Funds added during the day"
            />
          )}
          <Row
            label="Collateral"
            value={inr(s.collateral)}
            hint="Pledged holdings used as margin"
          />
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-gray-200 mb-2">Utilised</h3>
          <Row
            label="Total Debits"
            value={inr(s.utilised_margin)}
            hint="Sum of all margin/MTM locked"
          />
          <Row
            label="M2M (Unrealised)"
            value={inr(s.m2m_unrealised)}
            hint="Open positions, marked-to-market"
            color={pnlColor(s.m2m_unrealised)}
          />
          <Row
            label="M2M (Realised)"
            value={inr(s.m2m_realised)}
            hint="Closed today"
            color={pnlColor(s.m2m_realised)}
          />
          <Row
            label="Exposure"
            value={inr(s.exposure)}
            hint="Pre-trade exposure margin"
          />
          <Row
            label="SPAN"
            value={inr(s.span)}
            hint="Standard portfolio analysis margin (F&O)"
          />
          <Row
            label="Delivery"
            value={inr(s.delivery)}
            hint="Locked against delivery orders"
          />
          <Row
            label="Payout"
            value={inr(s.payout)}
            hint="Funds queued for withdrawal"
          />
          {s.option_premium !== undefined && s.option_premium > 0 && (
            <Row
              label="Option Premium"
              value={inr(s.option_premium)}
              hint="Premium paid on long options"
            />
          )}
          {s.turnover !== undefined && s.turnover > 0 && (
            <Row
              label="Turnover Margin"
              value={inr(s.turnover)}
              hint="Margin on intraday turnover"
            />
          )}
        </div>
      </div>

      <FundsMovementChart />

      <p className="text-[11px] text-gray-600">
        Data fetched live from Zerodha (kite.get_margins). Refreshes every 30s
        automatically; use Refresh for an immediate update.
      </p>
    </div>
  );
}
