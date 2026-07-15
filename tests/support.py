"""Tiny dependency doubles for protocol modules in the local unit-test runtime."""

from __future__ import annotations

import sys
import types


def ensure_aiohttp() -> None:
    """Install just enough of aiohttp's public shape when AstrBot is not installed."""
    try:
        import aiohttp  # noqa: F401
    except ModuleNotFoundError:
        module = types.ModuleType("aiohttp")

        class ClientError(Exception):
            pass

        class ClientSession:
            pass

        class ClientResponse:
            pass

        module.ClientError = ClientError
        module.ClientSession = ClientSession
        module.ClientResponse = ClientResponse
        sys.modules["aiohttp"] = module
