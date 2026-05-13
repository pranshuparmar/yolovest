"""Tests for the Kite margins extractors — particularly that
_extract_available_cash returns post-deduction available balance,
not the opening cash for the day."""

from yolovest.dashboard.app import (
    _extract_available_cash,
    _extract_utilised_margin,
)


class TestExtractAvailableCash:
    """Regression: previous version returned equity.available.cash which
    is the OPENING balance, not the currently-available one. After
    placing MIS orders, cash stayed unchanged but live_balance / net
    dropped — and our dashboard kept reporting the stale cash value."""

    def test_prefers_net_over_cash(self):
        margins = {
            "equity": {
                "net": 88395.18,
                "available": {
                    "cash": 100000.0,
                    "live_balance": 88395.18,
                    "opening_balance": 100000.0,
                },
                "utilised": {"debits": 11604.82},
            }
        }
        assert _extract_available_cash(margins) == 88395.18

    def test_falls_back_to_live_balance_when_net_missing(self):
        margins = {
            "equity": {
                "available": {
                    "cash": 100000.0,
                    "live_balance": 88395.18,
                },
                "utilised": {"debits": 11604.82},
            }
        }
        assert _extract_available_cash(margins) == 88395.18

    def test_falls_back_to_cash_minus_debits(self):
        margins = {
            "equity": {
                "available": {"cash": 100000.0},
                "utilised": {"debits": 11604.82},
            }
        }
        assert _extract_available_cash(margins) == 100000.0 - 11604.82

    def test_returns_cash_when_no_utilisation(self):
        margins = {
            "equity": {
                "available": {"cash": 100000.0},
                "utilised": {"debits": 0.0},
            }
        }
        assert _extract_available_cash(margins) == 100000.0

    def test_returns_zero_for_empty(self):
        assert _extract_available_cash({}) == 0.0
        assert _extract_available_cash({"equity": {}}) == 0.0

    def test_handles_string_values_gracefully(self):
        margins = {"equity": {"net": "not-a-number",
                              "available": {"live_balance": 5000.0}}}
        # Falls through net (bad parse) → live_balance
        assert _extract_available_cash(margins) == 5000.0


class TestExtractUtilisedMargin:
    def test_reads_debits(self):
        margins = {"equity": {"utilised": {"debits": 11604.82}}}
        assert _extract_utilised_margin(margins) == 11604.82

    def test_falls_back_to_net(self):
        margins = {"equity": {"utilised": {"net": 11604.82}}}
        assert _extract_utilised_margin(margins) == 11604.82

    def test_returns_zero_for_empty(self):
        assert _extract_utilised_margin({}) == 0.0
