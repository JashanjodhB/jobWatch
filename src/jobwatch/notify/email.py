"""Email delivery over SMTP (§9).

The second channel. Discord arrives faster and takes a minute to set up; email
is what *survives* — searchable, archivable, and reachable from a machine that
has never had Discord installed. The two are independent: whichever is
configured gets the alert, and neither one's absence affects the other.

Configuration is entirely environment-driven. Five of the seven values are
useless without the two that are secrets, so splitting them across settings.yaml
and .env would only create a second place to look. `EmailConfig.from_env()`
returns None when the required variables are absent, which is what makes email
opt-in without a feature flag: an unconfigured channel is simply not in the
list, and nothing queues or fails on its behalf.

Transport is stdlib smtplib on a worker thread. aiosmtplib would remove the
thread hop, but one blocking call per posting does not justify a dependency.
"""

from __future__ import annotations

import asyncio
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Any

from ..logging_setup import get_logger
from .base import DeliveryError, host_of, humanize_age

__all__ = [
    "EmailChannel",
    "EmailConfig",
    "EmailError",
    "EmailSender",
    "build_alarm_email",
    "build_digest_email",
    "build_email",
    "build_job_email",
]

log = get_logger(__name__)

# §10 token system, as CSS hex. Inline styles only: Gmail strips <style> blocks.
COLOR_CONFIRMED = "#5ec1a0"
COLOR_SIGNAL = "#f5a524"
COLOR_ALARM = "#e5484d"
COLOR_TEXT = "#1b1f24"
COLOR_MUTED = "#7a8499"
COLOR_BORDER = "#e3e6ea"

SECURITY_ALIASES = {
    "starttls": "starttls", "tls": "starttls", "start_tls": "starttls",
    "ssl": "ssl", "smtps": "ssl", "tls_wrapper": "ssl",
    "none": "none", "plain": "none", "": "starttls",
}
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}

# Google shows an app password as four lowercase quads. It is a single 16-char
# secret; the spaces are display only, and pasting them as-is fails auth in a
# way whose error message says nothing about spaces.
_APP_PASSWORD = re.compile(r"^[a-z]{4}( [a-z]{4}){3}$")


class EmailError(DeliveryError):
    """An SMTP failure, classified into 'try again' and 'never going to work'."""

    def __init__(self, message: str, *, retryable: bool = True, code: int | None = None) -> None:
        super().__init__(message, retryable=retryable)
        self.code = code


# ── configuration ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class EmailConfig:
    host: str
    recipients: list[str]
    port: int = 587
    security: str = "starttls"          # 'starttls' | 'ssl' | 'none'
    username: str | None = None
    password: str | None = None
    sender: str = ""                    # envelope + From:, defaults to username
    sender_name: str = "jobwatch"
    subject_prefix: str = "[jobwatch]"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.sender:
            self.sender = self.username or f"jobwatch@{self.host}"

    @property
    def from_header(self) -> str:
        return formataddr((self.sender_name, self.sender))

    @property
    def summary(self) -> str:
        """One line for logs and the UI. Never includes the password."""
        who = self.username or "anonymous"
        return f"{who}@{self.host}:{self.port} ({self.security}) → {', '.join(self.recipients)}"

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, *, subject_prefix: str = "[jobwatch]",
        timeout_seconds: float = 30.0,
    ) -> EmailConfig | None:
        """Build from SMTP_* / EMAIL_*, or None if email is not set up.

        Returning None rather than raising is deliberate: no email configuration
        is the normal state for someone running on Discord alone, and it must
        not be an error at startup.
        """
        src = os.environ if env is None else env

        host = (src.get("SMTP_HOST") or "").strip()
        recipients = _addresses(src.get("EMAIL_TO") or "")
        if not host or not recipients:
            return None

        security = SECURITY_ALIASES.get((src.get("SMTP_SECURITY") or "").strip().lower())
        if security is None:
            log.warning(
                "smtp_security_unrecognized",
                value=src.get("SMTP_SECURITY"),
                using="starttls",
                valid=sorted(set(SECURITY_ALIASES.values())),
            )
            security = "starttls"

        raw_port = (src.get("SMTP_PORT") or "").strip()
        try:
            port = int(raw_port) if raw_port else DEFAULT_PORTS[security]
        except ValueError:
            log.warning("smtp_port_invalid", value=raw_port, using=DEFAULT_PORTS[security])
            port = DEFAULT_PORTS[security]

        username = (src.get("SMTP_USERNAME") or src.get("SMTP_USER") or "").strip() or None
        password = src.get("SMTP_PASSWORD") or src.get("SMTP_PASS") or None
        if password and _APP_PASSWORD.match(password.strip()):
            password = password.strip().replace(" ", "")

        sender = (src.get("EMAIL_FROM") or "").strip()
        name, address = parseaddr(sender)
        return cls(
            host=host,
            port=port,
            security=security,
            username=username,
            password=password,
            recipients=recipients,
            sender=address or username or "",
            sender_name=name or "jobwatch",
            subject_prefix=subject_prefix,
            timeout_seconds=timeout_seconds,
        )


