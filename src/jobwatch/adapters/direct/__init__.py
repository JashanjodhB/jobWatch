"""Company-owned careers endpoints — the canonical apply path (§5).

One module per company, each registered as `direct.<slug>`. These are bespoke by
nature: built by hand from the DevTools recipe in §5, pinned to a committed
fixture, and expected to break when a company redesigns its careers page. That
is fine — drift detection notices, the source flips to its fallback, and the
company keeps being polled through its ATS in the meantime.

Adding one:
    1. careers page → DevTools → Network → Fetch/XHR → find the JSON
    2. strip the request to the minimum that still returns 200, drop all cookies
    3. save the response to tests/fixtures/direct_<slug>.json
    4. write the mapping here, add a test against the fixture
"""

from __future__ import annotations

import importlib
import pkgutil

_loaded = False


def load_all() -> None:
    """Import every sibling module so `direct.*` adapters self-register."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{info.name}")
        # One malformed direct module must not stop the others registering.
        except Exception as exc:
            from ...logging_setup import get_logger

            get_logger(__name__).error(
                "direct_adapter_failed", module=info.name, error=str(exc)
            )
