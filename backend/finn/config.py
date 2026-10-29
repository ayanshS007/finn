"""Repo paths and config loading."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def cache_dir() -> Path:
    """On-disk cache for source responses (git-ignored). Override with FINN_CACHE_DIR."""
    return Path(os.environ.get("FINN_CACHE_DIR") or REPO_ROOT / "data" / "cache")


def load_portfolio(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_DIR / "portfolio.yaml").read_text())


def portfolio_symbols(path: Path | None = None) -> list[str]:
    return list(load_portfolio(path)["portfolio"])
