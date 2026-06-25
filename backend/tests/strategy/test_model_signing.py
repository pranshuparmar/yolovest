"""Unit tests for the model-artifact signing envelope (strategy/model_signing).

This is the primitive that closes the malicious-pickle RCE on the dashboard
model-upload boundary: only artifacts signed with MODEL_SIGNING_KEY load.
"""

import pytest

from yolovest.strategy.model_signing import is_wrapped, signing_key, unwrap, wrap

KEY = b"test-signing-key"


class TestEnvelope:
    def test_wrap_unwrap_roundtrip(self):
        payload = b"\x80\x04 pretend-pickle \x00\xff bytes"
        env = wrap(KEY, payload)
        assert is_wrapped(env)
        assert env != payload
        assert unwrap(KEY, env) == payload

    def test_unsigned_blob_rejected(self):
        with pytest.raises(ValueError, match="not signed"):
            unwrap(KEY, b"raw pickle, no envelope")

    def test_tampered_payload_rejected(self):
        env = bytearray(wrap(KEY, b"original payload"))
        env[-1] ^= 0xFF  # flip a payload byte
        with pytest.raises(ValueError, match="signature mismatch"):
            unwrap(KEY, bytes(env))

    def test_wrong_key_rejected(self):
        env = wrap(KEY, b"payload")
        with pytest.raises(ValueError, match="signature mismatch"):
            unwrap(b"a-different-key", env)

    def test_malformed_header_rejected(self):
        # Magic present but the signature isn't a full 64-hex-char line.
        with pytest.raises(ValueError, match="malformed"):
            unwrap(KEY, b"YVSIG1 deadbeef\npayload")


class TestSigningKey:
    def test_absent_env_disables_signing(self, monkeypatch):
        monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)
        assert signing_key() is None

    def test_blank_env_disables_signing(self, monkeypatch):
        monkeypatch.setenv("MODEL_SIGNING_KEY", "   ")
        assert signing_key() is None

    def test_set_env_enables_signing(self, monkeypatch):
        monkeypatch.setenv("MODEL_SIGNING_KEY", "abc123")
        assert signing_key() == b"abc123"
