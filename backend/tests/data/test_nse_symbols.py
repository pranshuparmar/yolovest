"""Tests for the live NSE constituent fetcher and parser."""

from yolovest.data.nse_symbols import (
    NIFTY_500_SUBSET,
    get_universe_symbols,
    parse_constituent_csv,
)


class TestParseConstituentCsv:
    """Parse niftyindices.com-style CSV bodies."""

    def test_extracts_symbol_column(self):
        csv_body = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
            "Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\n"
        )
        symbols = parse_constituent_csv(csv_body)
        assert symbols == ["RELIANCE", "TCS"]

    def test_filters_non_eq_series(self):
        csv_body = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "Reliance,Energy,RELIANCE,EQ,INE002A01018\n"
            "Test Corp,Misc,TESTSME,BE,XYZ\n"
            "Test Corp 2,Misc,TESTBZ,BZ,ABC\n"
        )
        symbols = parse_constituent_csv(csv_body)
        assert symbols == ["RELIANCE"]

    def test_accepts_all_when_series_column_missing(self):
        csv_body = (
            "Company Name,Industry,Symbol,ISIN Code\n"
            "Reliance,Energy,RELIANCE,INE002A01018\n"
            "TCS,IT,TCS,INE467B01029\n"
        )
        symbols = parse_constituent_csv(csv_body)
        assert symbols == ["RELIANCE", "TCS"]

    def test_deduplicates_preserving_order(self):
        csv_body = (
            "Symbol,Series\n"
            "RELIANCE,EQ\n"
            "TCS,EQ\n"
            "RELIANCE,EQ\n"
            "INFY,EQ\n"
        )
        symbols = parse_constituent_csv(csv_body)
        assert symbols == ["RELIANCE", "TCS", "INFY"]

    def test_tolerates_whitespace_and_case(self):
        csv_body = (
            "  Symbol  ,  Series  \n"
            "  reliance  ,  eq  \n"
            "  TCS  ,  EQ  \n"
        )
        symbols = parse_constituent_csv(csv_body)
        assert symbols == ["RELIANCE", "TCS"]

    def test_empty_csv_returns_empty_list(self):
        assert parse_constituent_csv("Symbol,Series\n") == []

    def test_filters_dummy_placeholder_tickers(self):
        """NSE issues DUMMYVEDL1-4 etc during corporate actions; these
        aren't tradable and Kite has no instrument_token for them."""
        csv_body = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "Reliance,Energy,RELIANCE,EQ,INE002A01018\n"
            "Vedanta Dummy 1,Materials,DUMMYVEDL1,EQ,INE_DUMMY1\n"
            "Vedanta Dummy 2,Materials,DUMMYVEDL2,EQ,INE_DUMMY2\n"
            "TCS,IT,TCS,EQ,INE467B01029\n"
        )
        symbols = parse_constituent_csv(csv_body)
        assert symbols == ["RELIANCE", "TCS"]

    def test_dummy_filter_is_case_insensitive(self):
        csv_body = (
            "Symbol,Series\n"
            "dummyXYZ,EQ\n"
            "Dummy123,EQ\n"
            "RELIANCE,EQ\n"
        )
        assert parse_constituent_csv(csv_body) == ["RELIANCE"]


class TestBundledFallback:
    """The static bundled list remains accessible regardless of live fetch."""

    def test_get_universe_symbols_nifty50_returns_50(self):
        symbols = get_universe_symbols("nifty50")
        assert len(symbols) == 50
        assert "RELIANCE" in symbols
        assert "TCS" in symbols

    def test_get_universe_symbols_nifty500_matches_bundled_subset(self):
        symbols = get_universe_symbols("nifty500")
        assert symbols == NIFTY_500_SUBSET


class TestUniverseAliases:
    """The Settings UI exposes 'all' — it should resolve to a live source,
    not silently fall back to the bundled list."""

    def test_all_alias_maps_to_nifty500(self):
        from yolovest.data.nse_symbols import _NIFTY_CSV_URLS, _UNIVERSE_ALIASES

        assert _UNIVERSE_ALIASES.get("all") == "nifty500"
        # And nifty500 has a real URL configured
        assert "nifty500" in _NIFTY_CSV_URLS
