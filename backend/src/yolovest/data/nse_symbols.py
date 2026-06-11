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
    universe: str = "nifty500",
) -> list[str]:
    """Return the bundled (static) symbol list for the requested universe.

    This is the safe fallback when a live fetch isn't available or fails.
    For up-to-date constituents, prefer fetch_live_constituents() instead.

    Args:
        universe: One of "nifty50", "nifty100", "nifty200", "nifty500", or
            "all". "all" is a legacy alias for "nifty500" (kept for
            backwards-compat with existing DB config). nifty100/nifty200
            don't have bundled lists — they fall through to NIFTY_500_SUBSET
            (a superset) since the live fetch is the primary path for them.

    Returns:
        List of NSE symbol strings.
    """
    if universe == "nifty50":
        return list(NIFTY_50)
    # nifty100, nifty200, nifty500, and "all" all use the broader bundled list
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


def parse_constituent_csv(body: str) -> list[dict[str, str]]:
    """Parse a niftyindices.com constituent CSV into a list of records.

    Returns a list of `{"symbol": ..., "industry": ...}` dicts (industry
    may be an empty string when the CSV row has none). Expected columns:
    'Company Name', 'Industry', 'Symbol', 'Series', 'ISIN Code'. Filters
    to Series == 'EQ' (equity, excludes Z/BE/etc.) when the Series
    column is present.

    Drops NSE-issued placeholder tickers — primarily ``DUMMY*`` symbols,
    which NSE introduces during corporate actions (demergers, splits)
    as temporary entries. They don't resolve to a real instrument_token
    on Kite and aren't tradable.
    """
    reader = csv.DictReader(io.StringIO(body))
    records: list[dict[str, str]] = []
    for row in reader:
        # niftyindices CSVs sometimes have stray whitespace or BOM in headers.
        # Build a case-insensitive lookup that strips whitespace.
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        sym = norm.get("symbol")
        series = norm.get("series")
        industry = norm.get("industry", "")
        if not sym:
            continue
        # If series is exposed, restrict to EQ; otherwise accept everything.
        if series and series.upper() != "EQ":
            continue
        # Drop NSE placeholder tickers — these aren't tradable.
        if _is_placeholder_symbol(sym):
            continue
        records.append({"symbol": sym.upper(), "industry": industry})
    # Dedup while preserving order
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for r in records:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            deduped.append(r)
    return deduped


def parse_constituent_csv_symbols(body: str) -> list[str]:
    """Convenience wrapper for callers that only need the symbol list."""
    return [r["symbol"] for r in parse_constituent_csv(body)]


def _is_placeholder_symbol(symbol: str) -> bool:
    """NSE creates DUMMY* tickers as temporary entries during corporate
    actions. They appear in index constituent lists but don't resolve to
    a real Kite instrument_token. Filter them out at the source.
    """
    upper = symbol.strip().upper()
    return upper.startswith("DUMMY")


async def fetch_live_constituent_details(
    universe: Literal["nifty50", "nifty100", "nifty200", "nifty500"],
    timeout_sec: float = 15.0,
) -> list[dict[str, str]] | None:
    """Fetch live index constituents (with industry) from niftyindices.com.

    Returns a list of `{"symbol", "industry"}` dicts on success, or None
    on any failure (HTTP error, parse failure, empty result). Caller
    should fall back to the bundled symbol list in that case.
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

        records = parse_constituent_csv(body)
        if not records:
            logger.warning(
                "Live constituent fetch for %s parsed 0 symbols", universe,
            )
            return None
        logger.info(
            "Live constituent fetch: %s -> %d symbols", universe, len(records),
        )
        return records
    except Exception as e:
        logger.warning("Live constituent fetch failed for %s: %s", universe, e)
        return None


async def fetch_live_constituents(
    universe: Literal["nifty50", "nifty100", "nifty200", "nifty500"],
    timeout_sec: float = 15.0,
) -> list[str] | None:
    """Backwards-compatible wrapper returning just the symbol list."""
    records = await fetch_live_constituent_details(universe, timeout_sec)
    return [r["symbol"] for r in records] if records is not None else None
