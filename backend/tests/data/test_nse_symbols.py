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
