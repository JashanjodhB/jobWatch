"""Console encoding (§3).

Job titles come from third-party boards and routinely contain an em-dash. On a
Windows console that means a UnicodeEncodeError raised *inside* a log call,
which takes down whichever task made it — a poller killed by a punctuation mark.
"""

from __future__ import annotations

import io

from jobwatch.logging_setup import force_utf8_console


class FakeStream:
    """A stand-in for a console stream. io.StringIO will not do: its `encoding`
    is read-only, and the encoding is the whole subject here."""

    def __init__(self, encoding: str, *, refuses: bool = False) -> None:
        self.encoding = encoding
        self.refuses = refuses
        self.reconfigured: dict | None = None

    def reconfigure(self, **kwargs) -> None:
        if self.refuses:
            raise ValueError("this stream cannot be reconfigured")
        self.reconfigured = kwargs
        self.encoding = kwargs.get("encoding", self.encoding)


def test_a_legacy_code_page_is_upgraded_to_utf8(monkeypatch):
    out, err = FakeStream("cp1252"), FakeStream("cp1252")
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)

    force_utf8_console()

    assert out.reconfigured == {"encoding": "utf-8", "errors": "replace"}
    assert err.reconfigured == {"encoding": "utf-8", "errors": "replace"}


def test_unencodable_characters_are_replaced_rather_than_raising(monkeypatch):
    """'replace' is the point: losing one glyph beats losing the log line."""
    out = FakeStream("cp1252")
    monkeypatch.setattr("sys.stdout", out)

    force_utf8_console()

    assert out.reconfigured["errors"] == "replace"


def test_a_console_already_on_utf8_is_left_alone(monkeypatch):
    out = FakeStream("UTF-8")
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", FakeStream("utf8"))

    force_utf8_console()

    assert out.reconfigured is None


def test_a_stream_that_refuses_does_not_stop_startup(monkeypatch):
    """Redirected and wrapped streams turn up in the wild; none of them is fatal."""
    monkeypatch.setattr("sys.stdout", FakeStream("cp1252", refuses=True))
    monkeypatch.setattr("sys.stderr", io.StringIO())  # no reconfigure at all

    force_utf8_console()  # must not raise
