import { useSignalClassDistribution } from "../hooks/queries";
import clsx from "clsx";

const COLORS = {
  BUY: "bg-emerald-500",
  HOLD: "bg-gray-500",
  SELL: "bg-red-500",
} as const;

const LABEL_COLORS = {
  BUY: "text-emerald-400",
  HOLD: "text-gray-400",
  SELL: "text-red-400",
} as const;

/**
 * Per-day stacked-bar widget for the dashboard. Mirrors the
 * drift-watch class-collapse check so the user can see a class
 * dropping to zero before the daily alert fires.
 */
export function SignalClassWidget({ days = 7 }: { days?: number }) {
  const { data, isLoading } = useSignalClassDistribution(days);

  if (isLoading || !data) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-4">
        <h3 className="text-sm font-medium text-gray-400">
          Signal distribution (last {days}d)
        </h3>
        <p className="text-xs text-gray-600 mt-2">Loading…</p>
      </div>
    );
  }

  const total = data.total || 0;
  const bullets = (["BUY", "HOLD", "SELL"] as const).map((cls) => {
    const n = data[cls] || 0;
    const pct = total ? (n / total) * 100 : 0;
    return (
      <span key={cls} className="inline-flex items-center gap-1.5">
        <span className={clsx("w-2 h-2 rounded-full", COLORS[cls])} />
        <span className={clsx("text-xs", LABEL_COLORS[cls])}>
          {cls} {n}
          {total > 0 && (
            <span className="text-gray-600"> ({pct.toFixed(0)}%)</span>
          )}
        </span>
      </span>
    );
  });

  const collapsed = total >= 30 && (data.BUY === 0 || data.SELL === 0 || data.HOLD === 0);
  const dominated =
    total >= 30 &&
    ((data.BUY / total) > 0.95 ||
      (data.SELL / total) > 0.95 ||
      (data.HOLD / total) > 0.95);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-4">
      <div className="flex items-baseline justify-between mb-3 gap-3 flex-wrap">
        <h3 className="text-sm font-medium text-gray-400">
          Signal distribution (last {days}d)
        </h3>
        <div className="flex items-center gap-3 flex-wrap">{bullets}</div>
      </div>

      {(collapsed || dominated) && (
        <div
          className={clsx(
            "rounded border px-2.5 py-1.5 text-xs mb-3",
            collapsed
              ? "border-red-700 bg-red-900/20 text-red-300"
              : "border-amber-700 bg-amber-900/20 text-amber-300",
          )}
        >
          {collapsed
            ? "Signal-class collapse: at least one of BUY / HOLD / SELL has zero entries over the window. The model is silently refusing a direction."
            : "Class imbalance: more than 95% of signals fall into a single class."}
          {" "}drift-watch will alert tonight; consider /run model-retrain.
        </div>
      )}

      {data.by_day.length === 0 ? (
        <p className="text-xs text-gray-600">
          No signals recorded in the last {days} days.
        </p>
      ) : (
        <div className="space-y-1">
          {data.by_day.map((row) => {
            const dayTotal = row.BUY + row.HOLD + row.SELL || 1;
            return (
              <div
                key={row.date}
                className="flex items-center gap-2 text-xs"
                title={`${row.date}: BUY=${row.BUY}, HOLD=${row.HOLD}, SELL=${row.SELL}`}
              >
                <span className="w-20 text-gray-500 font-mono">
                  {row.date.slice(5)}
                </span>
                <div className="flex-1 h-3 rounded overflow-hidden bg-gray-800 flex">
                  <div
                    className={COLORS.BUY}
                    style={{ width: `${(row.BUY / dayTotal) * 100}%` }}
                  />
                  <div
                    className={COLORS.HOLD}
                    style={{ width: `${(row.HOLD / dayTotal) * 100}%` }}
                  />
                  <div
                    className={COLORS.SELL}
                    style={{ width: `${(row.SELL / dayTotal) * 100}%` }}
                  />
                </div>
                <span className="text-gray-500 font-mono w-12 text-right">
                  {dayTotal}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
