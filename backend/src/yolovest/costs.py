"""Transaction cost computation for Indian equity trades (Zerodha/NSE).

Covers: brokerage, STT (Securities Transaction Tax), stamp duty, GST,
and exchange transaction charges.
"""


def compute_transaction_costs(
    entry_price: float, exit_price: float, quantity: int,
) -> float:
    """Compute Zerodha transaction costs for a round-trip trade.

    Includes:
    - Brokerage: ₹20 or 0.03% per leg (whichever is lower)
    - STT: 0.025% on sell-side value (intraday)
    - Other: ~0.01% combined (stamp duty, GST, exchange fees)
    """
    entry_value = entry_price * quantity
    exit_value = exit_price * quantity
    entry_brokerage = min(20, entry_value * 0.0003)
    exit_brokerage = min(20, exit_value * 0.0003)
    stt = exit_value * 0.00025  # STT on sell side
    other = (entry_value + exit_value) * 0.0001  # stamp, GST, exchange
    return round(entry_brokerage + exit_brokerage + stt + other, 2)
