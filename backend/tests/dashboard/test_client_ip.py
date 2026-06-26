"""_client_ip must be resistant to X-Forwarded-For spoofing.

The brute-force throttle keys on the client IP. The two nginx hops APPEND to
XFF, so an attacker-supplied value lands LEFTMOST — the previous "leftmost
wins" logic let an attacker rotate the throttle key per request and sidestep
the per-IP lockout. The real client is the entry _TRUSTED_PROXY_HOPS positions
from the right.
"""

from types import SimpleNamespace

from yolovest.dashboard import security


def _req(headers: dict[str, str], client_host: str | None = "10.0.0.9"):
    client = SimpleNamespace(host=client_host) if client_host else None
    return SimpleNamespace(headers=headers, client=client)


def test_ignores_spoofed_leftmost_entry(monkeypatch):
    monkeypatch.setattr(security, "_TRUSTED_PROXY_HOPS", 2)
    # attacker forges the leftmost; the two trusted proxies appended the
    # real client + their own hop on the right.
    r = _req({"x-forwarded-for": "1.1.1.1, 198.51.100.7, 10.0.0.2"})
    assert security._client_ip(r) == "198.51.100.7"


def test_rotating_spoof_yields_a_stable_key(monkeypatch):
    monkeypatch.setattr(security, "_TRUSTED_PROXY_HOPS", 2)
    r1 = _req({"x-forwarded-for": "aaa.aaa, 198.51.100.7, 10.0.0.2"})
    r2 = _req({"x-forwarded-for": "bbb.bbb, 198.51.100.7, 10.0.0.2"})
    # The attacker can't move the throttle bucket by changing the leftmost.
    assert security._client_ip(r1) == security._client_ip(r2) == "198.51.100.7"


def test_short_chain_falls_back_to_peer_not_forged_value(monkeypatch):
    monkeypatch.setattr(security, "_TRUSTED_PROXY_HOPS", 2)
    # Only one XFF entry (< hops): the request didn't traverse the full
    # trusted chain, so the lone (forgeable) value must NOT be trusted.
    r = _req({"x-forwarded-for": "1.2.3.4"}, client_host="10.0.0.5")
    assert security._client_ip(r) == "10.0.0.5"


def test_no_xff_uses_socket_peer(monkeypatch):
    monkeypatch.setattr(security, "_TRUSTED_PROXY_HOPS", 2)
    r = _req({}, client_host="10.0.0.5")
    assert security._client_ip(r) == "10.0.0.5"


def test_hops_zero_disables_xff_trust(monkeypatch):
    monkeypatch.setattr(security, "_TRUSTED_PROXY_HOPS", 0)
    r = _req({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, client_host="10.0.0.5")
    assert security._client_ip(r) == "10.0.0.5"


def test_no_client_falls_back_to_x_real_ip(monkeypatch):
    monkeypatch.setattr(security, "_TRUSTED_PROXY_HOPS", 2)
    r = _req({"x-real-ip": "203.0.113.4"}, client_host=None)
    assert security._client_ip(r) == "203.0.113.4"
