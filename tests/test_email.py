"""Email channel: configuration from the environment, formatting, SMTP failure
classification (§9). No network anywhere — smtplib is replaced by a double."""

from __future__ import annotations

import smtplib
from typing import ClassVar

import pytest

from jobwatch.notify.email import (
    EmailChannel,
    EmailConfig,
    EmailError,
    EmailSender,
    build_alarm_email,
    build_digest_email,
    build_job_email,
)

PAYLOAD = {
    "type": "job",
    "company": "Stripe",
    "company_slug": "stripe",
    "title": "Software Engineering Intern, Summer 2027",
    "url": "https://careers.stripe.com/jobs/1",
    "locations": ["San Francisco", "Seattle", "NYC"],
    "classification": "match",
    "detected_at": "2026-08-02T12:00:00Z",
    "ui_url": "http://127.0.0.1:8080/?q=abc",
}

ENV = {
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_USERNAME": "me@gmail.com",
    "SMTP_PASSWORD": "abcdefghijklmnop",
    "EMAIL_TO": "me@gmail.com",
}


def config(**overrides) -> EmailConfig:
    base = {"host": "smtp.test", "recipients": ["me@example.com"], "username": "me@example.com"}
    return EmailConfig(**{**base, **overrides})


def html_of(message) -> str:
    return message.get_body(preferencelist=("html",)).get_content()


def text_of(message) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


# ── configuration ─────────────────────────────────────────────────────────


def test_email_is_off_until_both_required_variables_are_set():
    """The normal state for someone on Discord alone must not be an error."""
    assert EmailConfig.from_env({}) is None
    assert EmailConfig.from_env({"SMTP_HOST": "smtp.test"}) is None, "no recipient"
    assert EmailConfig.from_env({"EMAIL_TO": "me@example.com"}) is None, "no server"
    assert EmailConfig.from_env(ENV) is not None


def test_port_defaults_follow_the_chosen_security():
    assert EmailConfig.from_env(ENV).port == 587                      # starttls
    assert EmailConfig.from_env({**ENV, "SMTP_SECURITY": "ssl"}).port == 465
    assert EmailConfig.from_env({**ENV, "SMTP_SECURITY": "none"}).port == 25
    assert EmailConfig.from_env({**ENV, "SMTP_PORT": "2525"}).port == 2525


def test_an_unreadable_port_or_security_falls_back_rather_than_crashing():
    """A typo in .env must not stop the service from starting."""
    assert EmailConfig.from_env({**ENV, "SMTP_PORT": "five-eight-seven"}).port == 587
    assert EmailConfig.from_env({**ENV, "SMTP_SECURITY": "quantum"}).security == "starttls"


def test_a_gmail_app_password_pasted_with_its_display_spaces_still_works():
    """Google shows the secret as four quads; the spaces are not part of it."""
    cfg = EmailConfig.from_env({**ENV, "SMTP_PASSWORD": "abcd efgh ijkl mnop"})
    assert cfg.password == "abcdefghijklmnop"


def test_a_real_password_containing_spaces_is_left_alone():
    cfg = EmailConfig.from_env({**ENV, "SMTP_PASSWORD": "correct horse battery staple"})
    assert cfg.password == "correct horse battery staple"


def test_several_recipients_can_be_given_at_once():
    cfg = EmailConfig.from_env({**ENV, "EMAIL_TO": "a@x.com, b@y.com; c@z.com"})
    assert cfg.recipients == ["a@x.com", "b@y.com", "c@z.com"]


def test_junk_recipients_are_dropped_not_sent_to():
    cfg = EmailConfig.from_env({**ENV, "EMAIL_TO": "me@example.com, not-an-address"})
    assert cfg.recipients == ["me@example.com"]


