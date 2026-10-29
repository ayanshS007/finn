"""One batched yfinance download for every .NS symbol, cached as parquet."""

from __future__ import annotations

import hashlib
import time
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd

from finn.config import cache_dir, portfolio_symbols

DAYS = 120
TTL_PRICES = 3600
COLUMNS = ["date", "symbol", "close", "volume"]


def _download(tickers: list[str], days: int) -> pd.DataFrame:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # yfinance trips pandas deprecation warnings
        # auto_adjust=False: Close is split-adjusted, not dividend-adjusted (the traded price).
        return yf.download(
            tickers,
            period=f"{days}d",
            auto_adjust=False,
            group_by="ticker",
            threads=False,
            progress=False,
        )


def tidy(raw: pd.DataFrame, symbols: Iterable[str]) -> pd.DataFrame:
    """yfinance's (ticker, field) wide frame -> tidy rows (date, symbol, close, volume)."""
    frames = []
    for symbol in dict.fromkeys(symbols):
        ticker = f"{symbol}.NS"
        if raw.empty or ticker not in raw.columns.get_level_values(0):
            continue
        part = raw[ticker][["Close", "Volume"]].dropna(subset=["Close"])
        frames.append(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(part.index).tz_localize(None).normalize(),
                    "symbol": symbol,
                    "close": part["Close"].astype(float).to_numpy(),
                    "volume": part["Volume"].fillna(0).astype("int64").to_numpy(),
                }
            )
        )
    if not frames:
        return pd.DataFrame(
            {
                c: pd.Series(dtype=t)
                for c, t in zip(
                    COLUMNS, ["datetime64[ns]", "object", "float64", "int64"], strict=True
                )
            }
        )
    return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"], ignore_index=True)


def fetch_prices(
    symbols: Iterable[str] | None = None,
    days: int = DAYS,
    *,
    ttl: float = TTL_PRICES,
    cache_path: Path | None = None,
    downloader: Callable[[list[str], int], pd.DataFrame] = _download,
) -> pd.DataFrame:
    """Daily close and volume for all symbols. A missing symbol simply has no rows."""
    symbols = list(dict.fromkeys(symbols if symbols is not None else portfolio_symbols()))
    digest = hashlib.sha256(f"{sorted(symbols)}|{days}".encode()).hexdigest()[:16]
    path = cache_path or cache_dir() / f"prices_{digest}.parquet"
    if ttl > 0 and path.exists() and time.time() - path.stat().st_mtime <= ttl:
        return pd.read_parquet(path)
    frame = tidy(downloader([f"{s}.NS" for s in symbols], days), symbols)
    if ttl > 0 and not frame.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    return frame