def _addresses(raw: str) -> list[str]:
    """Split EMAIL_TO on commas or semicolons, keeping only plausible addresses."""
    parts = [p.strip() for p in re.split(r"[,;]", raw)]
    return [parseaddr(p)[1] for p in parts if p and "@" in parseaddr(p)[1]]


# ── formatting ────────────────────────────────────────────────────────────


def _escape(value: Any) -> str:
    """HTML-escape. Every string in an email body is third-party text."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _shell(inner: str, accent: str) -> str:
    """A 600px card. Tables and inline styles, because this renders in Gmail."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f6f7f9;padding:24px 12px;font-family:-apple-system,'
        'BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif">'
        '<tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;width:100%;background:#ffffff;border:1px solid '
        f'{COLOR_BORDER};border-top:3px solid {accent};border-radius:6px">'
        f'<tr><td style="padding:22px 24px">{inner}</td></tr>'
        "</table></td></tr></table>"
    )


def _subject(prefix: str, text: str) -> str:
    subject = f"{prefix} {text}".strip()
    # Long subjects are folded by the client anyway, and some servers baulk past 255.
    return subject[:180]


def build_job_email(payload: dict[str, Any], config: EmailConfig) -> EmailMessage:
    """One posting. The apply link is the only thing that matters; it goes first."""
    is_review = payload.get("classification") == "review"
    accent = COLOR_SIGNAL if is_review else COLOR_CONFIRMED
    company = str(payload.get("company") or payload.get("company_slug") or "?")
    title = str(payload.get("title") or "(untitled)")
    url = str(payload.get("url") or "")
    locations = [str(x) for x in (payload.get("locations") or [])]
    detected = humanize_age(payload.get("detected_at"))
    via = host_of(url)

    place = " · ".join(locations[:4])
    if len(locations) > 4:
        place += f" · +{len(locations) - 4} more"

    text_lines = [f"{company} — {title}", ""]
    if place:
        text_lines.append(place)
    text_lines.append(f"Detected {detected} ago · via {via}")
    text_lines.append("")
    if url:
        text_lines.append(f"Apply: {url}")
    if payload.get("ui_url"):
        text_lines.append(f"Triage: {payload['ui_url']}")
    if is_review:
        text_lines += ["", "This one needs review — one key in the review queue decides it forever."]

    body = [
        f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{COLOR_MUTED}">{_escape(company)}</div>',
        f'<div style="font-size:20px;font-weight:600;color:{COLOR_TEXT};margin:6px 0 12px">'
        f"{_escape(title)}</div>",
    ]
    if place:
        body.append(
            f'<div style="font-size:14px;color:{COLOR_TEXT};margin-bottom:4px">{_escape(place)}</div>'
        )
    body.append(
        f'<div style="font-size:13px;color:{COLOR_MUTED};margin-bottom:18px">'
        f"Detected {_escape(detected)} ago · via {_escape(via)}</div>"
    )
    if url:
        body.append(
            f'<a href="{_escape(url)}" style="display:inline-block;background:{accent};'
            f'color:#0b1220;font-weight:600;font-size:15px;text-decoration:none;'
            'padding:11px 22px;border-radius:5px">Apply →</a>'
        )
    if payload.get("ui_url"):
        body.append(
            f'<div style="margin-top:16px;font-size:13px">'
            f'<a href="{_escape(payload["ui_url"])}" style="color:{COLOR_MUTED}">'
            "Triage in jobwatch</a></div>"
        )
    if is_review:
        body.append(
            f'<div style="margin-top:18px;padding:10px 12px;background:#fff8ea;'
            f'border-left:3px solid {COLOR_SIGNAL};font-size:13px;color:{COLOR_TEXT}">'
            "Needs review — one keystroke in the review queue classifies every future "
            "posting with this title.</div>"
        )
    elif payload.get("category"):
        body.append(
            f'<div style="margin-top:16px;font-size:12px;color:{COLOR_MUTED}">'
            f"{_escape(payload['category'])}</div>"
        )

    return _message(
        config,
        subject=_subject(config.subject_prefix, f"{company} — {title}"),
        text="\n".join(text_lines).strip() + "\n",
        html=_shell("".join(body), accent),
    )


