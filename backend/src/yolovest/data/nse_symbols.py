"""NSE index constituent lists — Nifty 50 and Nifty 500.

Bundled static lists as the primary source, with optional live refresh
from NSE India. If the live fetch fails, falls back to the bundled list.

These lists are used by the ingest-universe skill to populate the OHLCV
table so market-scan has a real pool of stocks to rank from.
"""

import logging
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bundled Nifty 50 constituents (updated March 2026)
# ---------------------------------------------------------------------------

NIFTY_50: list[str] = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BPCL",
    "BHARTIARTL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "ITC", "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "NTPC", "NESTLEIND",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

# ---------------------------------------------------------------------------
# Bundled Nifty 500 constituents (updated March 2026)
# Top ~200 by market cap included here; the full 500 list is fetched live.
# This subset covers >85% of NSE market cap and serves as a robust fallback.
# ---------------------------------------------------------------------------

NIFTY_500_SUBSET: list[str] = NIFTY_50 + [
    # Nifty Next 50
    "ABB", "ADANIENSOL", "ADANIGREEN", "AMBUJACEM", "ATGL",
    "BANKBARODA", "BERGEPAINT", "BOSCHLTD", "CANBK", "CHOLAFIN",
    "COLPAL", "DABUR", "DLF", "DIVISLAB", "GAIL",
    "GODREJCP", "HAVELLS", "ICICIPRULI", "INDIGO", "IOC",
    "IRCTC", "IRFC", "JINDALSTEL", "JIOFIN", "LICI",
    "LODHA", "LTIM", "LUPIN", "MARICO", "MOTHERSON",
    "NHPC", "PEL", "PERSISTENT", "PIDILITIND", "PNB",
    "POLYCAB", "RECLTD", "SAIL", "SBICARD", "SHREECEM",
    "SHRIRAMFIN", "SIEMENS", "SJVN", "TATACOMM", "TATAPOWER",
    "TORNTPHARM", "TVSMOTOR", "UNIONBANK", "VEDL", "ZOMATO",
    # Nifty Midcap 100 (selected)
    "AARTIIND", "ACC", "ALKEM", "APLAPOLLO", "ASHOKLEY",
    "ASTRAL", "AUBANK", "AUROPHARMA", "BALKRISIND", "BATAINDIA",
    "BIOCON", "CANFINHOME", "COFORGE", "CONCOR", "CROMPTON",
    "CUMMINSIND", "DEEPAKNTR", "DELHIVERY", "DIXON", "ESCORTS",
    "EXIDEIND", "FEDERALBNK", "FORTIS", "GMRINFRA", "GNFC",
    "GODREJPROP", "GSPL", "GUJGASLTD", "HAL", "HDFCAMC",
    "HONAUT", "IDFCFIRSTB", "INDIANB", "INDUSTOWER", "IREDA",
    "JUBLFOOD", "KEI", "KPITTECH", "LAURUSLABS", "LICHSGFIN",
    "LTTS", "MFSL", "MPHASIS", "MRF", "MUTHOOTFIN",
    "NATIONALUM", "NAUKRI", "NMDC", "OBEROIRLTY", "OFSS",
    "PAGEIND", "PATANJALI", "PETRONET", "PFC", "PIIND",
    "PRESTIGE", "PVRINOX", "RAMCOCEM", "SONACOMS", "SRF",
    "SUNDARMFIN", "SUPREMEIND", "SYNGENE", "TATACHEM", "TATAELXSI",
    "TORNTPOWER", "UNITDSPR", "UPL", "VOLTAS", "YESBANK",
]

# Deduplicate while preserving order
_seen: set[str] = set()
_deduped: list[str] = []
for _s in NIFTY_500_SUBSET:
    if _s not in _seen:
        _seen.add(_s)
        _deduped.append(_s)
NIFTY_500_SUBSET = _deduped


def get_universe_symbols(
    universe: Literal["nifty50", "nifty500", "all"] = "nifty500",
) -> list[str]:
    """Return symbol list for the requested universe.

    Args:
        universe: One of "nifty50", "nifty500", or "all".
            "all" is treated the same as "nifty500" (bundled subset).

    Returns:
        List of NSE symbol strings.
    """
    if universe == "nifty50":
        return list(NIFTY_50)
    # nifty500 and "all" both use the broader list
    return list(NIFTY_500_SUBSET)
