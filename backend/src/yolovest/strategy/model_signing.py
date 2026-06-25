"""HMAC signing for ML model artifacts that cross the dashboard
upload/download boundary.

The model-upload endpoint ``joblib.load()``s the file it's given, and pickle
executes arbitrary code during deserialization — so anyone who reaches the
dashboard could upload a crafted ``.pkl`` and get remote code execution as the
backend process (which holds the Kite session, API secret, and DB). The
internal ``.sha256`` sidecar is integrity-only: an attacker who can replace the
artifact can recompute the hash, so it proves the file isn't *corrupt*, not
that it's *authentic*.

When ``MODEL_SIGNING_KEY`` is configured, the download endpoint wraps an
artifact in a keyed-HMAC envelope and the upload endpoint refuses to load
anything without a valid signature — so only artifacts produced by a machine
that holds the same key (the operator's own training box) will load. This is
the intended train-on-a-big-box → import-on-the-trading-box workflow, with the
upload boundary closed against forged pickles.

Envelope format: ``b"YVSIG1 " + hex(hmac_sha256(key, payload)) + b"\\n" + payload``.
The signature precedes the payload so it can be verified *before* the pickle
bytes are ever written to disk or handed to joblib.
"""

import hashlib
import hmac
import os

_MAGIC = b"YVSIG1 "
_SIG_HEX_LEN = 64  # sha256 hexdigest length


def signing_key() -> bytes | None:
    """The configured model-signing key, or None when signing is disabled."""
    raw = os.environ.get("MODEL_SIGNING_KEY", "").strip()
    return raw.encode("utf-8") if raw else None


def _hmac_hex(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def wrap(key: bytes, payload: bytes) -> bytes:
    """Return a signed envelope around ``payload``."""
    return _MAGIC + _hmac_hex(key, payload).encode("ascii") + b"\n" + payload


def is_wrapped(blob: bytes) -> bool:
    return blob.startswith(_MAGIC)


def unwrap(key: bytes, blob: bytes) -> bytes:
    """Verify a signed envelope and return its payload.

    Raises ``ValueError`` on a missing, malformed, or mismatched signature —
    the caller must treat any ValueError as "refuse to load this artifact".
    """
    if not blob.startswith(_MAGIC):
        raise ValueError("artifact is not signed (missing envelope)")
    rest = blob[len(_MAGIC):]
    newline = rest.find(b"\n")
    if newline != _SIG_HEX_LEN:
        raise ValueError("malformed signature header")
    # Compare as bytes: decoding the signature to str and comparing strings
    # makes hmac.compare_digest raise TypeError on a forged non-ASCII
    # signature (it refuses non-ASCII str), which would escape the caller's
    # ValueError handling. Bytes-vs-bytes always returns a bool.
    sig = rest[:newline]
    payload = rest[newline + 1:]
    expected = _hmac_hex(key, payload).encode("ascii")
    if not hmac.compare_digest(sig, expected):
        raise ValueError("signature mismatch")
    return payload
