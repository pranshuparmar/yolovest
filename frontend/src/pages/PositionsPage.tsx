import { PositionsTable } from "../components/PositionsTable";
import { usePositions } from "../hooks/queries";

export function PositionsPage() {
  const { data, isLoading } = usePositions();

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Open Positions</h2>
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        {isLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : (
          <PositionsTable positions={data || []} />
        )}
      </div>
    </div>
  );
}
