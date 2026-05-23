import clsx from "clsx";
import { useRiskGates, useClearDriftSuspension } from "../hooks/queries";
import { SymbolLink } from "./SymbolLink";

function fmtMoney(n: number): string {
  return n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

export function RiskGatesPanel() {
  const { data, isLoading } = useRiskGates();
  const clearDrift = useClearDriftSuspension();

  if (isLoading || !data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-28 animate-pulse" />
    );
  }

  const { drift, beta, earnings } = data;

  // Hide the whole panel only when none of the three gates are
  // enabled — otherwise show it so the user always knows the state
  // of whatever they've turned on.
  if (!drift.enabled && !beta.enabled && !earnings.enabled) {
    return null;
  }

  const betaOver = beta.enabled && beta.utilization_pct > 100;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-3">Risk Gates</h3>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {/* Drift suspension */}
        {drift.enabled && (
          <div
            className={clsx(
              "rounded-lg border p-3",
              drift.suspended
                ? "border-rose-700 bg-rose-900/20"
                : "border-gray-800 bg-gray-800/30",
            )}
          >
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-gray-300">
                Drift Suspension
              </span>
              <span
                className={clsx(
                  "text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wide",
                  drift.suspended
                    ? "bg-rose-900/50 text-rose-300"
                    : "bg-emerald-900/40 text-emerald-300",
                )}
              >
                {drift.suspended ? "Suspended" : "Active"}
              </span>
            </div>
            {drift.suspended ? (
              <>
                <p className="text-xs text-rose-300/90 leading-snug">
                  Signal generation paused — {drift.reason}
                </p>
                <button
                  type="button"
                  onClick={() => clearDrift.mutate()}
                  disabled={clearDrift.isPending}
                  className="mt-2 text-[11px] px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-200 disabled:opacity-50"
                >
                  {clearDrift.isPending ? "Clearing…" : "Clear & resume"}
                </button>
              </>
            ) : (
              <p className="text-xs text-gray-500">
                Monitoring model drift; no suspension in effect.
              </p>
            )}
          </div>
        )}

        {/* Portfolio beta */}
        {beta.enabled && (
          <div
            className={clsx(
              "rounded-lg border p-3",
              betaOver
                ? "border-amber-700 bg-amber-900/20"
                : "border-gray-800 bg-gray-800/30",
            )}
          >
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-gray-300">
                Portfolio Beta
              </span>
              <span className="text-[10px] text-gray-500">
                cap {beta.cap_multiple.toFixed(1)}×
              </span>
            </div>
            <div className="flex items-baseline gap-1.5">
              <span
                className={clsx(
                  "text-lg font-mono",
                  betaOver ? "text-amber-300" : "text-gray-100",
                )}
              >
                {beta.utilization_pct.toFixed(0)}%
              </span>
              <span className="text-[10px] text-gray-500">of cap used</span>
            </div>
            <div className="text-[11px] text-gray-500 mt-0.5">
              ₹{fmtMoney(beta.current_beta_weighted)} / ₹{fmtMoney(beta.cap_value)}{" "}
              beta-weighted
            </div>
            {beta.positions.length > 0 && (
              <div className="mt-2 space-y-0.5 max-h-24 overflow-y-auto">
                {beta.positions.slice(0, 5).map((p) => (
                  <div
                    key={p.symbol}
                    className="flex items-center justify-between text-[11px]"
                  >
                    <SymbolLink symbol={p.symbol} className="text-gray-400" />
                    <span className="text-gray-500 font-mono">
                      β{p.beta.toFixed(2)}
                      {p.estimated && (
                        <span className="text-gray-600" title="No history — assumed β=1.0">
                          *
                        </span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Earnings blackout */}
        {earnings.enabled && (
          <div
            className={clsx(
              "rounded-lg border p-3",
              earnings.blocked_symbols.length > 0
                ? "border-blue-800 bg-blue-900/20"
                : "border-gray-800 bg-gray-800/30",
            )}
          >
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-gray-300">
                Earnings Blackout
              </span>
              <span className="text-[10px] text-gray-500">
                {earnings.window_days}d window
              </span>
            </div>
            {earnings.blocked_symbols.length === 0 ? (
              <p className="text-xs text-gray-500">
                No watched symbols have earnings in the window.
              </p>
            ) : (
              <div className="space-y-0.5 max-h-28 overflow-y-auto">
                {earnings.blocked_symbols.map((s) => (
                  <div
                    key={s.symbol}
                    className="flex items-center justify-between text-[11px]"
                    title={s.title ?? undefined}
                  >
                    <span className="flex items-center gap-1">
                      <SymbolLink symbol={s.symbol} className="text-blue-300" />
                      {s.held && (
                        <span className="text-[9px] px-1 rounded bg-blue-900/50 text-blue-300">
                          HELD
                        </span>
                      )}
                    </span>
                    <span className="text-gray-500">{s.event_date}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
