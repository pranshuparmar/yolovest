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

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [password, setPassword] = useState<string | null>(() =>
    sessionStorage.getItem("yv_password")
  );
  const [token, setToken] = useState<string | null>(() =>
    sessionStorage.getItem("yv_token")
  );
  const [csrfToken, setCsrfToken] = useState<string | null>(() =>
    sessionStorage.getItem("yv_csrf")
  );

  const login = useCallback(async (pw: string): Promise<boolean> => {
    // Try session token login first
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      if (res.ok) {
        const data = await res.json();
        sessionStorage.setItem("yv_token", data.token);
        sessionStorage.setItem("yv_csrf", data.csrf_token);
        sessionStorage.setItem("yv_password", pw);
        setToken(data.token);
        setCsrfToken(data.csrf_token);
        setPassword(pw);
        return true;
      }
    } catch {
      // Fall back to basic auth (server might be older version)
    }
    // Fallback: store password for basic auth
    sessionStorage.setItem("yv_password", pw);
    setPassword(pw);
    return true;
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem("yv_password");
    sessionStorage.removeItem("yv_token");
    sessionStorage.removeItem("yv_csrf");
    setPassword(null);
    setToken(null);
    setCsrfToken(null);
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
