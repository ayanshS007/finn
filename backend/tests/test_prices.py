import pandas as pd

from finn.sources import prices


def raw_frame(fixtures):
    return pd.read_csv(fixtures / "prices_raw.csv", header=[0, 1], index_col=0, parse_dates=True)


def test_tidy_from_raw_yfinance_frame(fixtures):
    raw = raw_frame(fixtures)
    df = prices.tidy(raw, ["INFY", "HDFCBANK", "ETERNAL", "MISSING"])
    assert list(df.columns) == prices.COLUMNS
    assert set(df["symbol"]) == {"INFY", "HDFCBANK", "ETERNAL"}  # MISSING simply has no rows
    assert (df.groupby("symbol").size() >= 60).all()
    assert df["close"].gt(0).all() and df["volume"].ge(0).all()
    assert (
        df["date"].is_monotonic_increasing
        or df.groupby("symbol")["date"].apply(lambda s: s.is_monotonic_increasing).all()
    )
    first = df[df["symbol"] == "INFY"].iloc[0]
    assert first["close"] == raw[("INFY.NS", "Close")].dropna().iloc[0]


def test_tidy_empty_download_gives_empty_typed_frame():
    df = prices.tidy(pd.DataFrame(), ["INFY"])
    assert df.empty and list(df.columns) == prices.COLUMNS


def test_fetch_prices_one_batched_call_cached_as_parquet(tmp_path, fixtures):
    raw = raw_frame(fixtures)
    calls = []

    def downloader(tickers, days):
        calls.append((tickers, days))
        return raw

    path = tmp_path / "p.parquet"
    syms = ["INFY", "HDFCBANK", "ETERNAL"]
    a = prices.fetch_prices(syms, cache_path=path, downloader=downloader)
    b = prices.fetch_prices(syms, cache_path=path, downloader=downloader)
    assert calls == [(["INFY.NS", "HDFCBANK.NS", "ETERNAL.NS"], 120)]
    assert path.exists()
    pd.testing.assert_frame_equal(a, b)
    prices.fetch_prices(syms, ttl=0, cache_path=path, downloader=downloader)
    assert len(calls) == 2


def test_duplicate_symbols_do_not_duplicate_rows(fixtures):
    once = prices.tidy(raw_frame(fixtures), ["INFY"])
    twice = prices.tidy(raw_frame(fixtures), ["INFY", "INFY"])
    assert len(once) == len(twice)
