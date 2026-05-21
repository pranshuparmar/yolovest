import { useState } from "react";
import { useAuth } from "../hooks/useAuth";

export function LoginPage() {
  const { login } = useAuth();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const ok = await login(password);
      if (!ok) {
        setError("Invalid password");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center">
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 w-full max-w-sm">
        <h1 className="text-xl font-bold text-blue-400 mb-1">YoloVest</h1>
        <p className="text-sm text-gray-500 mb-6">Trading Dashboard</p>
        <form onSubmit={handleSubmit}>
          <label className="block text-sm text-gray-400 mb-2">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500 mb-4"
            placeholder="Enter dashboard password"
            autoFocus
          />
          {error && (
            <p className="text-red-400 text-xs mb-3">{error}</p>
          )}
          <button
            type="submit"
            disabled={loading || !password}
            className="w-full bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded py-2 text-sm font-medium"
          >
            {loading ? "Connecting..." : "Login"}
          </button>
        </form>
      </div>
    </div>
  );
}
