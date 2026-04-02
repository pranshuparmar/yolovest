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

/** Format a UTC datetime string to IST display (date + time). */
export function formatIST(iso: string): string {
  try {
    return parseUTC(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
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

/** Format a UTC datetime string to IST display (date only). */
export function formatISTDate(iso: string): string {
  try {
    return parseUTC(iso).toLocaleDateString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

/** Format a UTC datetime string to IST display (time only). */
export function formatISTTime(iso: string): string {
  try {
    return parseUTC(iso).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
