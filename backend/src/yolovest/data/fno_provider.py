"""F&O option-chain aggregator (Kite-only).

Pulls the NFO instrument master once, groups by underlying name, picks
the nearest expiry for each, and batch-quotes all CE/PE strikes + the
front-month futures contract to compute daily aggregates:
  pcr_oi, pcr_volume, futures_oi, futures_volume, futures_close

Returns a flat dict keyed by underlying tradingsymbol. Caller (ingest-fno)
persists the result via `db.upsert_fno_daily`.

Kite quote limit is ~500 instruments per call; we batch in 200s to stay
well under and to keep individual request latency reasonable. A min
0.4s interval between quote calls — same throttle profile as
KiteDataProvider's historical_data path — keeps us within rate limits
even when fetching the full F&O universe (~200 names × ~50 strikes).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

_QUOTE_BATCH_SIZE = 200
_MIN_QUOTE_INTERVAL_SEC = 0.4

# Index underlyings carried in the NFO master alongside single-stock names.
# The intraday equity model trades stocks, not index derivatives, and these
# have no NSE-equity 5-min series to train on, so they're excluded from the
# F&O *underlying* list by default.
_FNO_INDEX_NAMES = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "SENSEX50",
})


async def fetch_fno_underlyings(
    kite: Any, *, include_indices: bool = False,
) -> list[str]:
    """Return the sorted list of F&O equity underlyings (~190 names).

    A single, cheap trip through the NFO instrument master — just the
    distinct underlying ``name`` values, no quote calls. Index underlyings
    (NIFTY/BANKNIFTY/...) are dropped unless ``include_indices`` is set.
    Empty list on any failure (caller decides how to fall back).
    """
    try:
        instruments = await asyncio.to_thread(kite.instruments, "NFO")
    except Exception:
        logger.exception("kite.instruments(NFO) failed")
        return []
    names: set[str] = set()
    for inst in instruments:
        name = inst.get("name")
        if not name:
            continue
        if not include_indices and name in _FNO_INDEX_NAMES:
            continue
        names.add(name)
    return sorted(names)


def _nearest_expiry(expiries: list[date]) -> date | None:
    today = date.today()
    future = sorted(e for e in expiries if e and e >= today)
    return future[0] if future else None


async def fetch_fno_aggregates(kite: Any) -> dict[str, dict[str, float]]:
    """Return per-underlying daily F&O aggregates.

    Single trip through the NFO instrument master + chunked quote calls.
    Empty dict on any catastrophic failure (caller decides whether to
    retry or skip the cycle). Per-underlying failures are logged at
    DEBUG and that underlying is silently dropped from the result.
    """
    try:
        instruments = await asyncio.to_thread(kite.instruments, "NFO")
    except Exception:
        logger.exception("kite.instruments(NFO) failed")
        return {}

    # Group by underlying name. Track per-name expiry set so we can
    # pick the nearest expiry per name (different underlyings can have
    # different expiry calendars — weeklies vs monthlies).
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for inst in instruments:
        name = inst.get("name")
        if not name:
            continue
        by_name[name].append(inst)

    # For each underlying, pick the nearest expiry's CE/PE set + the
    # front-month futures (FUT segment, same nearest expiry).
    # Returns (name, [(tradingsymbol, instrument_type), ...]) tuples.
    chain_specs: list[tuple[str, list[tuple[str, str]]]] = []
    for name, contracts in by_name.items():
        expiries = [e for e in {c.get("expiry") for c in contracts} if e is not None]
        if not expiries:
            continue
        nearest = _nearest_expiry(expiries)
        if not nearest:
            continue
        symbols_for_name: list[tuple[str, str]] = []
        for c in contracts:
            if c.get("expiry") != nearest:
                continue
            it = c.get("instrument_type")
            ts = c.get("tradingsymbol")
            if it in ("CE", "PE", "FUT") and ts:
                symbols_for_name.append((ts, it))
        if symbols_for_name:
            chain_specs.append((name, symbols_for_name))

    if not chain_specs:
        logger.warning("ingest-fno: no F&O underlyings resolved from instrument master")
        return {}

    # Build the full quote-symbol list ("NFO:TRADINGSYM") and a reverse
    # index back to (underlying_name, instrument_type) so we can route
    # each quote's oi/volume into the right bucket without a second pass.
    all_quote_keys: list[str] = []
    route: dict[str, tuple[str, str]] = {}
    for name, syms in chain_specs:
        for ts, it in syms:
            key = f"NFO:{ts}"
            all_quote_keys.append(key)
            route[key] = (name, it)

    logger.info(
        "ingest-fno: batch-quoting %d F&O instruments across %d underlyings",
        len(all_quote_keys), len(chain_specs),
    )

    # Per-name accumulators.
    ce_oi: dict[str, float] = defaultdict(float)
    ce_vol: dict[str, float] = defaultdict(float)
    pe_oi: dict[str, float] = defaultdict(float)
    pe_vol: dict[str, float] = defaultdict(float)
    fut_oi: dict[str, float] = defaultdict(float)
    fut_vol: dict[str, float] = defaultdict(float)
    fut_close: dict[str, float] = defaultdict(float)

    last_call = 0.0
    for chunk_start in range(0, len(all_quote_keys), _QUOTE_BATCH_SIZE):
        chunk = all_quote_keys[chunk_start:chunk_start + _QUOTE_BATCH_SIZE]
        elapsed = time.monotonic() - last_call
        if elapsed < _MIN_QUOTE_INTERVAL_SEC:
            await asyncio.sleep(_MIN_QUOTE_INTERVAL_SEC - elapsed)
        try:
            quotes = await asyncio.to_thread(kite.quote, chunk)
        except Exception:
            logger.exception(
                "ingest-fno: quote batch %d-%d failed",
                chunk_start, chunk_start + len(chunk),
            )
            last_call = time.monotonic()
            continue
        last_call = time.monotonic()

        for key, q in quotes.items():
            route_entry = route.get(key)
            if not route_entry:
                continue
            name, it = route_entry
            oi = float(q.get("oi") or 0.0)
            vol = float(q.get("volume") or 0.0)
            lp = float(q.get("last_price") or 0.0)
            if it == "CE":
                ce_oi[name] += oi
                ce_vol[name] += vol
            elif it == "PE":
                pe_oi[name] += oi
                pe_vol[name] += vol
            elif it == "FUT":
                fut_oi[name] += oi
                fut_vol[name] += vol
                # Only one futures contract per (name, nearest_expiry).
                fut_close[name] = lp

    # Compose final per-underlying dict.
    result: dict[str, dict[str, float]] = {}
    for name, _ in chain_specs:
        c_oi = ce_oi.get(name, 0.0)
        p_oi = pe_oi.get(name, 0.0)
        c_vol = ce_vol.get(name, 0.0)
        p_vol = pe_vol.get(name, 0.0)
        # Guard against zero call activity — PCR is undefined.
        pcr_oi = p_oi / c_oi if c_oi > 0 else 0.0
        pcr_vol = p_vol / c_vol if c_vol > 0 else 0.0
        result[name] = {
            "pcr_oi": pcr_oi,
            "pcr_volume": pcr_vol,
            "futures_oi": fut_oi.get(name, 0.0),
            "futures_volume": fut_vol.get(name, 0.0),
            "futures_close": fut_close.get(name, 0.0),
        }

    return result
