import { useState } from "react";
import {
  useIntegrations,
  usePingGemini,
  useAuthenticateZerodha,
  useLogoutZerodha,
  useTestTelegram,
  useSendTelegram,
  useChangePassword,
  useUpdateCapital,
  useSyncCapital,
  useUpdateConfig,
} from "../hooks/queries";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../hooks/useAuth";

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${
        ok ? "bg-emerald-400" : "bg-red-400"
      }`}
    />
  );
}

function Badge({ label, color }: { label: string; color: string }) {
  const colors: Record<string, string> = {
    green: "bg-emerald-900/40 text-emerald-400",
    red: "bg-red-900/40 text-red-400",
    amber: "bg-amber-900/40 text-amber-400",
    blue: "bg-blue-900/40 text-blue-400",
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${colors[color] ?? colors.blue}`}>
      {label}
    </span>
  );
}

function ActionButton({
  onClick,
  loading,
  children,
  variant = "default",
}: {
  onClick: () => void;
  loading: boolean;
  children: React.ReactNode;
  variant?: "default" | "primary" | "danger";
}) {
  const base =
    "px-3 py-1.5 rounded text-sm font-medium disabled:opacity-50 transition-colors";
  const styles =
    variant === "primary"
      ? `${base} bg-emerald-600 hover:bg-emerald-700 text-white`
      : variant === "danger"
        ? `${base} bg-red-700 hover:bg-red-600 text-white`
        : `${base} bg-gray-700 hover:bg-gray-600 text-gray-200`;
  return (
    <button onClick={onClick} disabled={loading} className={styles}>
      {loading ? "..." : children}
    </button>
  );
}

function ResultToast({ success, error }: { success?: boolean; error?: string }) {
  if (success === undefined) return null;
  return (
    <p className={`text-xs mt-2 ${success ? "text-emerald-400" : "text-red-400"}`}>
      {success ? "Success" : error || "Failed"}
    </p>
  );
}

