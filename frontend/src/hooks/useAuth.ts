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
  login: (password: string) => void;
  logout: () => void;
  isAuthenticated: boolean;
  authHeader: string | null;
}

export const AuthContext = createContext<AuthContextType>({
  password: null,
  login: () => {},
  logout: () => {},
  isAuthenticated: false,
  authHeader: null,
});

export function useAuth() {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [password, setPassword] = useState<string | null>(() =>
    sessionStorage.getItem("yv_password")
  );

  const login = useCallback((pw: string) => {
    sessionStorage.setItem("yv_password", pw);
    setPassword(pw);
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem("yv_password");
    setPassword(null);
  }, []);

  const authHeader = useMemo(
    () => (password ? "Basic " + btoa(":" + password) : null),
    [password]
  );

  const value = useMemo(
    () => ({
      password,
      login,
      logout,
      isAuthenticated: !!password,
      authHeader,
    }),
    [password, login, logout, authHeader]
  );

  return React.createElement(AuthContext.Provider, { value }, children);
}
