import { useMemo } from "react";
import clsx from "clsx";
import type { PnlCalendarDay } from "../types/api";
import { useTheme } from "../hooks/useTheme";

function fmt(n: number) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

// GitHub-style palette per theme. Dark uses brighter neons against
// gray-900; light uses saturated greens/reds that read against the
// near-white panel without the wash-out you'd get from neon alphas.
// Inline rgba() rather than Tailwind classes because the JIT can only
// see fully-literal class names — `bg-emerald-500/${alpha}` was being
// stripped and cells rendered with no background.
const PALETTE = {
  dark: {
    profit: "52, 211, 153",   // emerald-400
    loss: "239, 68, 68",       // red-500
    empty: "rgba(55, 65, 81, 0.5)",   // gray-700/50
    todayRing: "ring-gray-300",
  },
  light: {
    profit: "26, 127, 55",     // GitHub light green
    loss: "207, 34, 46",       // GitHub light red
    empty: "rgba(208, 215, 222, 0.6)",  // GitHub light border tint
    todayRing: "ring-gray-700",
  },
} as const;

function pnlStyle(pnl: number, maxAbs: number, isLight: boolean): { backgroundColor: string } {
  const p = isLight ? PALETTE.light : PALETTE.dark;
  if (maxAbs === 0 || pnl === 0) {
    return { backgroundColor: p.empty };
  }
  // Floor higher on light because pale tints over white wash out fast;
  // dark needs less because the contrast against gray-900 is forgiving.
  const minAlpha = isLight ? 0.18 : 0.35;
  const intensity = minAlpha + (1 - minAlpha) * Math.min(Math.abs(pnl) / maxAbs, 1);
  const rgb = pnl > 0 ? p.profit : p.loss;
  return { backgroundColor: `rgba(${rgb}, ${intensity})` };
}

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

interface Props {
  data: PnlCalendarDay[];
  months?: number;
}

export function PnlCalendarHeatmap({ data, months = 3 }: Props) {
  const { theme } = useTheme();
  const isLight = theme === "light";
  const palette = isLight ? PALETTE.light : PALETTE.dark;

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
                    isToday && `ring-1 ${palette.todayRing}`
                  )}
                  style={
                    entry
                      ? pnlStyle(entry.pnl, maxAbs, isLight)
                      : { backgroundColor: palette.empty }
                  }
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
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: `rgba(${palette.loss}, 1.0)` }} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: `rgba(${palette.loss}, 0.55)` }} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: palette.empty }} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: `rgba(${palette.profit}, 0.55)` }} />
          <div className="w-3 h-3 rounded-[2px]" style={{ backgroundColor: `rgba(${palette.profit}, 1.0)` }} />
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
