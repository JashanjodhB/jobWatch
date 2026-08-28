"""Adapter package. Import registers; nothing here touches the network.

Optional extras (`selectolax`, `playwright`) are *not* imported at module load.
The adapters that need them import lazily inside `fetch()` and raise
`AdapterUnavailable`, so the service starts normally without them and only the
affected source is skipped (§3, Phase 10 acceptance).
"""

from __future__ import annotations

import importlib

from .base import (
    AdapterError,
    AdapterUnavailable,
    FetchContext,
    SourceAdapter,
    default_fallback,
    get_adapter,
    known_adapters,
    register,
)

__all__ = [
    "AdapterError",
    "AdapterUnavailable",
    "FetchContext",
    "SourceAdapter",
    "default_fallback",
    "ensure_loaded",
    "get_adapter",
    "known_adapters",
    "register",
]

_CORE_MODULES = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "eightfold",
    "oracle",
    "jibe",
    "phenom",
    "workable",
    "rippling",
    "bamboohr",
    "html",
    "browser",
)

_loaded = False


def ensure_loaded() -> None:
    """Import every adapter module once, so the registry is complete."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    for mod in _CORE_MODULES:
        try:
            importlib.import_module(f"{__name__}.{mod}")
        # A broken adapter module must not stop the service from starting.
        except Exception as exc:
            from ..logging_setup import get_logger

            get_logger(__name__).error("adapter_module_failed", module=mod, error=str(exc))

    from . import direct

    direct.load_all()
