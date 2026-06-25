import { useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./ErrorBoundary";
import { Sidebar } from "./Sidebar";
import { StatusBadge } from "./StatusBadge";
import {
  NotificationCenter,
  useNotifications,
} from "./NotificationCenter";
import { QuickReviewFloater } from "./QuickReviewFloater";
import { useAuth } from "../hooks/useAuth";
import { useTheme } from "../hooks/useTheme";

export function Layout() {
  const { logout } = useAuth();
  const { theme, toggle } = useTheme();
  const { notifications, clearAll, dismiss } = useNotifications();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-gray-950">
      {/* Full-width header */}
      <header className="h-12 bg-gray-900 border-b border-gray-800 flex items-center justify-between px-4 shrink-0 z-10">
        <div className="flex items-center gap-4">
          {/* Mobile sidebar toggle */}
          <button
            onClick={() => setMobileOpen(true)}
            className="md:hidden text-gray-400 hover:text-gray-200"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 6h16M4 12h16M4 18h16"
              />
            </svg>
          </button>
          {/* Brand */}
          <h1 className="text-sm font-bold text-blue-400 hidden md:block">YoloVest</h1>
          <StatusBadge />
        </div>
        <div className="flex items-center gap-3">
          {/* Theme toggle */}
          <button
            onClick={toggle}
            className="text-gray-400 hover:text-gray-200 p-1"
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? (
              <svg
                className="w-4 h-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"
                />
              </svg>
            ) : (
              <svg
                className="w-4 h-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"
                />
              </svg>
            )}
          </button>
          <NotificationCenter
            notifications={notifications}
            clearAll={clearAll}
            dismiss={dismiss}
          />
          <button
            onClick={logout}
            className="text-xs text-gray-500 hover:text-gray-300"
          >
            Logout
          </button>
        </div>
      </header>

      {/* Sidebar + content below header */}
      <div className="flex flex-1 min-h-0">
        <Sidebar
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
          mobileOpen={mobileOpen}
          onMobileClose={() => setMobileOpen(false)}
        />
        <main className="flex-1 p-3 sm:p-4 md:p-6 overflow-auto">
          {/* Per-route boundary: a crash in one page shows a recoverable
              fallback without taking down the nav shell. Keyed by path so
              navigating elsewhere clears a stuck error. */}
          <ErrorBoundary key={location.pathname} scope="page">
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <QuickReviewFloater />
    </div>
  );
}
