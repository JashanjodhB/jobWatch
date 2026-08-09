"""Entrypoint: scheduler, outbox worker, and web UI as sibling asyncio tasks.

The scheduler is the product; the UI is an accessory. So the web task is
supervised separately and an unhandled error inside it is logged and the task
restarted — it can never take the poller down (§13.8).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from .config import AppConfig
from .logging_setup import get_logger
from .service import Service, build_service, ensure_ready

__all__ = ["run_service"]

log = get_logger(__name__)

WEB_RESTART_DELAY = 5.0


async def run_service(
    config: AppConfig | None = None,
    *,
    dry_run: bool = False,
    with_web: bool = True,
    once: bool = False,
) -> int:
    cfg = config or AppConfig.load()
    ensure_ready(cfg)

    service = build_service(cfg, dry_run=dry_run)
    service.scheduler.startup_maintenance()

    log.info(
        "jobwatch_starting",
        db=str(cfg.db_path),
        sources=len(service.scheduler.all_sources()),
        dry_run=dry_run,
        web=with_web,
    )

    if once:
        try:
            report = await service.scheduler.tick()
            await service.outbox.flush()
            log.info(
                "single_tick_complete",
                polled=report.polled,
                ok=report.succeeded,
                failed=report.failed,
                alerted=report.alerted,
            )
            return 0
        finally:
            await service.aclose()

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(service.scheduler.run_forever(), name="scheduler"),
        asyncio.create_task(service.outbox.run_forever(), name="outbox"),
    ]
    if with_web:
        tasks.append(asyncio.create_task(_supervised_web(service, stop), name="web"))

    waiter = asyncio.create_task(stop.wait(), name="stop")
    done, _pending = await asyncio.wait(
        [*tasks, waiter], return_when=asyncio.FIRST_COMPLETED
    )

    for task in done:
        if task is waiter:
            continue
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            log.error("task_crashed", task=task.get_name(), error=str(exc), exc_info=exc)

    log.info("jobwatch_stopping")
    for task in [*tasks, waiter]:
        task.cancel()
    await asyncio.gather(*tasks, waiter, return_exceptions=True)
    await service.aclose()
    return 0


async def _supervised_web(service: Service, stop: asyncio.Event) -> None:
    """Run uvicorn, restarting it if it dies. Never propagates upward.

    A traceback in a route handler is a bug in an accessory. The poller keeps
    polling either way — that is the whole point of the supervision.
    """
    import uvicorn

    from .web.app import create_app

    web_cfg = service.config.settings.web

    while not stop.is_set():
        try:
            app = create_app(service)
            config = uvicorn.Config(
                app,
                host=web_cfg.bind_host,
                port=web_cfg.bind_port,
                log_config=None,
                access_log=False,
                lifespan="on",
            )
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            log.info("web_started", host=web_cfg.bind_host, port=web_cfg.bind_port)
            await server.serve()
            if stop.is_set():
                return
            log.warning("web_exited_unexpectedly", note="restarting; the poller is unaffected")
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log.error(
                "web_bind_failed",
                host=web_cfg.bind_host,
                port=web_cfg.bind_port,
                error=str(exc),
                note="the scheduler continues without the UI",
            )
            return
        # §13.8: the web task is an accessory. Nothing it does may escape to the poller.
        except Exception as exc:
            log.error("web_crashed", error=str(exc), exc_info=True)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=WEB_RESTART_DELAY)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows has no add_signal_handler for SIGTERM; KeyboardInterrupt
            # still unwinds cleanly through asyncio.run.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: stop.set())


def main() -> int:
    from .cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
