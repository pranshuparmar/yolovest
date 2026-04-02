/**
 * Timezone-aware datetime utilities for YoloVest.
 *
 * Backend stores all timestamps in UTC. Frontend converts to display timezone
 * following this priority:
 * 1. Config's market_hours.timezone (from /api/health endpoint)
 * 2. Browser's local timezone (Intl.DateTimeFormat().resolvedOptions().timeZone)
 * 3. "UTC" as fallback
 */

/** Display timezone — set by initTimezone() from the health endpoint. */
let _displayTz: string = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

/**
 * Initialize the display timezone from config.
 * Call once on app startup with the timezone from /api/health.
 */
export function initTimezone(tz: string | undefined) {
  if (tz) {
    _displayTz = tz;
  }
}

/** Get the current display timezone. */
export function getTimezone(): string {
  return _displayTz;
}

/**
 * Parse a datetime string as UTC.
 *
 * Backend stores timestamps via SQLite datetime('now') which produces UTC
 * strings without a timezone suffix (e.g. "2026-04-02 10:30:00").
 * JavaScript's Date constructor interprets these as LOCAL time, which is wrong.
 *
 * This function ensures the string is parsed as UTC by appending 'Z' if no
 * timezone indicator is present.
 */
export function parseUTC(iso: string): Date {
  if (!iso) return new Date(NaN);
  // Already has timezone info (Z, +HH:MM, -HH:MM)
  if (/[Zz]$/.test(iso) || /[+-]\d{2}:\d{2}$/.test(iso)) {
    return new Date(iso);
  }
  // Replace space separator with T for ISO compliance, append Z for UTC
  const normalized = iso.includes("T") ? iso : iso.replace(" ", "T");
  return new Date(normalized + "Z");
}

/** Format a UTC datetime string for display (date + time). */
export function formatIST(iso: string): string {
  try {
    return parseUTC(iso).toLocaleString("en-IN", {
      timeZone: _displayTz,
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** Format a UTC datetime string for display (date only). */
export function formatISTDate(iso: string): string {
  try {
    return parseUTC(iso).toLocaleDateString("en-IN", {
      timeZone: _displayTz,
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

/** Format a UTC datetime string for display (time only). */
export function formatISTTime(iso: string): string {
  try {
    return parseUTC(iso).toLocaleTimeString("en-IN", {
      timeZone: _displayTz,
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
