"""structlog configuration (§3).

JSON to stdout under systemd, so `journalctl -u jobwatch -f` is the ops surface.
Human-readable console output when attached to a TTY, because that is where
development happens.
"""

from __future__ import annotations

import contextlib
import logging
import sys

import structlog

__all__ = ["configure_logging", "force_utf8_console", "get_logger"]

_configured = False


def force_utf8_console() -> None:
    """Make stdout and stderr able to carry the text we actually print.

    Windows consoles default to a legacy code page (cp1252 here), which cannot
    encode an em-dash. Job titles come from third-party boards and routinely
    contain one, and so does our own output — so without this, logging a posting
    raises UnicodeEncodeError *inside the log call* and takes down whichever
    task made it. Replacing an unencodable character is always better than
    losing the line and the task with it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding in ("utf8", "utf8sig"):
            continue
        # A stream that refuses is not worth failing startup over.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def configure_logging(level: str = "INFO", *, force_json: bool | None = None) -> None:
    global _configured

    force_utf8_console()
    as_json = force_json if force_json is not None else not sys.stderr.isatty()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # uvicorn's own handlers would double-print every line.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if as_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "jobwatch") -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