export function IntegrationsPage() {
  const { data, isLoading } = useIntegrations();

  const pingGemini = usePingGemini();
  const authZerodha = useAuthenticateZerodha();
  const logoutZerodha = useLogoutZerodha();
  const testTelegram = useTestTelegram();
  const sendTelegram = useSendTelegram();
  const updateConfig = useUpdateConfig();
  const qc = useQueryClient();

  const toggleEnabled = (key: string, current: boolean) => {
    updateConfig.mutate(
      { [key]: !current },
      { onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }) },
    );
  };

  const { login } = useAuth();
  const changePassword = useChangePassword();
  const updateCapital = useUpdateCapital();
  const syncCapital = useSyncCapital();

  const [requestToken, setRequestToken] = useState("");
  const [telegramMsg, setTelegramMsg] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [capitalAmount, setCapitalAmount] = useState("");

  // Check for OAuth callback result in URL params
  const [authResult, setAuthResult] = useState<string | null>(null);
  useState(() => {
    const params = new URLSearchParams(window.location.search);
    const auth = params.get("zerodha_auth");
    if (auth) {
      setAuthResult(auth);
      // Clean URL
      window.history.replaceState({}, "", window.location.pathname);
    }
  });

  if (isLoading || !data) {
    return (
      <div className="p-6 space-y-4">
        <h2 className="text-lg font-semibold text-gray-100">Settings</h2>
        <div className="grid gap-4 md:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-56 bg-gray-900 rounded-lg animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  const { gemini, zerodha, telegram } = data;

  return (
    <div className="p-6 space-y-4">
      <h2 className="text-lg font-semibold text-gray-100">Settings</h2>

      {authResult && (
        <div
          className={`rounded-lg p-3 text-sm ${
            authResult === "success"
              ? "bg-emerald-900/20 border border-emerald-800 text-emerald-400"
              : "bg-red-900/20 border border-red-800 text-red-400"
          }`}
        >
          {authResult === "success"
            ? "Zerodha authenticated successfully! You can now trade."
            : "Zerodha authentication failed. Please try again."}
          <button
            onClick={() => setAuthResult(null)}
            className="ml-3 text-xs opacity-60 hover:opacity-100"
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        {/* ---- Gemini LLM ---- */}
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="font-medium text-gray-100">Gemini LLM</h3>
            <StatusDot ok={gemini.connected} />
          </div>

          <div className="space-y-2 text-sm text-gray-400">
            <div className="flex justify-between">
              <span>Status</span>
              <Badge
                label={
                  !gemini.enabled
                    ? "Inactive"
                    : gemini.connected
                    ? "Connected"
                    : gemini.configured
                    ? "Disconnected"
                    : "Not configured"
                }
                color={
                  !gemini.enabled
                    ? "amber"
                    : gemini.connected
                    ? "green"
                    : gemini.configured
                    ? "red"
                    : "amber"
                }
              />
            </div>
            {gemini.model && (
              <div className="flex justify-between">
                <span>Model</span>
                <span className="text-gray-300">{gemini.model}</span>
              </div>
            )}
          </div>

          <div className="mt-auto pt-3 border-t border-gray-800 space-y-2">
            <div className="flex gap-2">
              <ActionButton
                onClick={() => pingGemini.mutate()}
                loading={pingGemini.isPending}
                variant="primary"
              >
                Test Connection
              </ActionButton>
              <ActionButton
                onClick={() => toggleEnabled("llm.enabled", gemini.enabled)}
                loading={updateConfig.isPending}
                variant={gemini.enabled ? "danger" : "default"}
              >
                {gemini.enabled ? "Mark Inactive" : "Mark Active"}
              </ActionButton>
            </div>
            {pingGemini.data && (
              <ResultToast success={pingGemini.data.success} error={pingGemini.data.error} />
            )}
          </div>
        </div>

        {/* ---- Zerodha Broker ---- */}
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="font-medium text-gray-100">Zerodha Kite</h3>
            <StatusDot ok={zerodha.connected} />
          </div>

          <div className="space-y-2 text-sm text-gray-400">
            <div className="flex justify-between">
              <span>Status</span>
              <Badge
                label={zerodha.connected ? "Authenticated" : zerodha.configured ? "Not authenticated" : "Not configured"}
                color={zerodha.connected ? "green" : zerodha.configured ? "red" : "amber"}
              />
            </div>
            <div className="flex justify-between">
              <span>Mode</span>
              <Badge
                label={zerodha.mode}
                color={zerodha.mode === "live" ? "red" : "blue"}
              />
            </div>
            {zerodha.margins && (
              <div className="flex justify-between">
                <span>Available</span>
                <span className="text-gray-300">
                  {"\u20B9"}
                  {(
                    (zerodha.margins as Record<string, Record<string, number>>)?.available
                      ?.cash ?? 0
                  ).toLocaleString("en-IN")}
                </span>
              </div>
            )}
          </div>

          <div className="mt-auto pt-3 border-t border-gray-800 space-y-2">
            {zerodha.login_url && (
              zerodha.connected ? (
                // Authenticated: compact Re-auth + Logout pair, same
                // layout shape as the Telegram card's two-button row.
                <div className="flex gap-2">
                  <a
                    href={zerodha.login_url}
                    className="px-3 py-1.5 rounded text-sm font-medium bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
                  >
                    Re-auth
                  </a>
                  <ActionButton
                    onClick={() => {
                      if (!window.confirm(
                        "Drop the cached Kite token? Trading will be blocked until you re-authenticate, and the live tick stream will stop.",
                      )) return;
                      logoutZerodha.mutate();
                    }}
                    loading={logoutZerodha.isPending}
                    variant="danger"
                  >
                    Logout
                  </ActionButton>
                </div>
              ) : (
                // Not authenticated: full-width Login call to action.
                <a
                  href={zerodha.login_url}
                  className="block text-center px-3 py-1.5 rounded text-sm font-medium bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
                >
                  Login to Kite
                </a>
              )
            )}
            <div className="flex gap-2">
              <input
                type="text"
                placeholder="Request token"
                value={requestToken}
                onChange={(e) => setRequestToken(e.target.value)}
                className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-emerald-600"
              />
              <ActionButton
                onClick={() => {
                  authZerodha.mutate(requestToken);
                  setRequestToken("");
                }}
                loading={authZerodha.isPending}
                variant="primary"
              >
                Auth
              </ActionButton>
            </div>
            {authZerodha.data && (
              <ResultToast success={authZerodha.data.success} error={authZerodha.data.error} />
            )}
          </div>
        </div>

        {/* ---- Telegram Bot ---- */}
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="font-medium text-gray-100">Telegram</h3>
            <StatusDot ok={telegram.enabled && telegram.configured} />
          </div>

          <div className="space-y-2 text-sm text-gray-400">
            <div className="flex justify-between">
              <span>Status</span>
              <Badge
                label={
                  telegram.enabled && telegram.configured
                    ? "Active"
                    : telegram.configured
                    ? "Disabled"
                    : "Not configured"
                }
                color={
                  telegram.enabled && telegram.configured
                    ? "green"
                    : telegram.configured
                    ? "amber"
                    : "red"
                }
              />
            </div>
            {telegram.chat_id && (
              <div className="flex justify-between">
                <span>Chat ID</span>
                <span className="text-gray-300 font-mono text-xs">{telegram.chat_id}</span>
              </div>
            )}
            {telegram.hint && (
              <p className="text-xs text-amber-400 bg-amber-900/20 rounded px-2 py-1.5 leading-relaxed">
                {telegram.hint}
              </p>
            )}
          </div>

          <div className="mt-auto pt-3 border-t border-gray-800 space-y-2">
            <div className="flex gap-2">
              <ActionButton
                onClick={() => testTelegram.mutate()}
                loading={testTelegram.isPending}
              >
                Send Test Message
              </ActionButton>
              <ActionButton
                onClick={() =>
                  toggleEnabled("notifications.telegram.enabled", telegram.enabled)
                }
                loading={updateConfig.isPending}
                variant={telegram.enabled ? "danger" : "default"}
              >
                {telegram.enabled ? "Mark Inactive" : "Mark Active"}
              </ActionButton>
            </div>
            {testTelegram.data && (
              <ResultToast success={testTelegram.data.success} error={testTelegram.data.error} />
            )}

            <div className="flex gap-2">
              <input
                type="text"
                placeholder="Custom message"
                value={telegramMsg}
                onChange={(e) => setTelegramMsg(e.target.value)}
                className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-emerald-600"
              />
              <ActionButton
                onClick={() => {
                  sendTelegram.mutate(telegramMsg);
                  setTelegramMsg("");
                }}
                loading={sendTelegram.isPending}
                variant="primary"
              >
                Send
              </ActionButton>
            </div>
            {sendTelegram.data && (
              <ResultToast success={sendTelegram.data.success} error={sendTelegram.data.error} />
            )}
          </div>
        </div>
      </div>

      {/* Settings */}
      <div className="grid gap-4 md:grid-cols-2">
        {/* Password Change */}
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 flex flex-col gap-3">
          <h3 className="font-medium text-gray-100">Dashboard Password</h3>
          <div className="flex gap-2">
            <input
              type="password"
              placeholder="New password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-emerald-600"
            />
            <ActionButton
              onClick={() => {
                changePassword.mutate(newPassword, {
                  onSuccess: () => {
                    login(newPassword);
                    setNewPassword("");
                  },
                });
              }}
              loading={changePassword.isPending}
              variant="primary"
            >
              Update
            </ActionButton>
          </div>
          {changePassword.data && (
            <ResultToast success={changePassword.data.success} />
          )}
          {changePassword.error && (
            <ResultToast success={false} error="Password must be at least 4 characters" />
          )}
        </div>

        {/* Capital Management */}
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 flex flex-col gap-3">
          <h3 className="font-medium text-gray-100">Capital</h3>
          <div className="flex gap-2">
            <input
              type="number"
              placeholder="Amount (INR)"
              value={capitalAmount}
              onChange={(e) => setCapitalAmount(e.target.value)}
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-emerald-600"
            />
            <ActionButton
              onClick={() => {
                updateCapital.mutate(Number(capitalAmount), {
                  onSuccess: () => setCapitalAmount(""),
                });
              }}
              loading={updateCapital.isPending}
              variant="primary"
            >
              Set
            </ActionButton>
          </div>
          <ActionButton
            onClick={() => syncCapital.mutate()}
            loading={syncCapital.isPending}
          >
            Sync from Zerodha
          </ActionButton>
          {syncCapital.data && (
            <ResultToast
              success={syncCapital.data.success}
              error={syncCapital.data.error}
            />
          )}
        </div>
      </div>
    </div>
  );
}
