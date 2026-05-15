import {
  createContext,
  useContext,
  useState,
  useCallback,
  useMemo,
} from "react";
import React from "react";

interface AuthContextType {
  password: string | null;
  login: (password: string) => Promise<boolean>;
  logout: () => void;
  isAuthenticated: boolean;
  authHeader: string | null;
  csrfToken: string | null;
}

export const AuthContext = createContext<AuthContextType>({
  password: null,
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
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [password, setPassword] = useState<string | null>(() =>
    localStorage.getItem("yv_password")
  );
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
      localStorage.setItem("yv_password", pw);
      setToken(data.token);
      setCsrfToken(data.csrf_token);
      setPassword(pw);
      return true;
    }
    if (res.status === 401) {
      return false;
    }
    // Older server build that doesn't have /api/auth/login at all
    // (404). Fall back to HTTP Basic — the next protected fetch will
    // reject if the password is actually wrong, but at least the
    // app boots against legacy backends.
    if (res.status === 404) {
      localStorage.setItem("yv_password", pw);
      setPassword(pw);
      return true;
    }
    throw new Error(`Login failed (${res.status})`);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("yv_password");
    localStorage.removeItem("yv_token");
    localStorage.removeItem("yv_csrf");
    setPassword(null);
    setToken(null);
    setCsrfToken(null);
  }, []);

  // Keep tabs in sync — when one tab logs in or out, the others pick
  // it up via the storage event and re-render.
  React.useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === "yv_token") setToken(e.newValue);
      else if (e.key === "yv_password") setPassword(e.newValue);
      else if (e.key === "yv_csrf") setCsrfToken(e.newValue);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const authHeader = useMemo(() => {
    if (token) return "Bearer " + token;
    if (password) return "Basic " + btoa(":" + password);
    return null;
  }, [token, password]);

  const value = useMemo(
    () => ({
      password,
      login,
      logout,
      isAuthenticated: !!(token || password),
      authHeader,
      csrfToken,
    }),
    [password, token, login, logout, authHeader, csrfToken]
  );

  return React.createElement(AuthContext.Provider, { value }, children);
}