def test_the_sender_defaults_to_the_login_and_honours_a_display_name():
    assert EmailConfig.from_env(ENV).sender == "me@gmail.com"
    cfg = EmailConfig.from_env({**ENV, "EMAIL_FROM": "jobwatch bot <bot@example.com>"})
    assert cfg.sender == "bot@example.com"
    assert "jobwatch bot" in cfg.from_header


def test_the_summary_never_leaks_the_password():
    cfg = EmailConfig.from_env(ENV)
    assert "abcdefghijklmnop" not in cfg.summary
    assert "smtp.gmail.com:587" in cfg.summary


# ── formatting ────────────────────────────────────────────────────────────


def test_a_job_email_leads_with_the_apply_link():
    message = build_job_email(PAYLOAD, config())

    assert "Stripe" in message["Subject"]
    assert "Software Engineering Intern" in message["Subject"]
    assert PAYLOAD["url"] in html_of(message)
    assert PAYLOAD["url"] in text_of(message)
    assert "San Francisco" in html_of(message)


def test_every_job_email_carries_a_plain_text_alternative():
    """Not every mail client renders HTML, and search indexes the text part."""
    message = build_job_email(PAYLOAD, config())
    assert message.is_multipart()
    assert "Apply:" in text_of(message)


def test_a_review_posting_says_so_instead_of_looking_like_a_match():
    message = build_job_email({**PAYLOAD, "classification": "review"}, config())
    assert "review" in html_of(message).lower()


def test_a_hostile_posting_title_cannot_inject_markup():
    """Titles come from third-party boards and land in an HTML body."""
    message = build_job_email(
        {**PAYLOAD, "title": "<script>alert('x')</script> Intern"}, config()
    )
    html = html_of(message)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_digest_lists_every_posting_in_one_message():
    payloads = [{**PAYLOAD, "title": f"Intern {chr(65 + i)}"} for i in range(5)]
    message = build_digest_email(payloads, config())

    assert "5 new postings" in message["Subject"]
    for i in range(5):
        assert f"Intern {chr(65 + i)}" in html_of(message)


def test_an_alarm_is_not_dressed_up_as_a_posting():
    message = build_alarm_email(
        {"type": "alarm", "title": "stripe/greenhouse returned 0 postings", "body": "check it"},
        config(),
    )
    assert "alarm" in message["Subject"].lower()
    assert "check it" in text_of(message)


def test_the_subject_prefix_is_configurable_for_filtering():
    message = build_job_email(PAYLOAD, config(subject_prefix="[interns]"))
    assert message["Subject"].startswith("[interns]")


def test_a_long_subject_is_truncated_rather_than_rejected_by_the_server():
    message = build_job_email({**PAYLOAD, "title": "Intern " * 100}, config())
    assert len(message["Subject"]) <= 180


# ── transport ─────────────────────────────────────────────────────────────


class FakeSMTP:
    """Stands in for smtplib.SMTP. Records what a real server would have seen."""

    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port, timeout=None, raises: Exception | None = None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.login_args: tuple | None = None
        self.sent: list = []
        self.raises = raises
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message, from_addr=None, to_addrs=None):
        if self.raises:
            raise self.raises
        self.sent.append((message, from_addr, to_addrs))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def fail_with(monkeypatch, exc: Exception) -> None:
    monkeypatch.setattr(
        smtplib, "SMTP", lambda host, port, timeout=None: FakeSMTP(host, port, timeout, raises=exc)
    )


async def test_a_message_goes_out_over_starttls(smtp):
    sender = EmailSender(config(password="pw"))
    await sender.send_message(build_job_email(PAYLOAD, config()))

    server = smtp.instances[0]
    assert (server.host, server.port) == ("smtp.test", 587)
    assert server.started_tls
    assert server.login_args == ("me@example.com", "pw")
    assert len(server.sent) == 1


async def test_a_relay_without_a_password_is_not_offered_a_login(smtp):
    """Local relays take no credentials, and AUTH with an empty one is refused."""
    await EmailSender(config()).send_message(build_job_email(PAYLOAD, config()))

    assert smtp.instances[0].login_args is None
    assert len(smtp.instances[0].sent) == 1


