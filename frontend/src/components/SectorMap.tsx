import type { SectorRotation } from "../types/api";

export function SectorMap({ data }: { data: SectorRotation }) {
  const sortedSectors = Object.entries(data.sectors).sort(
    ([, a], [, b]) => b.avg_score - a.avg_score
  );

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-4">
        Sector Rotation
      </h3>
      <div className="flex gap-4 mb-4">
        <div className="flex-1">
          <p className="text-xs text-gray-500 mb-2">Strong Sectors</p>
          <div className="flex flex-wrap gap-1">
            {data.strong.map((s) => (
              <span
                key={s}
                className="text-xs px-2 py-0.5 rounded bg-emerald-900/40 text-emerald-400"
              >
                {s}
              </span>
            ))}
            {data.strong.length === 0 && (
              <span className="text-xs text-gray-600">None</span>
            )}
          </div>
        </div>
        <div className="flex-1">
          <p className="text-xs text-gray-500 mb-2">Weak Sectors</p>
          <div className="flex flex-wrap gap-1">
            {data.weak.map((s) => (
              <span
                key={s}
                className="text-xs px-2 py-0.5 rounded bg-red-900/40 text-red-400"
              >
                {s}
              </span>
            ))}
            {data.weak.length === 0 && (
              <span className="text-xs text-gray-600">None</span>
            )}
          </div>
        </div>
      </div>
      {sortedSectors.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500 border-b border-gray-800">
              <th className="pb-2 pr-4">Sector</th>
              <th className="pb-2 pr-4">Avg Score</th>
              <th className="pb-2">Stocks</th>
            </tr>
          </thead>
          <tbody>
            {sortedSectors.map(([name, info]) => (
              <tr
                key={name}
                className="border-b border-gray-800/50"
              >
                <td className="py-1.5 pr-4">{name}</td>
                <td className="py-1.5 pr-4">{info.avg_score.toFixed(2)}</td>
                <td className="py-1.5">{info.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
