let _authHeader: string | null = null;
let _onUnauthorized: (() => void) | null = null;

export function setAuthHeader(header: string | null) {
  _authHeader = header;
}

export function setOnUnauthorized(fn: () => void) {
  _onUnauthorized = fn;
}

export async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string>),
  };

  if (_authHeader) {
    headers["Authorization"] = _authHeader;
  }

  const res = await fetch(path, { ...options, headers });

  if (res.status === 401) {
    _onUnauthorized?.();
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }

  return res.json();
}