def build_digest_email(payloads: list[dict[str, Any]], config: EmailConfig) -> EmailMessage:
    """Warm and cold tiers accumulate into one message on an interval."""
    count = len(payloads)
    rows: list[str] = []
    text_lines = [f"{count} new posting{'s' if count != 1 else ''}", ""]

    for item in payloads:
        company = str(item.get("company") or item.get("company_slug") or "?")
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        locations = " · ".join(str(x) for x in (item.get("locations") or [])[:3])
        mark = "?" if item.get("classification") == "review" else "·"

        text_lines.append(f"{mark} {company} — {title}")
        if locations:
            text_lines.append(f"    {locations}")
        if url:
            text_lines.append(f"    {url}")

        link = (
            f'<a href="{_escape(url)}" style="color:{COLOR_TEXT};text-decoration:none;'
            f'font-weight:600">{_escape(title)}</a>'
            if url
            else f'<span style="font-weight:600">{_escape(title)}</span>'
        )
        rows.append(
            f'<tr><td style="padding:10px 0;border-bottom:1px solid {COLOR_BORDER}">'
            f'<div style="font-size:12px;color:{COLOR_MUTED}">{_escape(company)}'
            f'{" · needs review" if mark == "?" else ""}</div>'
            f'<div style="font-size:15px;margin-top:2px">{link}</div>'
            + (
                f'<div style="font-size:12px;color:{COLOR_MUTED};margin-top:2px">'
                f"{_escape(locations)}</div>"
                if locations
                else ""
            )
            + "</td></tr>"
        )

    inner = (
        f'<div style="font-size:18px;font-weight:600;color:{COLOR_TEXT};margin-bottom:6px">'
        f"{count} new posting{'s' if count != 1 else ''}</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
    )
    return _message(
        config,
        subject=_subject(config.subject_prefix, f"{count} new posting{'s' if count != 1 else ''}"),
        text="\n".join(text_lines) + "\n",
        html=_shell(inner, COLOR_MUTED),
    )


def build_alarm_email(payload: dict[str, Any], config: EmailConfig) -> EmailMessage:
    """Source failure and drift. Styled apart from postings on purpose (§12)."""
    title = str(payload.get("title") or "Source alarm")
    body = str(payload.get("body") or "")
    footer = str(payload.get("footer") or "jobwatch health")
    inner = (
        f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{COLOR_ALARM}">Alarm</div>'
        f'<div style="font-size:18px;font-weight:600;color:{COLOR_TEXT};margin:6px 0 12px">'
        f"{_escape(title)}</div>"
        f'<div style="font-size:14px;color:{COLOR_TEXT};white-space:pre-wrap">{_escape(body)}</div>'
        f'<div style="margin-top:18px;font-size:12px;color:{COLOR_MUTED}">{_escape(footer)}</div>'
    )
    return _message(
        config,
        subject=_subject(config.subject_prefix, f"alarm — {title}"),
        text=f"{title}\n\n{body}\n\n{footer}\n",
        html=_shell(inner, COLOR_ALARM),
    )


