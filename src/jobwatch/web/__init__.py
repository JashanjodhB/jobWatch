"""The local web UI (§10). Single operator, no login, Tailscale-only."""

from .app import create_app

__all__ = ["create_app"]