async def test_ssl_mode_does_not_also_issue_starttls(smtp):
    sender = EmailSender(config(security="ssl", port=465, password="pw"))
    await sender.send_message(build_job_email(PAYLOAD, config()))

    server = smtp.instances[0]
    assert server.started_tls is False
    assert server.login_args == ("me@example.com", "pw")


async def test_the_envelope_recipients_are_the_configured_ones(smtp):
    cfg = config(recipients=["a@x.com", "b@y.com"], sender="bot@x.com")
    await EmailSender(cfg).send_message(build_job_email(PAYLOAD, cfg))

    _message, from_addr, to_addrs = smtp.instances[0].sent[0]
    assert from_addr == "bot@x.com"
    assert to_addrs == ["a@x.com", "b@y.com"]


async def test_a_rejected_password_fails_permanently_instead_of_retrying(monkeypatch):
    """Five attempts against a wrong password is five attempts wasted."""
    fail_with(monkeypatch, smtplib.SMTPAuthenticationError(535, b"bad credentials"))

    with pytest.raises(EmailError) as exc:
        await EmailSender(config(password="wrong")).send_message(
            build_job_email(PAYLOAD, config())
        )

    assert exc.value.retryable is False
    assert "App Password" in str(exc.value), "the fix belongs in the error"


async def test_a_refused_recipient_fails_permanently(monkeypatch):
    fail_with(monkeypatch, smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"no such user")}))

    with pytest.raises(EmailError) as exc:
        await EmailSender(config()).send_message(build_job_email(PAYLOAD, config()))

    assert exc.value.retryable is False


async def test_a_temporary_server_error_is_retried(monkeypatch):
    fail_with(monkeypatch, smtplib.SMTPResponseException(451, b"try again later"))

    with pytest.raises(EmailError) as exc:
        await EmailSender(config()).send_message(build_job_email(PAYLOAD, config()))

    assert exc.value.retryable is True


async def test_a_permanent_server_error_is_not_retried(monkeypatch):
    fail_with(monkeypatch, smtplib.SMTPResponseException(550, b"message rejected"))

    with pytest.raises(EmailError) as exc:
        await EmailSender(config()).send_message(build_job_email(PAYLOAD, config()))

    assert exc.value.retryable is False


async def test_an_unreachable_server_is_retried(monkeypatch):
    fail_with(monkeypatch, OSError("connection refused"))

    with pytest.raises(EmailError) as exc:
        await EmailSender(config()).send_message(build_job_email(PAYLOAD, config()))

    assert exc.value.retryable is True


async def test_an_unconfigured_sender_fails_permanently_and_says_what_to_set():
    with pytest.raises(EmailError) as exc:
        await EmailSender(None).send_message(build_job_email(PAYLOAD, config()))

    assert exc.value.retryable is False
    assert "SMTP_HOST" in str(exc.value)


# ── the channel contract ──────────────────────────────────────────────────


async def test_the_channel_reports_whether_it_can_send():
    assert EmailChannel(EmailSender(None)).configured is False
    assert EmailChannel(EmailSender(config())).configured is True


async def test_a_digest_batch_becomes_exactly_one_email(smtp):
    channel = EmailChannel(EmailSender(config()))
    await channel.send([{**PAYLOAD, "title": f"Intern {i}"} for i in range(4)], kind="digest")

    assert len(smtp.instances) == 1, "four postings should not open four connections"
    assert "4 new postings" in smtp.instances[0].sent[0][0]["Subject"]


async def test_an_alarm_payload_is_formatted_as_an_alarm(smtp):
    channel = EmailChannel(EmailSender(config()))
    await channel.send([{"type": "alarm", "title": "source down", "body": "b"}], kind="alarm")

    assert "alarm" in smtp.instances[0].sent[0][0]["Subject"].lower()
