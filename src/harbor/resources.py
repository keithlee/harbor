"""Resolve runtime assets from an installed package or source checkout."""

from __future__ import annotations

from pathlib import Path


def runtime_asset(directory: str, name: str) -> Path:
    """Return a packaged asset, falling back to the repository source tree."""

    packaged = Path(__file__).resolve().parent / directory / name
    if packaged.is_file():
        return packaged
    return Path(__file__).resolve().parents[2] / directory / name


__all__ = ["runtime_asset"]