def build_email(payload: dict[str, Any], config: EmailConfig) -> EmailMessage:
    if payload.get("type") == "alarm":
        return build_alarm_email(payload, config)
    return build_job_email(payload, config)


def _message(config: EmailConfig, *, subject: str, text: str, html: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.from_header
    message["To"] = ", ".join(config.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=config.host)
    # Alerts are transactional; nothing here should trigger a vacation reply
    # or land in a promotions tab if it can be helped.
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


# ── transport ─────────────────────────────────────────────────────────────


@dataclass
class EmailSender:
    """Sends one message per call over a fresh SMTP connection.

    A connection per message is the right trade at this volume: alerts arrive in
    ones and twos, a pooled connection would sit idle for hours and be dropped
    by the server anyway, and reconnecting costs a few hundred milliseconds
    against a poll interval measured in minutes.
    """

    config: EmailConfig | None

    @property
    def configured(self) -> bool:
        return self.config is not None

    async def send_message(self, message: EmailMessage) -> None:
        if self.config is None:
            raise EmailError(
                "email is not configured — set SMTP_HOST and EMAIL_TO in .env",
                retryable=False,
            )
        # smtplib is blocking; the event loop is running the scheduler.
        await asyncio.to_thread(self._send_blocking, self.config, message)

    @staticmethod
    def _send_blocking(config: EmailConfig, message: EmailMessage) -> None:
        try:
            if config.security == "ssl":
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                    config.host, config.port, timeout=config.timeout_seconds
                )
            else:
                smtp = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)

            with smtp:
                smtp.ehlo()
                if config.security == "starttls":
                    smtp.starttls()
                    smtp.ehlo()
                if config.username and config.password:
                    smtp.login(config.username, config.password)
                smtp.send_message(message, from_addr=config.sender, to_addrs=config.recipients)

        except smtplib.SMTPAuthenticationError as exc:
            raise EmailError(
                f"SMTP rejected the credentials (code {exc.smtp_code}) — for Gmail this "
                "must be a 16-character App Password, not the account password",
                retryable=False,
                code=exc.smtp_code,
            ) from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise EmailError(
                f"the server does not support what was asked of it: {exc} — check SMTP_SECURITY",
                retryable=False,
            ) from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise EmailError(
                f"the server refused the addresses: {exc} — check EMAIL_TO and EMAIL_FROM",
                retryable=False,
            ) from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx is a deferral and will clear; 5xx is a verdict and will not.
            permanent = 500 <= int(exc.smtp_code or 0) < 600
            raise EmailError(
                f"SMTP {exc.smtp_code}: {_decode(exc.smtp_error)}",
                retryable=not permanent,
                code=exc.smtp_code,
            ) from exc
        # OSError covers the connection failures and TimeoutError alike.
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailError(f"{type(exc).__name__}: {exc}") from exc


@dataclass
class EmailChannel:
    """Adapts the SMTP sender to the outbox's channel contract."""

    sender: EmailSender
    name: str = "email"

    @property
    def configured(self) -> bool:
        return self.sender.configured

    async def send(self, payloads: list[dict[str, Any]], *, kind: str = "job") -> None:
        config = self.sender.config
        if config is None:
            raise EmailError(
                "email is not configured — set SMTP_HOST and EMAIL_TO in .env",
                retryable=False,
            )
        if not payloads:
            return

        if kind == "digest" and len(payloads) > 1:
            messages = [build_digest_email(payloads, config)]
        else:
            messages = [build_email(payload, config) for payload in payloads]

        for message in messages:
            await self.sender.send_message(message)


def _decode(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")
