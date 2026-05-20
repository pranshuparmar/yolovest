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

  // Render only when the alert is genuinely actionable today —
  // unauthorised holdings AND something that might try to sell
  // (open CNC trade / active GTT / pending CNC sell). Long-term
  // holders with no system exits never see this.
  if (!cdsl || !cdsl.authenticated || !cdsl.alert_needed) return null;

  const pendingSyms = (cdsl.pending_symbols ?? []).slice(0, 6).map((s) => s.symbol).join(", ");
  const more = (cdsl.pending_count ?? 0) - 6;
  const symBlurb = more > 0 ? `${pendingSyms}, +${more} more` : pendingSyms;

  // Build a small "why now" line so the alert isn't mysterious.
  const triggerBits: string[] = [];
  if (cdsl.active_cnc_positions) triggerBits.push(`${cdsl.active_cnc_positions} open CNC trade${cdsl.active_cnc_positions === 1 ? "" : "s"}`);
  if (cdsl.active_gtts) triggerBits.push(`${cdsl.active_gtts} active GTT${cdsl.active_gtts === 1 ? "" : "s"}`);
  if (cdsl.pending_cnc_sells) triggerBits.push(`${cdsl.pending_cnc_sells} pending CNC sell${cdsl.pending_cnc_sells === 1 ? "" : "s"}`);
  const trigger = triggerBits.join(" + ");

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
          {trigger && (
            <p className="text-[11px] text-gray-500 mt-1">
              Showing because of: <span className="text-gray-400">{trigger}</span>
            </p>
          )}
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
