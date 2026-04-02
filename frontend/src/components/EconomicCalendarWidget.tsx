import { useEconomicCalendar } from "../hooks/queries";
import clsx from "clsx";
import { getTimezone } from "../utils/datetime";

const impactDot: Record<string, string> = {
  high: "bg-red-400",
  medium: "bg-amber-400",
  low: "bg-blue-400",
};

export function EconomicCalendarWidget() {
  const { data: events, isLoading } = useEconomicCalendar({ days: 7 });

  if (isLoading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-48 animate-pulse" />
    );
  }

  const upcoming = (events || []).slice(0, 5);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-3">
        Upcoming Events (7d)
      </h3>
      {upcoming.length === 0 ? (
        <p className="text-gray-500 text-sm py-2">No upcoming events</p>
      ) : (
        <div className="space-y-2">
          {upcoming.map((evt, i) => (
            <div
              key={i}
              className="flex items-center gap-2 py-1.5 border-b border-gray-800/50 last:border-0"
            >
              <span
                className={clsx(
                  "w-2 h-2 rounded-full shrink-0",
                  impactDot[evt.impact] || impactDot.low
                )}
              />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-gray-300 truncate">{evt.title}</p>
                <p className="text-xs text-gray-500">
                  {evt.event_date
                    ? new Date(evt.event_date).toLocaleDateString("en-IN", {
                        timeZone: getTimezone(),
                        month: "short",
                        day: "numeric",
                      })
                    : ""}{" "}
                  — {evt.country}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
