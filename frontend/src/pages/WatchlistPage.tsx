import { WatchlistTable } from "../components/WatchlistTable";
import { SectorMap } from "../components/SectorMap";
import { useWatchlist, useSectors } from "../hooks/queries";

export function WatchlistPage() {
  const { data: watchlist, isLoading: wlLoading } = useWatchlist();
  const { data: sectors, isLoading: secLoading } = useSectors();

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Watchlist</h2>

      {secLoading ? (
        <div className="h-40 animate-pulse bg-gray-900 rounded-lg" />
      ) : sectors ? (
        <SectorMap data={sectors} />
      ) : null}

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Scored Watchlist
        </h3>
        {wlLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : (
          <WatchlistTable items={watchlist || []} />
        )}
      </div>
    </div>
  );
}
