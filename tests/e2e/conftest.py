"""End-to-end tier: a real browser against a real server (§10, §15).

The rest of the suite drives the UI through FastAPI's `TestClient`, which proves
the server returns the right HTML. It cannot prove that HTML *does* anything.
Three things exist only in a browser:

* the HTMX swaps — `hx-target`/`hx-swap` have to land a fragment in the right
  node, and landing it in the wrong one still returns 200;
* every keyboard binding in `static/app.js`, including the modifier guard that
  keeps Ctrl+R a page reload rather than a permanent, global reject;
* `hx-confirm`, which is a native dialog.

That gap is what this directory covers. It is the only place in the suite where
JavaScript runs at all.

Not part of the default run — `addopts = -m 'not e2e'` keeps `pytest` hermetic
and ~16s (§15). To run it:

    .venv\\Scripts\\python.exe -m pytest -m e2e

It needs a real Chrome on the machine, and Selenium resolves its own
chromedriver through Selenium Manager, which is the one point in the suite that
may touch the network — once, then cached. A machine without Chrome *skips*
rather than fails, on the same principle as `AdapterUnavailable` (§3): a missing
optional dependency degrades one tier, it never breaks the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from jobwatch.classify.verdicts import Classifier
from jobwatch.config import AppConfig
from jobwatch.db import Database
from jobwatch.notify.discord import DiscordChannel
from jobwatch.notify.outbox import Outbox
from jobwatch.pipeline import Pipeline
from jobwatch.scheduler import Scheduler
from jobwatch.service import Service
from jobwatch.web.app import create_app

SERVER_START_TIMEOUT = 15.0
SERVER_STOP_TIMEOUT = 10.0

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items) -> None:
    """Mark everything under this directory `e2e`, so a new file cannot forget to.

    The default run excludes the marker, which is what keeps `pytest` hermetic.
    Relying on each module to declare it would make "I forgot the marker" fail
    as "the suite suddenly needs Chrome in CI".
    """
    for item in items:
        if _HERE in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.e2e)


class _Sender:
    """Stands in for DiscordSender. Records embeds instead of posting them."""

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []
        self.configured = True

    async def send_embeds(self, embeds: list[dict], *, content: str | None = None) -> None:
        self.messages.append(embeds)


# ── the app under test ────────────────────────────────────────────────────


@pytest.fixture
def e2e_service(seeded_db: Database, settings, tmp_path) -> Service:
    """A Service wired to a temp DB and a client that never leaves the process."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"jobs": []}))
    )
    classifier = Classifier(seeded_db)
    outbox = Outbox(seeded_db, [DiscordChannel(_Sender())])  # type: ignore[list-item]
    pipeline = Pipeline(seeded_db, classifier, outbox, settings, client)
    return Service(
        config=AppConfig(settings=settings, db_path=tmp_path / "e2e.db", config_dir=tmp_path),
        db=seeded_db,
        client=client,
        classifier=classifier,
        outbox=outbox,
        pipeline=pipeline,
        scheduler=Scheduler(seeded_db, pipeline, settings),
        health=None,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(4),
    )


@dataclass(slots=True)
class LiveServer:
    url: str
    db: Database
    service: Service

    def at(self, path: str) -> str:
        return f"{self.url}{path}"


def _free_port() -> int:
    """Bind :0 and hand back what the OS chose, so parallel runs cannot collide."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(e2e_service: Service):
    """Real uvicorn on a real port, in a thread, for the duration of one test.

    Per-test rather than per-session: these tests write verdicts and rules, and
    a shared server would make them order-dependent. Booting uvicorn costs
    milliseconds; the expensive thing is Chrome, and that is session-scoped.

    Safe across threads because `Database` opens SQLite with
    `check_same_thread=False` behind an `RLock`, so the test thread can assert
    on rows the server thread just wrote.
    """
    port = _free_port()
    config = uvicorn.Config(
        create_app(e2e_service),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name=f"e2e-uvicorn-{port}")
    thread.start()

    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(f"uvicorn did not come up on port {port}")
        time.sleep(0.02)

    try:
        yield LiveServer(f"http://127.0.0.1:{port}", e2e_service.db, e2e_service)
    finally:
        server.should_exit = True
        thread.join(timeout=SERVER_STOP_TIMEOUT)


# ── the browser ───────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def driver() -> Any:
    """One headless Chrome for the whole session — it is the slow part."""
    webdriver = pytest.importorskip(
        "selenium.webdriver", reason="the e2e tier needs the 'e2e' extra: uv sync --extra e2e"
    )
    from selenium.common.exceptions import WebDriverException

    options = webdriver.ChromeOptions()
    for flag in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1440,1000",
    ):
        options.add_argument(flag)

    try:
        instance = webdriver.Chrome(options=options)
    except WebDriverException as exc:
        pytest.skip(f"no usable Chrome for the e2e tier: {exc}")

    # Explicit waits only. See the note in helpers.py.
    instance.implicitly_wait(0)
    instance.set_page_load_timeout(30)
    try:
        yield instance
    finally:
        instance.quit()


@pytest.fixture(autouse=True)
def _clean_browser_state(driver):
    """Each test starts on a blank page with no carried-over dialog or storage."""
    yield
    # Both are best-effort cleanup: a test that already failed must not be
    # reported as an error in teardown instead.
    with contextlib.suppress(Exception):
        driver.switch_to.alert.dismiss()
    with contextlib.suppress(Exception):
        driver.get("about:blank")
