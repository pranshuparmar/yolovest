import { useState } from "react";
import { useEconomicCalendar, useEarnings } from "../hooks/queries";
import clsx from "clsx";

const impactColors: Record<string, string> = {
  high: "bg-red-900/40 text-red-400",
  medium: "bg-amber-900/40 text-amber-400",
  low: "bg-blue-900/40 text-blue-400",
};

export function EconomicCalendarPage() {
  const [days, setDays] = useState(30);
  const [country, setCountry] = useState<string | undefined>();
  const { data: events, isLoading } = useEconomicCalendar({ days, country });
  const { data: earnings, isLoading: earningsLoading } = useEarnings({ days });

  // Group events by date
  const grouped = (events || []).reduce<Record<string, typeof events>>(
    (acc, evt) => {
      const date = evt.event_date?.split("T")[0] || "Unknown";
      if (!acc[date]) acc[date] = [];
      acc[date]!.push(evt);
      return acc;
    },
    {}
  );

  const sortedDates = Object.keys(grouped).sort();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Economic Calendar</h2>
        <div className="flex items-center gap-3">
          <select
            value={country || ""}
            onChange={(e) => setCountry(e.target.value || undefined)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
          >
            <option value="">All Countries</option>
            <option value="IN">India</option>
            <option value="US">United States</option>
          </select>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
          >
            <option value={7}>Next 7 days</option>
            <option value={14}>Next 14 days</option>
            <option value={30}>Next 30 days</option>
            <option value={60}>Next 60 days</option>
          </select>
        </div>
      </div>

      {/* Impact legend */}
      <div className="flex gap-4 text-xs">
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-red-400" /> High Impact
        </span>
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-amber-400" /> Medium Impact
        </span>
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-blue-400" /> Low Impact
        </span>
      </div>

      {/* Calendar events */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-4">
          Upcoming Events
        </h3>
        {isLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : sortedDates.length === 0 ? (
          <p className="text-gray-500 text-sm py-4">No upcoming events</p>
        ) : (
          <div className="space-y-4">
            {sortedDates.map((date) => (
              <div key={date}>
                <div className="text-xs font-medium text-emerald-400 mb-2 sticky top-0 bg-gray-900 py-1">
                  {new Date(date + "T00:00:00").toLocaleDateString("en-IN", {
                    weekday: "long",
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                  })}
                </div>
                <div className="space-y-2 ml-3 border-l border-gray-800 pl-4">
                  {grouped[date]!.map((evt, i) => (
                    <div
                      key={i}
                      className="flex items-start justify-between py-2 border-b border-gray-800/50 last:border-0"
                    >
                      <div className="flex-1">
                        <p className="text-sm text-gray-200">{evt.title}</p>
                        <div className="flex items-center gap-2 mt-1">
                          <span className="text-xs text-gray-500">
                            {evt.source}
                          </span>
                          <span className="text-xs text-gray-600">|</span>
                          <span className="text-xs text-gray-500">
                            {evt.country}
                          </span>
                          {evt.event_type && (
                            <>
                              <span className="text-xs text-gray-600">|</span>
                              <span className="text-xs text-gray-500">
                                {evt.event_type}
                              </span>
                            </>
                          )}
                        </div>
                      </div>
                      <span
                        className={clsx(
                          "px-2 py-0.5 rounded text-xs font-medium ml-3 shrink-0",
                          impactColors[evt.impact] || impactColors.low
                        )}
                      >
                        {evt.impact}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Earnings calendar */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Upcoming Earnings
        </h3>
        {earningsLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : !earnings || earnings.length === 0 ? (
          <p className="text-gray-500 text-sm py-4">No upcoming earnings</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4">Date</th>
                  <th className="pb-2 pr-4">Symbol</th>
                  <th className="pb-2 pr-4">Event</th>
                  <th className="pb-2">Source</th>
                </tr>
              </thead>
              <tbody>
                {earnings.map((e, i) => (
                  <tr
                    key={i}
                    className="border-b border-gray-800/50 hover:bg-gray-800/30"
                  >
                    <td className="py-2 pr-4 text-gray-400">
                      {new Date(e.event_date).toLocaleDateString("en-IN", {
                        month: "short",
                        day: "numeric",
                      })}
                    </td>
                    <td className="py-2 pr-4 font-medium text-emerald-400">
                      {e.symbol}
                    </td>
                    <td className="py-2 pr-4">{e.title}</td>
                    <td className="py-2 text-gray-500 text-xs">{e.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
