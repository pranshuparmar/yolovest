"""Transaction cost computation for Indian equity trades.

Covers: brokerage, STT (Securities Transaction Tax), stamp duty, GST,
and exchange transaction charges. Rates are configurable via
config.transaction_costs for different brokers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yolovest.config import TransactionCostConfig


def compute_transaction_costs(
    entry_price: float,
    exit_price: float,
    quantity: int,
    product: str = "MIS",
    cost_config: TransactionCostConfig | None = None,
) -> float:
    """Compute round-trip transaction costs for an equity trade.

    Args:
        entry_price: Buy/entry price per share.
        exit_price: Sell/exit price per share.
        quantity: Number of shares.
        product: "MIS" (intraday) or "CNC" (delivery). STT rates differ.
        cost_config: Optional config override. Uses Zerodha defaults if None.
    """
    # Defaults match Zerodha's current rates
    brokerage_pct = 0.0003
    brokerage_cap = 20.0
    stt_pct = 0.00025 if product == "MIS" else 0.001
    other_pct = 0.0001

    if cost_config is not None:
        brokerage_pct = cost_config.brokerage_per_leg_pct
        brokerage_cap = cost_config.brokerage_cap_per_leg
        stt_pct = (
            cost_config.stt_intraday_pct if product == "MIS"
            else cost_config.stt_delivery_pct
        )
        other_pct = cost_config.other_charges_pct

    entry_value = entry_price * quantity
    exit_value = exit_price * quantity

    entry_brokerage = min(brokerage_cap, entry_value * brokerage_pct)
    exit_brokerage = min(brokerage_cap, exit_value * brokerage_pct)
    stt = exit_value * stt_pct  # STT on sell side only
    other = (entry_value + exit_value) * other_pct

    return round(entry_brokerage + exit_brokerage + stt + other, 2)


def compute_transaction_cost_breakdown(
    entry_price: float,
    exit_price: float,
    quantity: int,
    product: str = "MIS",
    cost_config: TransactionCostConfig | None = None,
) -> dict[str, float]:
    """Compute itemized transaction cost breakdown for an equity trade.

    Returns dict with brokerage, stt, other_charges, and total.
    """
    brokerage_pct = 0.0003
    brokerage_cap = 20.0
    stt_pct = 0.00025 if product == "MIS" else 0.001
    other_pct = 0.0001

    if cost_config is not None:
        brokerage_pct = cost_config.brokerage_per_leg_pct
        brokerage_cap = cost_config.brokerage_cap_per_leg
        stt_pct = (
            cost_config.stt_intraday_pct if product == "MIS"
            else cost_config.stt_delivery_pct
        )
        other_pct = cost_config.other_charges_pct

    entry_value = entry_price * quantity
    exit_value = exit_price * quantity

    entry_brokerage = min(brokerage_cap, entry_value * brokerage_pct)
    exit_brokerage = min(brokerage_cap, exit_value * brokerage_pct)
    brokerage = round(entry_brokerage + exit_brokerage, 2)
    stt = round(exit_value * stt_pct, 2)
    other = round((entry_value + exit_value) * other_pct, 2)

    return {
        "brokerage": brokerage,
        "stt": stt,
        "other_charges": other,
        "total": round(brokerage + stt + other, 2),
    }
