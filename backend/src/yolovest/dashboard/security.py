"""Dashboard auth primitives: session tokens, brute-force throttle.

Split out of app.py so route modules and tests can import them
without the FastAPI app factory.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.security import HTTPBasic

logger = logging.getLogger(__name__)

security = HTTPBasic(auto_error=False)

# Token signing key — generated once per process lifetime.
# Tokens become invalid on restart (forces re-login, which is fine).
_TOKEN_SECRET = secrets.token_bytes(32)
_TOKEN_TTL_SEC = 24 * 60 * 60  # 24 hours


def _sign_token(username: str, ttl: int = _TOKEN_TTL_SEC) -> str:
    """Create a signed session token: base64(payload).signature.

    `ttl` defaults to the normal session lifetime; pass a short value to
    mint a single-use-ish download token that can ride in a URL query
    param (native browser downloads can't send the Authorization header).
    """

    payload = json.dumps({
        "user": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl,
        "jti": secrets.token_hex(8),
    }).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(_TOKEN_SECRET, payload, hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_token(token: str) -> str:
    """Verify a signed session token. Returns username or raises."""

    parts = token.split(".", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=401, detail="Invalid token format")

    payload_b64, sig = parts
    try:
        payload = base64.urlsafe_b64decode(payload_b64)
    except Exception:
        # Original parse error is noise to the client; a clean 401 suffices.
        raise HTTPException(status_code=401, detail="Invalid token encoding") from None

    expected_sig = hmac.new(_TOKEN_SECRET, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Invalid token payload") from None

    if data.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="Token expired")

    return data.get("user", "anonymous")


class _LoginThrottle:
    """In-memory brute-force throttle for password attempts.

    Covers both /api/auth/login and the Basic-auth fallback (without the
    latter, an attacker could brute-force the password against any
    protected endpoint and never touch the login route). Per-source
    exponential lockout after PER_IP_THRESHOLD consecutive failures,
    plus a global damper so X-Forwarded-For spoofing can't sidestep the
    per-IP key. State is per-process; a restart clears it — fine, the
    lockout only needs to make online guessing impractical.
    """

    BASE_LOCK_SEC = 30.0
    MAX_LOCK_SEC = 900.0
    PER_IP_THRESHOLD = 5
    GLOBAL_THRESHOLD = 20
    MAX_TRACKED_IPS = 1000

    def __init__(self) -> None:
        self._by_ip: dict[str, tuple[int, float]] = {}
        self._global_failures = 0
        self._global_locked_until = 0.0

    def locked_for(self, ip: str) -> float:
        """Seconds the source must still wait, 0.0 when free to try."""
        now = time.monotonic()
        remaining = max(0.0, self._global_locked_until - now)
        _, until = self._by_ip.get(ip, (0, 0.0))
        return max(remaining, until - now)

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        fails, _ = self._by_ip.get(ip, (0, 0.0))
        fails += 1
        lock = 0.0
        if fails >= self.PER_IP_THRESHOLD:
            lock = min(
                self.BASE_LOCK_SEC * 2 ** (fails - self.PER_IP_THRESHOLD),
                self.MAX_LOCK_SEC,
            )
        if len(self._by_ip) >= self.MAX_TRACKED_IPS and ip not in self._by_ip:
            # Bound memory under spoofed-source floods: drop the entry
            # closest to expiry. The global damper still applies.
            oldest = min(self._by_ip, key=lambda k: self._by_ip[k][1])
            self._by_ip.pop(oldest, None)
        self._by_ip[ip] = (fails, now + lock)
        self._global_failures += 1
        if self._global_failures >= self.GLOBAL_THRESHOLD:
            self._global_locked_until = now + self.BASE_LOCK_SEC

    def record_success(self, ip: str) -> None:
        self._by_ip.pop(ip, None)
        self._global_failures = 0
        self._global_locked_until = 0.0


def _client_ip(request: Request) -> str:
    """Best-effort client identity behind the nginx-proxy → frontend-nginx
    chain. Leftmost X-Forwarded-For entry is the real client (both
    proxies append); X-Real-IP and the socket peer are fallbacks."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip", "")
    if xri:
        return xri
    return request.client.host if request.client else "unknown"

