import {
  createContext,
  useContext,
  useState,
  useCallback,
  useMemo,
} from "react";
import React from "react";

interface AuthContextType {
  login: (password: string) => Promise<boolean>;
  logout: () => void;
  isAuthenticated: boolean;
  authHeader: string | null;
  csrfToken: string | null;
}

export const AuthContext = createContext<AuthContextType>({
  login: async () => false,
  logout: () => {},
  isAuthenticated: false,
  authHeader: null,
  csrfToken: null,
});

export function useAuth() {
  return useContext(AuthContext);
}

// localStorage so a fresh tab inherits the auth from existing ones.
// `storage` events fired by the browser keep state in sync across tabs.
// Only the signed session token is persisted — never the password.
// Tokens are invalidated on backend restart (per-process signing key),
// which simply forces a re-login.
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(() =>
    localStorage.getItem("yv_token")
  );
  const [csrfToken, setCsrfToken] = useState<string | null>(() =>
    localStorage.getItem("yv_csrf")
  );

  const login = useCallback(async (pw: string): Promise<boolean> => {
    let res: Response;
    try {
      res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
    } catch {
      // Network error — surface to caller so the login form can show
      // "Cannot connect to server".
      throw new Error("Cannot connect to server");
    }
    if (res.ok) {
      const data = await res.json();
      localStorage.setItem("yv_token", data.token);
      localStorage.setItem("yv_csrf", data.csrf_token);
      // One-time cleanup: older builds persisted the raw password.
      localStorage.removeItem("yv_password");
      setToken(data.token);
      setCsrfToken(data.csrf_token);
      return true;
    }
    if (res.status === 401) {
      return false;
    }
    if (res.status === 429) {
      throw new Error("Too many failed attempts — wait a moment and retry");
    }
    throw new Error(`Login failed (${res.status})`);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("yv_password");
    localStorage.removeItem("yv_token");
    localStorage.removeItem("yv_csrf");
    setToken(null);
    setCsrfToken(null);
  }, []);

  // Keep tabs in sync — when one tab logs in or out, the others pick
  // it up via the storage event and re-render.
  React.useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === "yv_token") setToken(e.newValue);
      else if (e.key === "yv_csrf") setCsrfToken(e.newValue);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const authHeader = useMemo(
    () => (token ? "Bearer " + token : null),
    [token]
  );

  const value = useMemo(
    () => ({
      login,
      logout,
      isAuthenticated: !!token,
      authHeader,
      csrfToken,
    }),
    [token, login, logout, authHeader, csrfToken]
  );

  return React.createElement(AuthContext.Provider, { value }, children);
}
