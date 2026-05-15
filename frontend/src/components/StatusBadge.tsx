import { useHealth } from "../hooks/queries";
import clsx from "clsx";

export function StatusBadge() {
  const { data, isLoading } = useHealth();

  if (isLoading || !data) {
    return <span className="text-xs text-gray-500">Loading...</span>;
  }

  // The kill-switch state lives in <KillSwitchControl /> (rendered in the
  // header). Keeping a duplicate red badge here would just be noise.
  return (
    <div className="flex items-center gap-3">
      <span
        className={clsx(
          "inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full",
          data.status === "ok"
            ? "bg-emerald-900/40 text-emerald-400"
            : "bg-red-900/40 text-red-400"
        )}
      >
        <span
          className={clsx(
            "w-1.5 h-1.5 rounded-full",
            data.status === "ok" ? "bg-emerald-400" : "bg-red-400"
          )}
        />
        {data.status === "ok" ? "Healthy" : "Degraded"}
      </span>
      <span
        className={clsx(
          "text-xs px-2 py-0.5 rounded-full",
          data.mode === "live"
            ? "bg-amber-900/40 text-amber-400"
            : "bg-blue-900/40 text-blue-400"
        )}
      >
        {data.mode.toUpperCase()}
      </span>
    </div>
  );
}
