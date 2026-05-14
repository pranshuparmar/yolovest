import { PositionsTable } from "../components/PositionsTable";
import { CSVExportButton } from "../components/CSVExportButton";
import { usePositions } from "../hooks/queries";
import { SymbolLink } from "../components/SymbolLink";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function PositionsPage() {
  const { data, isLoading } = usePositions();
  const positions = data || [];

  // Compute summary stats
  const totalValue = positions.reduce(
    (s, p) => s + p.fill_price * p.quantity,
    0
  );
  const totalSlippage = positions.reduce((s, p) => s + Math.abs(p.slippage), 0);
  const misCount = positions.filter((p) => p.product === "MIS").length;
  const cncCount = positions.filter((p) => p.product === "CNC").length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Open Positions</h2>
        <CSVExportButton
          data={positions as unknown as Record<string, unknown>[]}
          filename={`positions-${new Date().toISOString().split("T")[0]}`}
        />
      </div>

      {/* Summary cards */}
      {positions.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">Total Positions</p>
            <p className="text-xl font-semibold">{positions.length}</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">Total Value</p>
            <p className="text-xl font-semibold">
              {fmt(totalValue, 0)}
            </p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">MIS / CNC</p>
            <p className="text-xl font-semibold">
              <span className="text-blue-400">{misCount}</span>
              {" / "}
              <span className="text-purple-400">{cncCount}</span>
            </p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
            <p className="text-xs text-gray-500">Total Slippage</p>
            <p className="text-xl font-semibold text-amber-400">
              {fmt(totalSlippage)}
            </p>
          </div>
        </div>
      )}

      {/* Position price level indicators */}
      {positions.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">
            Price Levels (Entry / SL / Target)
          </h3>
          <div className="space-y-3">
            {positions.map((p) => {
              const min = Math.min(p.stop_loss_price, p.entry_price) * 0.99;
              const max = Math.max(p.target_price, p.entry_price) * 1.01;
              const range = max - min || 1;
              const entryPct = ((p.entry_price - min) / range) * 100;
              const slPct = ((p.stop_loss_price - min) / range) * 100;
              const tgtPct = ((p.target_price - min) / range) * 100;
              const fillPct = ((p.fill_price - min) / range) * 100;

              return (
                <div key={p.trade_id} className="flex items-center gap-3">
                  <span className="text-xs font-medium text-emerald-400 w-20 shrink-0 truncate">
                    <SymbolLink symbol={p.symbol} className="text-emerald-400" />
                  </span>
                  <div className="flex-1 h-6 bg-gray-800 rounded-full relative">
                    {/* SL marker */}
                    <div
                      className="absolute top-0 bottom-0 w-0.5 bg-red-500"
                      style={{ left: `${Math.max(0, Math.min(100, slPct))}%` }}
                      title={`SL: ${fmt(p.stop_loss_price)}`}
                    />
                    {/* Entry marker */}
                    <div
                      className="absolute top-0 bottom-0 w-0.5 bg-gray-400"
                      style={{
                        left: `${Math.max(0, Math.min(100, entryPct))}%`,
                      }}
                      title={`Entry: ${fmt(p.entry_price)}`}
                    />
                    {/* Fill price dot */}
                    <div
                      className="absolute top-1/2 -translate-y-1/2 w-2.5 h-2.5 rounded-full bg-blue-400 border border-blue-300"
                      style={{
                        left: `${Math.max(0, Math.min(100, fillPct))}%`,
                        transform: "translate(-50%, -50%)",
                      }}
                      title={`Fill: ${fmt(p.fill_price)}`}
                    />
                    {/* Target marker */}
                    <div
                      className="absolute top-0 bottom-0 w-0.5 bg-emerald-500"
                      style={{
                        left: `${Math.max(0, Math.min(100, tgtPct))}%`,
                      }}
                      title={`Target: ${fmt(p.target_price)}`}
                    />
                    {/* Range fill between SL and target */}
                    <div
                      className="absolute top-0 bottom-0 bg-emerald-900/20 rounded"
                      style={{
                        left: `${Math.max(0, Math.min(100, slPct))}%`,
                        width: `${Math.max(0, tgtPct - slPct)}%`,
                      }}
                    />
                  </div>
                  <div className="flex gap-3 text-xs shrink-0">
                    <span className="text-red-400">
                      SL {fmt(p.stop_loss_price)}
                    </span>
                    <span className="text-gray-400">
                      E {fmt(p.entry_price)}
                    </span>
                    <span className="text-emerald-400">
                      T {fmt(p.target_price)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex gap-4 mt-3 text-xs text-gray-500">
            <span className="flex items-center gap-1">
              <span className="w-2 h-0.5 bg-red-500 inline-block" /> Stop Loss
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-0.5 bg-gray-400 inline-block" /> Entry
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-blue-400 inline-block" />{" "}
              Fill
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-0.5 bg-emerald-500 inline-block" />{" "}
              Target
            </span>
          </div>
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        {isLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : (
          <PositionsTable positions={positions} />
        )}
      </div>
    </div>
  );
}
