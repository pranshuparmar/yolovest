/**
 * Minimal structural guards for API responses.
 *
 * The dashboard's TS types are hand-maintained with no compile-time contract to
 * the backend's (mostly `dict[str, Any]`) endpoints, so a renamed/removed field
 * or a wrong-shape response surfaces only as a downstream `.map`/`.field` crash
 * — which, on a live trading view, used to white-screen the whole SPA.
 *
 * These guards assert only the shape CATEGORY (object vs array) at the fetch
 * boundary. They can never reject an otherwise-valid object/array, but they DO
 * catch the catastrophic drift (an endpoint returning null, an error envelope,
 * or the wrong category) and turn it into a clear, caught error that the route's
 * ErrorBoundary renders instead of a blank page. Pass one as apiFetch's third
 * argument on the critical money endpoints.
 */

function describe(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v;
}

export function expectObject<T>(raw: unknown): T {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`Unexpected API response: expected an object, got ${describe(raw)}`);
  }
  return raw as T;
}

export function expectArray<T>(raw: unknown): T {
  if (!Array.isArray(raw)) {
    throw new Error(`Unexpected API response: expected an array, got ${describe(raw)}`);
  }
  return raw as T;
}
