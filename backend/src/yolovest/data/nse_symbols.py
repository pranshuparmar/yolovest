"""NSE index constituent lists — Nifty 50 and Nifty 500.

Bundled static lists as the primary source, with optional live refresh
from NSE India. If the live fetch fails, falls back to the bundled list.

These lists are used by the ingest-universe skill to populate the OHLCV
table so market-scan has a real pool of stocks to rank from.
"""

import csv
import io
import logging
from typing import Literal

import aiohttp

from yolovest.http_utils import scraper_headers

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
    """Return the bundled (static) symbol list for the requested universe.

    This is the safe fallback when a live fetch isn't available or fails.
    For up-to-date constituents, prefer fetch_live_constituents() instead.

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


# ---------------------------------------------------------------------------
# Live constituent fetch — niftyindices.com publishes CSVs of every index.
# These URLs are publicly accessible and don't require Kite or NSE auth.
# ---------------------------------------------------------------------------

_NIFTY_CSV_URLS: dict[str, str] = {
    "nifty50":  "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "nifty100": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "nifty200": "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "nifty500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
}

# UI-friendly aliases that map to a concrete index. "all" is the catch-all
# label in the Settings dropdown — treat it as the broadest live source we
# have (Nifty 500) rather than the partial bundled list.
_UNIVERSE_ALIASES: dict[str, str] = {
    "all": "nifty500",
}


def parse_constituent_csv(body: str) -> list[str]:
    """Parse a niftyindices.com constituent CSV into a list of NSE symbols.

    Expected columns: 'Company Name', 'Industry', 'Symbol', 'Series',
    'ISIN Code'. Filters to Series == 'EQ' (equity, excludes Z/BE/etc.)
    when the Series column is present.
    """
    reader = csv.DictReader(io.StringIO(body))
    symbols: list[str] = []
    for row in reader:
        # niftyindices CSVs sometimes have stray whitespace or BOM in headers.
        # Build a case-insensitive lookup that strips whitespace.
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        sym = norm.get("symbol")
        series = norm.get("series")
        if not sym:
            continue
        # If series is exposed, restrict to EQ; otherwise accept everything.
        if series and series.upper() != "EQ":
            continue
        symbols.append(sym.upper())
    # Dedup while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


async def fetch_live_constituents(
    universe: Literal["nifty50", "nifty100", "nifty200", "nifty500"],
    timeout_sec: float = 15.0,
) -> list[str] | None:
    """Fetch live index constituents from niftyindices.com.

    Returns the symbol list on success, or None on any failure (HTTP error,
    parse failure, empty result). Caller should fall back to the bundled
    list in that case.
    """
    resolved = _UNIVERSE_ALIASES.get(universe, universe)
    if resolved != universe:
        logger.info("Universe alias '%s' resolved to '%s' for live fetch", universe, resolved)
    url = _NIFTY_CSV_URLS.get(resolved)
    if not url:
        logger.warning("No live URL configured for universe '%s'", universe)
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        headers = scraper_headers({"Accept": "text/csv,application/csv,*/*"})
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Live constituent fetch for %s returned HTTP %d",
                        universe, resp.status,
                    )
                    return None
                body = await resp.text()

        symbols = parse_constituent_csv(body)
        if not symbols:
            logger.warning(
                "Live constituent fetch for %s parsed 0 symbols", universe,
            )
            return None
        logger.info(
            "Live constituent fetch: %s -> %d symbols", universe, len(symbols),
        )
        return symbols
    except Exception as e:
        logger.warning("Live constituent fetch failed for %s: %s", universe, e)
        return None
