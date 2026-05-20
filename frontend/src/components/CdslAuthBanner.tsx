import { useSystemState } from "../hooks/queries";

/**
 * Shown on the Dashboard when the cdsl-auth-check skill (or a live
 * status refresh) reports holdings that still need CDSL TPIN auth
 * before delivery sells will go through. DDPI users (and anyone
 * who's already authorised today) never see this — the system_state
 * `cdsl_auth` field carries needs_auth=false in those cases.
 */
export function CdslAuthBanner() {
  const { data: state } = useSystemState();
  const cdsl = state?.cdsl_auth;

  if (!cdsl || !cdsl.authenticated || !cdsl.needs_auth) return null;

  const pendingSyms = (cdsl.pending_symbols ?? []).slice(0, 6).map((s) => s.symbol).join(", ");
  const more = (cdsl.pending_count ?? 0) - 6;
  const symBlurb = more > 0 ? `${pendingSyms}, +${more} more` : pendingSyms;

  const handleOpenAuth = async () => {
    // Hitting the proactive endpoint mirrors what the OrderForm does
    // when an actual sell fails — it returns the same auth_url shape.
    try {
      const res = await fetch("/api/broker/holdings-auth", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "same-origin",
        body: "{}",
      });
      const body = await res.json();
      if (body?.auth_url) {
        window.open(body.auth_url, "_blank", "noopener,noreferrer");
        return;
      }
    } catch {
      // fall through to static URL
    }
    window.open("https://kite.zerodha.com/#holdings", "_blank", "noopener,noreferrer");
  };

  return (
    <div className="bg-amber-900/30 border border-amber-700/50 rounded-lg p-4 relative">
      <div className="flex items-start gap-3">
        <span className="text-amber-400 text-lg shrink-0">!</span>
        <div className="flex-1 min-w-0">
          <p className="text-amber-300 font-semibold text-sm">
            CDSL TPIN authorisation pending
          </p>
          <p className="text-gray-400 text-xs mt-1">
            {cdsl.pending_qty} share{cdsl.pending_qty === 1 ? "" : "s"} across{" "}
            {cdsl.pending_count} symbol{cdsl.pending_count === 1 ? "" : "s"} still need
            authorising before delivery sells can be placed today.
            {pendingSyms && <span className="ml-1 text-gray-500">({symBlurb})</span>}
          </p>
          <div className="flex flex-wrap items-center gap-2 mt-3">
            <button
              onClick={handleOpenAuth}
              className="px-3 py-1.5 rounded text-xs font-medium bg-amber-600 hover:bg-amber-500 text-white"
            >
              Open CDSL auth
            </button>
            <a
              href="https://zerodha.com/cdsl-tpin/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-gray-500 hover:text-gray-300 underline"
            >
              Set up DDPI (one-time, skips daily TPIN)
            </a>
            {cdsl.checked_at && (
              <span className="text-[11px] text-gray-600 ml-auto">
                Checked {new Date(cdsl.checked_at).toLocaleTimeString("en-IN", {
                  timeZone: "Asia/Kolkata",
                  hour: "2-digit", minute: "2-digit",
                })}
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
