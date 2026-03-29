import { useMemo } from "react";
import clsx from "clsx";
import type { PnlCalendarDay } from "../types/api";

function fmt(n: number) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

/** Map PnL to a color intensity. Green for profit, red for loss. */
function pnlColor(pnl: number, maxAbs: number): string {
  if (maxAbs === 0) return "bg-gray-800";
  const intensity = Math.min(Math.abs(pnl) / maxAbs, 1);
  if (pnl > 0) {
    // Green scale: 10% → faint, 100% → vivid
    const alpha = Math.round(15 + intensity * 85);
    return `bg-emerald-500/${alpha}`;
  }
  if (pnl < 0) {
    const alpha = Math.round(15 + intensity * 85);
    return `bg-red-500/${alpha}`;
  }
  return "bg-gray-800";
}

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

interface Props {
  data: PnlCalendarDay[];
  months?: number;
}

export function PnlCalendarHeatmap({ data, months = 3 }: Props) {
  const { weeks, maxAbs, pnlMap } = useMemo(() => {
    // Build lookup
    const map = new Map<string, PnlCalendarDay>();
    let max = 0;
    for (const d of data) {
      map.set(d.date, d);
      max = Math.max(max, Math.abs(d.pnl));
    }

    // Generate calendar grid: last N months of weeks
    const today = new Date();
    const start = new Date(today);
    start.setMonth(start.getMonth() - months);
    // Align to Monday
    const day = start.getDay();
    start.setDate(start.getDate() - ((day + 6) % 7));

    const weeks: Date[][] = [];
    let week: Date[] = [];
    const cursor = new Date(start);
    while (cursor <= today) {
      week.push(new Date(cursor));
      if (week.length === 7) {
        weeks.push(week);
        week = [];
      }
      cursor.setDate(cursor.getDate() + 1);
    }
    if (week.length > 0) {
      weeks.push(week);
    }

    return { weeks, maxAbs: max, pnlMap: map };
  }, [data, months]);

  // Month labels
  const monthLabels = useMemo(() => {
    const labels: { label: string; col: number }[] = [];
    let lastMonth = -1;
    weeks.forEach((week, i) => {
      const d = week.find((d) => d.getDate() <= 7) || week[0];
      if (d.getMonth() !== lastMonth) {
        lastMonth = d.getMonth();
        labels.push({
          label: d.toLocaleString("en-IN", { month: "short" }),
          col: i,
        });
      }
    });
    return labels;
  }, [weeks]);

  return (
    <div>
      {/* Month labels */}
      <div className="flex mb-1 ml-8" style={{ gap: 0 }}>
        {monthLabels.map((m, i) => (
          <div
            key={i}
            className="text-[10px] text-gray-500"
            style={{
              position: "relative",
              left: `${m.col * 14}px`,
              marginRight: i < monthLabels.length - 1 ? 0 : undefined,
            }}
          >
            {m.label}
          </div>
        ))}
      </div>

      <div className="flex gap-0.5">
        {/* Weekday labels */}
        <div className="flex flex-col gap-0.5 mr-1 pt-0">
          {WEEKDAY_LABELS.map((d, i) => (
            <div key={d} className="h-3 flex items-center">
              {i % 2 === 0 ? (
                <span className="text-[9px] text-gray-600 w-6 text-right">{d}</span>
              ) : (
                <span className="w-6" />
              )}
            </div>
          ))}
        </div>

        {/* Calendar grid */}
        {weeks.map((week, wi) => (
          <div key={wi} className="flex flex-col gap-0.5">
            {week.map((date) => {
              const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
              const entry = pnlMap.get(key);
              const isToday =
                date.toDateString() === new Date().toDateString();

              return (
                <div
                  key={key}
                  className={clsx(
                    "w-3 h-3 rounded-[2px] cursor-default transition-colors",
                    entry
                      ? pnlColor(entry.pnl, maxAbs)
                      : "bg-gray-800/40",
                    isToday && "ring-1 ring-gray-500"
                  )}
                  title={
                    entry
                      ? `${key}: ₹${fmt(entry.pnl)} (${entry.wins}W/${entry.losses}L, ${entry.trade_count} trades)`
                      : `${key}: No trades`
                  }
                />
              );
            })}
          </div>
        ))}
      </div>

      {/* Legend */}
      <div className="flex items-center gap-3 mt-2 ml-8">
        <span className="text-[10px] text-gray-500">Less</span>
        <div className="flex gap-0.5">
          <div className="w-3 h-3 rounded-[2px] bg-red-500/85" />
          <div className="w-3 h-3 rounded-[2px] bg-red-500/40" />
          <div className="w-3 h-3 rounded-[2px] bg-gray-800" />
          <div className="w-3 h-3 rounded-[2px] bg-emerald-500/40" />
          <div className="w-3 h-3 rounded-[2px] bg-emerald-500/85" />
        </div>
        <span className="text-[10px] text-gray-500">More</span>
        {maxAbs > 0 && (
          <span className="text-[10px] text-gray-600 ml-2">
            Max: ₹{fmt(maxAbs)}
          </span>
        )}
      </div>
    </div>
  );
}
