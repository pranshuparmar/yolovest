import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { StatusBadge } from "./StatusBadge";
import { useAuth } from "../hooks/useAuth";

export function Layout() {
  const { logout } = useAuth();

  return (
    <div className="flex min-h-screen bg-gray-950">
      <Sidebar />
      <div className="flex-1 flex flex-col">
        <header className="h-12 border-b border-gray-800 flex items-center justify-between px-4">
          <StatusBadge />
          <button
            onClick={logout}
            className="text-xs text-gray-500 hover:text-gray-300"
          >
            Logout
          </button>
        </header>
        <main className="flex-1 p-6 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
