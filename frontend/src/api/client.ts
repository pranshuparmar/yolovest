let _authHeader: string | null = null;
let _csrfToken: string | null = null;
let _onUnauthorized: (() => void) | null = null;

export function setAuthHeader(header: string | null) {
  _authHeader = header;
}

export function setCsrfToken(token: string | null) {
  _csrfToken = token;
}

export function setOnUnauthorized(fn: () => void) {
  _onUnauthorized = fn;
}

export async function apiFetch<T>(
  path: string,
  options?: RequestInit,
  // Optional structural guard run on the parsed JSON (see api/validate.ts).
  // Lets the critical money endpoints fail loudly on shape drift at the fetch
  // boundary instead of crashing a downstream render.
  validate?: (raw: unknown) => T
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string>),
  };

  if (_authHeader) {
    headers["Authorization"] = _authHeader;
  }

  // Include CSRF token on state-changing requests
  const method = (options?.method || "GET").toUpperCase();
  if (_csrfToken && ["POST", "PUT", "DELETE"].includes(method)) {
    headers["X-CSRF-Token"] = _csrfToken;
  }

  const res = await fetch(path, { ...options, headers });

  if (res.status === 401) {
    _onUnauthorized?.();
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    // Try to parse the FastAPI error body so structured detail (e.g.
    // the CDSL TPIN authorisation prompt) is available to callers.
    // detail can be a string or an object — for objects we attach
    // the parsed body to the Error so consumers can branch on it.
    let detail: unknown = null;
    try {
      const body = await res.json();
      detail = body?.detail ?? body;
    } catch {
      // not JSON, fall through to generic message
    }
    const detailMsg = typeof detail === "string"
      ? detail
      : `API error: ${res.status} ${res.statusText}`;
    const err = new Error(detailMsg) as Error & { status?: number; detail?: unknown };
    err.status = res.status;
    err.detail = detail;
    throw err;
  }

  const data = await res.json();
  return validate ? validate(data) : (data as T);
}

function _authHeaders(includeCsrf: boolean): Record<string, string> {
  const headers: Record<string, string> = {};
  if (_authHeader) headers["Authorization"] = _authHeader;
  if (includeCsrf && _csrfToken) headers["X-CSRF-Token"] = _csrfToken;
  return headers;
}

/** Download a file from `path` and trigger a browser "Save as" with
 *  `suggestedName`. Used for backup / model / config exports.
 *
 *  Streams straight to disk via a native <a download> rather than
 *  fetch()+blob(): a blob buffers the ENTIRE payload in browser memory
 *  before the save dialog, which stalls (and can OOM) on multi-hundred-MB
 *  DB backups. A native download can't carry the Authorization header, so
 *  we first mint a short-lived token and pass it as a query param. The
 *  server's Content-Disposition still drives the actual filename. */
export async function apiDownload(path: string, suggestedName: string): Promise<void> {
  const { token } = await apiFetch<{ token: string }>("/api/download-token");
  const sep = path.includes("?") ? "&" : "?";
  const url = `${path}${sep}token=${encodeURIComponent(token)}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = suggestedName;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Upload a file via multipart/form-data. The browser sets the
 *  Content-Type (with boundary) itself, so we must NOT set it. */
export async function apiUpload<T>(path: string, file: File, field = "file"): Promise<T> {
  const form = new FormData();
  form.append(field, file);
  const res = await fetch(path, {
    method: "POST",
    headers: _authHeaders(true),
    body: form,
  });
  if (res.status === 401) {
    _onUnauthorized?.();
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    let detail: unknown = null;
    try {
      const body = await res.json();
      detail = body?.detail ?? body;
    } catch {
      // not JSON
    }
    const msg = typeof detail === "string" ? detail : `Upload failed: ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}
