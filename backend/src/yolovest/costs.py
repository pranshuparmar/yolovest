"""Transaction cost computation for Indian equity trades.

Covers: brokerage, STT (Securities Transaction Tax), stamp duty, GST,
and exchange transaction charges. Prefers actual charges from the
broker's contract-note API when available; falls back to a
config-driven estimate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yolovest.config import TransactionCostConfig

logger = logging.getLogger(__name__)


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


def round_trip_cost_floor_pct(
    product: str = "MIS",
    cost_config: TransactionCostConfig | None = None,
    slippage_pct: float = 0.0005,
) -> float:
    """Round-trip transaction cost + slippage as a fraction of notional.

    Used as a *label* floor: a triple-barrier "win" whose target move is
    smaller than this is a net loss after costs, so labelling it a win
    teaches the model an unprofitable target (worst on the tight 0.6×ATR
    intraday geometry). The percentage components mirror
    compute_transaction_costs — the same cost model the walk-forward
    backtest uses — so labels and backtest agree on what "profitable"
    means. The per-leg brokerage CAP is intentionally ignored: at the cap
    the percentage drag only shrinks, so this stays a conservative floor.
    `slippage_pct` is per side and mirrors BacktestConfig.entry_slippage_pct.
    """
    brokerage_pct = 0.0003
    stt_pct = 0.00025 if product == "MIS" else 0.001
    other_pct = 0.0001
    if cost_config is not None:
        brokerage_pct = cost_config.brokerage_per_leg_pct
        stt_pct = (
            cost_config.stt_intraday_pct if product == "MIS"
            else cost_config.stt_delivery_pct
        )
        other_pct = cost_config.other_charges_pct
    # brokerage both legs + STT (sell only) + other both legs + slippage both sides
    return 2 * brokerage_pct + stt_pct + 2 * other_pct + 2 * slippage_pct


def evaluate_net_rr(
    *,
    signal_type: str,
    entry_price: float,
    target_price: float,
    stop_loss_price: float,
    quantity: int,
    product: str = "MIS",
    cost_config: TransactionCostConfig | None = None,
) -> tuple[float | None, float, str | None]:
    """Compute the cost-adjusted reward:risk ratio of a trade setup.

    Returns (net_rr, round_trip_costs, reason). When the setup is
    unviable (costs would exceed gross win, or denominator non-positive),
    `net_rr` is None and `reason` carries a human-readable explanation.
    Otherwise `reason` is None and the caller compares `net_rr` against
    its threshold to decide.

    Shared by risk-check (signal-time gate) and the pending-trade
    repricer (per-heartbeat revalidation), so the same arithmetic
    rejects the same setups in both places.

    For SELL trades the entry leg is the sell side, so the costs call
    is flipped — STT lands on the correct leg in both directions.
    """
    try:
        entry = float(entry_price)
        target = float(target_price)
        sl = float(stop_loss_price)
        qty = int(quantity)
    except (TypeError, ValueError):
        return None, 0.0, "non-numeric levels"
    if entry <= 0 or target <= 0 or sl <= 0 or qty <= 0:
        return None, 0.0, "missing levels"
    direction = 1 if signal_type == "BUY" else -1
    gross_win = (target - entry) * direction * qty
    gross_loss = (entry - sl) * direction * qty
    if direction > 0:
        costs = compute_transaction_costs(
            entry_price=entry, exit_price=target,
            quantity=qty, product=product, cost_config=cost_config,
        )
    else:
        costs = compute_transaction_costs(
            entry_price=target, exit_price=entry,
            quantity=qty, product=product, cost_config=cost_config,
        )
    net_win = gross_win - costs
    net_loss = gross_loss + costs
    if net_win <= 0:
        return None, costs, (
            f"Costs ₹{costs:.0f} exceed gross win ₹{gross_win:.0f} "
            f"({qty} qty, target ₹{target:.2f})"
        )
    if net_loss <= 0:
        return None, costs, "non-positive net loss (SL on wrong side?)"
    return net_win / net_loss, costs, None


def compute_transaction_cost_breakdown(
    entry_price: float,
    exit_price: float,
    quantity: int,
    product: str = "MIS",
    cost_config: TransactionCostConfig | None = None,
) -> dict[str, Any]:
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


async def resolve_round_trip_costs(
    broker: Any,
    *,
    symbol: str,
    signal_type: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    product: str,
    exchange: str = "NSE",
    cost_config: TransactionCostConfig | None = None,
) -> tuple[float, str, dict[str, Any]]:
    """Return (total_costs, source, breakdown) for a round-trip trade.

    `source` is "broker" (from contract-note API) or "estimate" (config-based).
    `breakdown` carries `brokerage`, `stt`, `other_charges`, `total`, plus
    `source` — same shape regardless of which path produced it, so it can be
    stored verbatim alongside the trade.
    """
    entry_side = signal_type.upper()
    exit_side = "SELL" if entry_side == "BUY" else "BUY"
    legs: list[dict[str, Any]] = [
        {
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": entry_side,
            "variety": "regular",
            "product": product,
            "order_type": "MARKET",
            "quantity": int(quantity),
            "average_price": float(entry_price),
        },
        {
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": exit_side,
            "variety": "regular",
            "product": product,
            "order_type": "MARKET",
            "quantity": int(quantity),
            "average_price": float(exit_price),
        },
    ]

    if broker is not None:
        try:
            actual = await broker.compute_charges(legs)
        except Exception as e:
            logger.debug("broker.compute_charges raised: %s", e)
            actual = None
        if actual and len(actual) == 2:
            brokerage = round(actual[0]["brokerage"] + actual[1]["brokerage"], 2)
            stt = round(actual[0]["stt"] + actual[1]["stt"], 2)
            other = round(actual[0]["other_charges"] + actual[1]["other_charges"], 2)
            total = round(actual[0]["total"] + actual[1]["total"], 2)
            breakdown = {
                "brokerage": brokerage,
                "stt": stt,
                "other_charges": other,
                "total": total,
                "source": "broker",
            }
            return total, "broker", breakdown

    fallback = compute_transaction_cost_breakdown(
        entry_price, exit_price, quantity, product=product,
        cost_config=cost_config,
    )
    fallback["source"] = "estimate"
    return fallback["total"], "estimate", fallback
