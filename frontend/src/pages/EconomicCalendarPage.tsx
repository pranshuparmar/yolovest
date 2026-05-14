import { useState, useMemo, useCallback, useEffect } from "react";
import {
  useEconomicCalendar,
  useEarnings,
  useHolidays,
  useAddHoliday,
  useRemoveHoliday,
  usePnlCalendar,
} from "../hooks/queries";
import { useTheme } from "../hooks/useTheme";
import { SymbolLink } from "../components/SymbolLink";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CalendarEvent {
  date: string;
  title: string;
  type: "economic" | "earnings" | "holiday" | "early_close" | "trade";
  impact?: "high" | "medium" | "low";
  source?: string;
  country?: string;
  symbol?: string;
  earlyCloseTime?: string;
}

interface PnlDay {
  pnl: number;
  trade_count: number;
  wins: number;
  losses: number;
}

type ViewMode = "week" | "month";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function toDateStr(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}

function startOfWeek(d: Date): Date {
  const r = new Date(d);
  r.setDate(r.getDate() - r.getDay());
  return r;
}

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function isSameDay(a: string, b: string): boolean {
  return a === b;
}

function isWeekend(d: Date): boolean {
  return d.getDay() === 0 || d.getDay() === 6;
}

function eventTooltip(evt: CalendarEvent): string {
  const parts = [evt.title];
  if (evt.impact) parts.push(`Impact: ${evt.impact}`);
  if (evt.symbol) parts.push(`Symbol: ${evt.symbol}`);
  if (evt.source) parts.push(`Source: ${evt.source}`);
  if (evt.country) parts.push(`Country: ${evt.country}`);
  return parts.join("\n");
}

