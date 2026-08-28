"""Notification: a durable outbox in front of one or more channels (§9)."""

from .base import Channel, DeliveryError
from .discord import (
    DiscordChannel,
    DiscordSender,
    build_alarm_embed,
    build_digest_embed,
    build_digest_embeds,
    build_job_embed,
)
from .email import EmailChannel, EmailConfig, EmailSender, build_job_email
from .outbox import Outbox, OutboxItem

__all__ = [
    "Channel",
    "DeliveryError",
    "DiscordChannel",
    "DiscordSender",
    "EmailChannel",
    "EmailConfig",
    "EmailSender",
    "Outbox",
    "OutboxItem",
    "build_alarm_embed",
    "build_digest_embed",
    "build_digest_embeds",
    "build_job_email",
    "build_job_embed",
]
