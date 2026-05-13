import { useMemo } from "react";
import clsx from "clsx";
import type { PnlCalendarDay } from "../types/api";

function fmt(n: number) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

/** Map PnL to a background colour. Inline rgba() rather than dynamic
 * Tailwind classes — Tailwind's JIT can only see fully-literal class
 * names at build time, so `bg-emerald-500/${alpha}` was silently
 * stripped, leaving cells with no background at all. */
function pnlStyle(pnl: number, maxAbs: number): { backgroundColor: string } {
  if (maxAbs === 0 || pnl === 0) {
    return { backgroundColor: "rgba(75, 85, 99, 0.4)" };  // gray-600/40
  }
  // Floor at 0.35 so even small days are clearly visible against the
  // dark background; scale up to 1.0 for the worst/best day.
  const intensity = 0.35 + 0.65 * Math.min(Math.abs(pnl) / maxAbs, 1);
  // emerald-400 (#34d399) for profit, rose-500 (#ef4444) for loss —
  // both pop against gray-900 better than emerald-500 / red-500.
  const rgb = pnl > 0 ? "52, 211, 153" : "239, 68, 68";
  return { backgroundColor: `rgba(${rgb}, ${intensity})` };
}

const EMPTY_CELL_STYLE = { backgroundColor: "rgba(55, 65, 81, 0.5)" };  // gray-700/50

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
                    isToday && "ring-1 ring-gray-300"
                  )}
                  style={entry ? pnlStyle(entry.pnl, maxAbs) : EMPTY_CELL_STYLE}
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

      {/* Legend — uses the same rgba scale as the cells so they actually match */}
      <div className="flex items-center gap-3 mt-2 ml-8">
        <span className="text-[10px] text-gray-500">Loss</span>
        <div className="flex gap-0.5">
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: "rgba(239, 68, 68, 1.0)" }} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: "rgba(239, 68, 68, 0.55)" }} />
          <div className="w-3 h-3 rounded-[2px]" style={EMPTY_CELL_STYLE} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: "rgba(52, 211, 153, 0.55)" }} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: "rgba(52, 211, 153, 1.0)" }} />
        </div>
        <span className="text-[10px] text-gray-500">Profit</span>
        {maxAbs > 0 && (
          <span className="text-[10px] text-gray-600 ml-2">
            Max: ₹{fmt(maxAbs)}
          </span>
        )}
      </div>
    </div>
  );
}