function formatInr(n: number): string {
  const abs = Math.abs(n);
  const sign = n >= 0 ? "+" : "-";
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

// PnL background color: green for profit, red for loss, scaled by magnitude
function pnlBgColor(pnl: number, maxAbsPnl: number, isLight: boolean): string {
  if (pnl === 0 || maxAbsPnl === 0) return "";
  const intensity = Math.min(Math.abs(pnl) / maxAbsPnl, 1);
  // Light theme has very low contrast with low alpha — bump the floor
  // and use GitHub-light tones that read against #f6f8fa. Dark theme
  // keeps the prior subtle range against gray-900.
  if (isLight) {
    const opacity = (0.15 + intensity * 0.45).toFixed(2);
    if (pnl > 0) return `rgba(26, 127, 55, ${opacity})`;
    return `rgba(207, 34, 46, ${opacity})`;
  }
  const opacity = Math.round(8 + intensity * 25);
  if (pnl > 0) return `rgba(34, 197, 94, ${opacity / 100})`;
  return `rgba(239, 68, 68, ${opacity / 100})`;
}

// ---------------------------------------------------------------------------
// Event type styles
// ---------------------------------------------------------------------------

const EVENT_STYLES: Record<string, { dot: string; bg: string; text: string }> = {
  holiday: { dot: "bg-red-400", bg: "bg-red-900/20", text: "text-red-300" },
  early_close: { dot: "bg-amber-400", bg: "bg-amber-900/20", text: "text-amber-300" },
  economic: { dot: "bg-blue-400", bg: "bg-blue-900/20", text: "text-blue-300" },
  earnings: { dot: "bg-emerald-400", bg: "bg-emerald-900/20", text: "text-emerald-300" },
  trade: { dot: "bg-purple-400", bg: "bg-purple-900/20", text: "text-purple-300" },
};

const IMPACT_COLORS: Record<string, string> = {
  high: "bg-red-900/40 text-red-400",
  medium: "bg-amber-900/40 text-amber-400",
  low: "bg-blue-900/40 text-blue-400",
};

// ---------------------------------------------------------------------------
// PnL Badge (shown in day cells when there are trades)
// ---------------------------------------------------------------------------

function PnlBadge({ pnl }: { pnl: PnlDay }) {
  const positive = pnl.pnl >= 0;
  return (
    <div
      className={clsx(
        "flex items-center gap-1.5 px-1.5 py-1 rounded text-[11px] font-medium",
        positive ? "bg-emerald-900/30 text-emerald-400" : "bg-red-900/30 text-red-400",
      )}
      title={`${pnl.trade_count} trade${pnl.trade_count !== 1 ? "s" : ""}: ${pnl.wins}W ${pnl.losses}L`}
    >
      <span className={clsx("w-2 h-2 rounded-full shrink-0", positive ? "bg-emerald-400" : "bg-red-400")} />
      <span>{formatInr(pnl.pnl)}</span>
      <span className="text-[9px] text-gray-500 ml-auto">{pnl.trade_count}T</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Event Badge
// ---------------------------------------------------------------------------

function EventBadge({ event }: { event: CalendarEvent }) {
  const s = EVENT_STYLES[event.type] || EVENT_STYLES.economic;
  return (
    <div
      className={clsx("flex items-center gap-1 px-1 py-0.5 rounded text-[10px] leading-tight", s.bg, s.text)}
      title={eventTooltip(event)}
    >
      <span className={clsx("w-1.5 h-1.5 rounded-full shrink-0", s.dot)} />
      <span className="truncate">{event.title}</span>
      {event.impact && (
        <span className={clsx("shrink-0 px-1 rounded text-[9px]", IMPACT_COLORS[event.impact])}>
          {event.impact[0].toUpperCase()}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add Holiday Dialog
// ---------------------------------------------------------------------------

function AddHolidayDialog({
  dateStr,
  onClose,
}: {
  dateStr: string | null;
  onClose: () => void;
}) {
  const addMutation = useAddHoliday();
  const [date, setDate] = useState("");
  const [isEarlyClose, setIsEarlyClose] = useState(false);
  const [earlyCloseTime, setEarlyCloseTime] = useState("13:00");

  useEffect(() => {
    if (dateStr !== null) {
      setDate(dateStr);
      setIsEarlyClose(false);
      setEarlyCloseTime("13:00");
    }
  }, [dateStr]);

  const handleSubmit = () => {
    if (!date) return;
    addMutation.mutate(
      { date, early_close: isEarlyClose ? earlyCloseTime : undefined },
      { onSuccess: onClose },
    );
  };

  if (dateStr === null) return null;

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-gray-900 border border-gray-700 rounded-lg p-5 w-full max-w-sm space-y-4" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-semibold text-gray-200">Add Holiday</h3>
        <div>
          <label className="text-xs text-gray-400">Date</label>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
            className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:border-blue-500 focus:outline-none" />
        </div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={isEarlyClose} onChange={(e) => setIsEarlyClose(e.target.checked)} className="w-4 h-4 accent-amber-500" />
          <span className="text-sm text-gray-300">Early close day (not full holiday)</span>
        </label>
        {isEarlyClose && (
          <div>
            <label className="text-xs text-gray-400">Close time</label>
            <input type="time" value={earlyCloseTime} onChange={(e) => setEarlyCloseTime(e.target.value)}
              className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:border-blue-500 focus:outline-none" />
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600 text-gray-300">Cancel</button>
          <button onClick={handleSubmit} disabled={!date || addMutation.isPending}
            className="px-3 py-1.5 rounded text-sm font-medium bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-40">
            {addMutation.isPending ? "Adding..." : "Add"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Day Cell (month view) — with PnL heatmap background
// ---------------------------------------------------------------------------

function DayCell({
  date,
  events,
  pnl,
  maxAbsPnl,
  showPnl,
  isToday,
  isCurrentMonth,
  isLight,
  onAddHoliday,
  onRemoveHoliday,
}: {
  date: Date;
  events: CalendarEvent[];
  pnl: PnlDay | undefined;
  maxAbsPnl: number;
  showPnl: boolean;
  isToday: boolean;
  isCurrentMonth: boolean;
  isLight: boolean;
  onAddHoliday: (dateStr: string) => void;
  onRemoveHoliday: (dateStr: string) => void;
}) {
  const dateStr = toDateStr(date);
  const hasHoliday = events.some((e) => e.type === "holiday");
  const weekend = isWeekend(date);
  const bgStyle = showPnl && pnl && pnl.pnl !== 0
    ? { backgroundColor: pnlBgColor(pnl.pnl, maxAbsPnl, isLight) }
    : undefined;

  return (
    <div
      className={clsx(
        "border border-gray-800 flex flex-col min-h-[90px]",
        !isCurrentMonth && "opacity-40",
        hasHoliday && !bgStyle && "bg-red-950/20",
        weekend && !hasHoliday && !bgStyle && "bg-gray-800/40",
      )}
      style={bgStyle}
    >
      {/* Day header */}
      <div className="flex items-center justify-between px-1.5 py-1 border-b border-gray-800/50">
        <span
          className={clsx(
            "text-xs font-medium",
            isToday && "bg-blue-600 text-white rounded-full w-6 h-6 flex items-center justify-center",
            !isToday && weekend && "text-gray-600",
            !isToday && !weekend && "text-gray-400",
          )}
        >
          {date.getDate()}
        </span>
        <div className="flex items-center gap-0.5">
          {hasHoliday ? (
            <button onClick={() => onRemoveHoliday(dateStr)}
              className="text-red-500 hover:text-red-300 text-[10px] px-1 rounded hover:bg-red-900/30" title="Remove holiday">✕</button>
          ) : !weekend && (
            <button onClick={() => onAddHoliday(dateStr)}
              className="text-gray-600 hover:text-gray-300 text-[10px] px-1 rounded hover:bg-gray-800" title="Add holiday">+</button>
          )}
        </div>
      </div>
      {/* PnL badge */}
      {showPnl && pnl && <div className="px-1 pt-0.5"><PnlBadge pnl={pnl} /></div>}
      {/* Events */}
      <div className="flex-1 px-1 py-0.5 space-y-0.5 overflow-hidden">
        {events.slice(0, showPnl && pnl ? 2 : 3).map((evt, i) => (
          <EventBadge key={i} event={evt} />
        ))}
        {events.length > (showPnl && pnl ? 2 : 3) && (
          <span className="text-[10px] text-gray-500 px-1">+{events.length - (showPnl && pnl ? 2 : 3)} more</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Week View
// ---------------------------------------------------------------------------

function WeekView({
  currentDate,
  eventsMap,
  pnlMap,
  maxAbsPnl,
  showPnl,
  isLight,
  onAddHoliday,
  onRemoveHoliday,
}: {
  currentDate: Date;
  eventsMap: Map<string, CalendarEvent[]>;
  pnlMap: Map<string, PnlDay>;
  maxAbsPnl: number;
  showPnl: boolean;
  isLight: boolean;
  onAddHoliday: (dateStr: string) => void;
  onRemoveHoliday: (dateStr: string) => void;
}) {
  const weekStart = startOfWeek(currentDate);
  const todayStr = toDateStr(new Date());
  const days = Array.from({ length: 7 }, (_, i) => addDays(weekStart, i));

  return (
    <div>
      {/* Header row */}
      <div className="grid grid-cols-7 border-b border-gray-700">
        {days.map((d, i) => {
          const ds = toDateStr(d);
          const weekend = isWeekend(d);
          const dayPnl = pnlMap.get(ds);
          return (
            <div
              key={i}
              className={clsx(
                "text-center py-2 text-xs font-medium border-r border-gray-800 last:border-r-0",
                isSameDay(ds, todayStr) ? "text-blue-400" : weekend ? "text-gray-600" : "text-gray-500",
                weekend && "bg-gray-800/40",
              )}
            >
              <div>{DAY_NAMES[d.getDay()]}</div>
              <div className={clsx("text-lg font-bold mt-0.5", weekend && !isSameDay(ds, todayStr) ? "text-gray-500" : "text-gray-300")}>
                {isSameDay(ds, todayStr) ? (
                  <span className="bg-blue-600 text-white rounded-full w-8 h-8 inline-flex items-center justify-center">{d.getDate()}</span>
                ) : d.getDate()}
              </div>
              <div className="text-[10px] text-gray-600">{MONTH_NAMES[d.getMonth()]?.slice(0, 3)}</div>
              {/* PnL summary under date */}
              {showPnl && dayPnl && (
                <div className={clsx("text-[10px] font-medium mt-0.5", dayPnl.pnl >= 0 ? "text-emerald-400" : "text-red-400")}>
                  {formatInr(dayPnl.pnl)}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {/* Event columns */}
      <div className="grid grid-cols-7">
        {days.map((d, i) => {
          const ds = toDateStr(d);
          const events = eventsMap.get(ds) || [];
          const weekend = isWeekend(d);
          const dayPnl = pnlMap.get(ds);
          const bgStyle = showPnl && dayPnl && dayPnl.pnl !== 0
            ? { backgroundColor: pnlBgColor(dayPnl.pnl, maxAbsPnl, isLight) }
            : undefined;
          return (
            <div
              key={i}
              className={clsx(
                "border-r border-gray-800 last:border-r-0 min-h-[300px] p-1.5 space-y-1",
                events.some((e) => e.type === "holiday") && !bgStyle && "bg-red-950/20",
                weekend && !events.some((e) => e.type === "holiday") && !bgStyle && "bg-gray-800/40",
              )}
              style={bgStyle}
            >
              {/* PnL card at top of day column */}
              {showPnl && dayPnl && (
                <div className={clsx(
                  "rounded px-2 py-1.5 text-xs",
                  dayPnl.pnl >= 0 ? "bg-emerald-900/30" : "bg-red-900/30",
                )}>
                  <div className={clsx("font-semibold text-sm", dayPnl.pnl >= 0 ? "text-emerald-400" : "text-red-400")}>
                    {formatInr(dayPnl.pnl)}
                  </div>
                  <div className="text-gray-500 mt-0.5">
                    {dayPnl.trade_count} trade{dayPnl.trade_count !== 1 ? "s" : ""}
                    <span className="mx-1">·</span>
                    <span className="text-emerald-500">{dayPnl.wins}W</span>
                    {dayPnl.losses > 0 && <><span className="mx-1">·</span><span className="text-red-500">{dayPnl.losses}L</span></>}
                  </div>
                </div>
              )}
              {events.length === 0 && !weekend && !dayPnl && (
                <button onClick={() => onAddHoliday(ds)}
                  className="w-full text-center text-gray-700 hover:text-gray-400 text-xs py-8 hover:bg-gray-800/50 rounded transition-colors">
                  + Add Holiday
                </button>
              )}
              {events.map((evt, j) => (
                <div
                  key={j}
                  className={clsx("rounded px-2 py-1.5 text-xs group", EVENT_STYLES[evt.type]?.bg || "bg-gray-800", EVENT_STYLES[evt.type]?.text || "text-gray-300")}
                  title={eventTooltip(evt)}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={clsx("w-2 h-2 rounded-full shrink-0", EVENT_STYLES[evt.type]?.dot || "bg-gray-500")} />
                    <span className="font-medium truncate group-hover:whitespace-normal group-hover:break-words">{evt.title}</span>
                    {evt.type === "holiday" && (
                      <button onClick={() => onRemoveHoliday(ds)}
                        className="ml-auto text-red-500 hover:text-red-300 shrink-0" title="Remove holiday">✕</button>
                    )}
                  </div>
                  {evt.impact && (
                    <span className={clsx("inline-block mt-1 px-1.5 py-0 rounded text-[10px]", IMPACT_COLORS[evt.impact])}>{evt.impact}</span>
                  )}
                  {evt.symbol && (
                    <span className="block mt-0.5 text-emerald-400 text-[10px]">
                      <SymbolLink symbol={evt.symbol} className="text-emerald-400" />
                    </span>
                  )}
                  {evt.source && <span className="block mt-0.5 text-gray-600 text-[10px]">{evt.source}</span>}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Month View
// ---------------------------------------------------------------------------

function MonthView({
  currentDate,
  eventsMap,
  pnlMap,
  maxAbsPnl,
  showPnl,
  isLight,
  onAddHoliday,
  onRemoveHoliday,
}: {
  currentDate: Date;
  eventsMap: Map<string, CalendarEvent[]>;
  pnlMap: Map<string, PnlDay>;
  maxAbsPnl: number;
  showPnl: boolean;
  isLight: boolean;
  onAddHoliday: (dateStr: string) => void;
  onRemoveHoliday: (dateStr: string) => void;
}) {
  const monthStart = startOfMonth(currentDate);
  const calStart = startOfWeek(monthStart);
  const todayStr = toDateStr(new Date());

  const cells: Date[] = [];
  for (let i = 0; i < 42; i++) cells.push(addDays(calStart, i));
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) {
    const week = cells.slice(i, i + 7);
    if (week.some((d) => d.getMonth() === currentDate.getMonth())) weeks.push(week);
  }

  return (
    <div>
      <div className="grid grid-cols-7">
        {DAY_NAMES.map((d, i) => (
          <div key={d} className={clsx("text-center py-2 text-xs font-medium border-b border-gray-700", i === 0 || i === 6 ? "text-gray-600 bg-gray-800/40" : "text-gray-500")}>{d}</div>
        ))}
      </div>
      {weeks.map((week, wi) => (
        <div key={wi} className="grid grid-cols-7">
          {week.map((d) => {
            const ds = toDateStr(d);
            return (
              <DayCell
                key={ds}
                date={d}
                events={eventsMap.get(ds) || []}
                pnl={pnlMap.get(ds)}
                maxAbsPnl={maxAbsPnl}
                showPnl={showPnl}
                isToday={isSameDay(ds, todayStr)}
                isCurrentMonth={d.getMonth() === currentDate.getMonth()}
                isLight={isLight}
                onAddHoliday={onAddHoliday}
                onRemoveHoliday={onRemoveHoliday}
              />
            );
          })}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export function EconomicCalendarPage() {
  const [view, setView] = useState<ViewMode>("week");
  const [currentDate, setCurrentDate] = useState(() => new Date());
  const [addHolidayDate, setAddHolidayDate] = useState<string | null>(null);
  const [showPnl, setShowPnl] = useState(true);
  const { theme } = useTheme();
  const isLight = theme === "light";

  const { data: events } = useEconomicCalendar({ days: 90 });
  const { data: earnings } = useEarnings({ days: 90 });
  const { data: holidaysData } = useHolidays();
  const { data: pnlData } = usePnlCalendar(365);
  const removeMutation = useRemoveHoliday();

  // Build PnL map
  const { pnlMap, maxAbsPnl } = useMemo(() => {
    const map = new Map<string, PnlDay>();
    let maxAbs = 0;
    for (const day of pnlData || []) {
      const key = day.date?.split("T")[0];
      if (key) {
        map.set(key, { pnl: day.pnl, trade_count: day.trade_count, wins: day.wins, losses: day.losses });
        maxAbs = Math.max(maxAbs, Math.abs(day.pnl));
      }
    }
    return { pnlMap: map, maxAbsPnl: maxAbs };
  }, [pnlData]);

  // Build events map
  const eventsMap = useMemo(() => {
    const map = new Map<string, CalendarEvent[]>();
    const addEvent = (dateStr: string, evt: CalendarEvent) => {
      const key = dateStr.split("T")[0];
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(evt);
    };

    for (const e of events || []) {
      addEvent(e.event_date, { date: e.event_date, title: e.title, type: "economic", impact: e.impact, source: e.source, country: e.country });
    }
    for (const e of earnings || []) {
      addEvent(e.event_date, { date: e.event_date, title: e.title, type: "earnings", symbol: e.symbol, source: e.source });
    }
    for (const h of holidaysData?.holidays || []) {
      addEvent(h, { date: h, title: "NSE Holiday", type: "holiday" });
    }
    for (const [d, t] of Object.entries(holidaysData?.early_close_days || {})) {
      addEvent(d, { date: d, title: `Early Close (${t})`, type: "early_close", earlyCloseTime: t });
    }

    return map;
  }, [events, earnings, holidaysData]);

  const navigate = useCallback((direction: -1 | 0 | 1) => {
    if (direction === 0) { setCurrentDate(new Date()); return; }
    setCurrentDate((prev) => view === "week" ? addDays(prev, direction * 7) : new Date(prev.getFullYear(), prev.getMonth() + direction, 1));
  }, [view]);

  const handleRemoveHoliday = useCallback((dateStr: string) => {
    if (confirm(`Remove holiday on ${dateStr}?`)) removeMutation.mutate(dateStr);
  }, [removeMutation]);

  const titleText = useMemo(() => {
    if (view === "week") {
      const ws = startOfWeek(currentDate);
      const we = addDays(ws, 6);
      const fmt = (d: Date) => d.toLocaleDateString("en-IN", { month: "short", day: "numeric" });
      return `${fmt(ws)} – ${fmt(we)}, ${we.getFullYear()}`;
    }
    return `${MONTH_NAMES[currentDate.getMonth()]} ${currentDate.getFullYear()}`;
  }, [view, currentDate]);

  // Compute summary for current view
  const viewPnlSummary = useMemo(() => {
    let totalPnl = 0;
    let totalTrades = 0;
    let totalWins = 0;
    let totalLosses = 0;
    let tradingDays = 0;

    const checkDate = (ds: string) => {
      const p = pnlMap.get(ds);
      if (p) {
        totalPnl += p.pnl;
        totalTrades += p.trade_count;
        totalWins += p.wins;
        totalLosses += p.losses;
        tradingDays++;
      }
    };

    if (view === "week") {
      const ws = startOfWeek(currentDate);
      for (let i = 0; i < 7; i++) checkDate(toDateStr(addDays(ws, i)));
    } else {
      const ms = startOfMonth(currentDate);
      const daysInMonth = new Date(currentDate.getFullYear(), currentDate.getMonth() + 1, 0).getDate();
      for (let i = 0; i < daysInMonth; i++) checkDate(toDateStr(addDays(ms, i)));
    }

    return { totalPnl, totalTrades, totalWins, totalLosses, tradingDays };
  }, [view, currentDate, pnlMap]);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <button onClick={() => navigate(0)}
            className="px-3 py-1.5 rounded text-sm bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700">Today</button>
          <div className="flex">
            <button onClick={() => navigate(-1)}
              className="px-2 py-1.5 rounded-l text-sm bg-gray-800 hover:bg-gray-700 text-gray-400 border border-gray-700">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" /></svg>
            </button>
            <button onClick={() => navigate(1)}
              className="px-2 py-1.5 rounded-r text-sm bg-gray-800 hover:bg-gray-700 text-gray-400 border border-gray-700 border-l-0">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
            </button>
          </div>
          <h2 className="text-lg font-semibold text-gray-200 ml-2">{titleText}</h2>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowPnl(!showPnl)}
            className={clsx(
              "px-3 py-1.5 rounded text-sm border",
              showPnl
                ? "bg-purple-900/40 text-purple-300 border-purple-800/50"
                : "bg-gray-800 text-gray-500 border-gray-700 hover:text-gray-300",
            )}
          >
            {showPnl ? "PnL On" : "PnL Off"}
          </button>
          <button onClick={() => setAddHolidayDate(toDateStr(new Date()))}
            className="px-3 py-1.5 rounded text-sm bg-red-900/40 hover:bg-red-900/60 text-red-300 border border-red-800/50">+ Holiday</button>
          <div className="flex rounded overflow-hidden border border-gray-700">
            <button onClick={() => setView("week")}
              className={clsx("px-3 py-1.5 text-sm", view === "week" ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700")}>Week</button>
            <button onClick={() => setView("month")}
              className={clsx("px-3 py-1.5 text-sm border-l border-gray-700", view === "month" ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700")}>Month</button>
          </div>
        </div>
      </div>

      {/* PnL Summary Bar */}
      {showPnl && viewPnlSummary.tradingDays > 0 && (
        <div className="flex items-center gap-4 px-4 py-2.5 bg-gray-900 border border-gray-800 rounded-lg text-sm">
          <span className="text-gray-500">{view === "week" ? "Week" : "Month"} P&L:</span>
          <span className={clsx("font-semibold", viewPnlSummary.totalPnl >= 0 ? "text-emerald-400" : "text-red-400")}>
            {formatInr(viewPnlSummary.totalPnl)}
          </span>
          <span className="text-gray-600">|</span>
          <span className="text-gray-400">{viewPnlSummary.totalTrades} trades</span>
          <span className="text-emerald-500">{viewPnlSummary.totalWins}W</span>
          <span className="text-red-500">{viewPnlSummary.totalLosses}L</span>
          <span className="text-gray-600">|</span>
          <span className="text-gray-500">{viewPnlSummary.tradingDays} trading days</span>
        </div>
      )}

      {/* Legend */}
      <div className="flex flex-wrap gap-4 text-xs text-gray-400">
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-red-400" /> Holiday</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-amber-400" /> Early Close</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-blue-400" /> Economic</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-emerald-400" /> Earnings</span>
        <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded bg-gray-800/60 border border-gray-700" /> <span className="text-gray-500">Weekend</span></span>
        {showPnl && (
          <>
            <span className="flex items-center gap-1.5">
              <span
                className="w-3 h-3 rounded"
                style={{ backgroundColor: isLight ? "rgba(26, 127, 55, 0.45)" : "rgba(34, 197, 94, 0.3)" }}
              /> Profit
            </span>
            <span className="flex items-center gap-1.5">
              <span
                className="w-3 h-3 rounded"
                style={{ backgroundColor: isLight ? "rgba(207, 34, 46, 0.45)" : "rgba(239, 68, 68, 0.3)" }}
              /> Loss
            </span>
          </>
        )}
      </div>

      {/* Calendar */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {view === "week" ? (
          <WeekView
            currentDate={currentDate}
            eventsMap={eventsMap}
            pnlMap={pnlMap}
            maxAbsPnl={maxAbsPnl}
            showPnl={showPnl}
            isLight={isLight}
            onAddHoliday={setAddHolidayDate}
            onRemoveHoliday={handleRemoveHoliday}
          />
        ) : (
          <MonthView
            currentDate={currentDate}
            eventsMap={eventsMap}
            pnlMap={pnlMap}
            maxAbsPnl={maxAbsPnl}
            showPnl={showPnl}
            isLight={isLight}
            onAddHoliday={setAddHolidayDate}
            onRemoveHoliday={handleRemoveHoliday}
          />
        )}
      </div>

      <AddHolidayDialog dateStr={addHolidayDate} onClose={() => setAddHolidayDate(null)} />
    </div>
  );
}
