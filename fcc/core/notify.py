"""Outbound notifications.

Deliberately dumb: format a message, push it, never raise. A notifier that
throws would take down the 3am waiver job it was supposed to be reporting on.
"""

from __future__ import annotations

import logging

import httpx

from fcc.core.config import get_settings

log = logging.getLogger(__name__)


class Notifier:
    """Sends to ntfy and/or a Discord webhook, whichever are configured."""

    def __init__(self) -> None:
        self.settings = get_settings()

    # -- transport --------------------------------------------------------
    def send(self, title: str, body: str, priority: str = "default", tags: str = "") -> None:
        if self.settings.dry_run:
            log.info("[notify:dry-run] %s — %s", title, body)
        sent = False
        if self.settings.ntfy_topic:
            sent |= self._ntfy(title, body, priority, tags)
        if self.settings.discord_webhook_url:
            sent |= self._discord(title, body)
        if not sent:
            log.info("notify (no transport configured): %s — %s", title, body)

    def _ntfy(self, title: str, body: str, priority: str, tags: str) -> bool:
        url = f"{self.settings.ntfy_server.rstrip('/')}/{self.settings.ntfy_topic}"
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        try:
            httpx.post(url, content=body.encode(), headers=headers, timeout=10).raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - never let notification failure escalate
            log.warning("ntfy notification failed: %s", exc)
            return False

    def _discord(self, title: str, body: str) -> bool:
        try:
            httpx.post(
                self.settings.discord_webhook_url,
                json={"content": f"**{title}**\n{body}"[:1900]},
                timeout=10,
            ).raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("discord notification failed: %s", exc)
            return False

    # -- semantic events --------------------------------------------------
    def needs_approval(self, action) -> None:
        self.send(
            f"Approve: {action.kind.value}",
            f"{action.rationale}\n\nPayload: {action.payload}",
            priority="high",
            tags="warning",
        )

    def action_failed(self, action) -> None:
        self.send(
            f"FAILED: {action.kind.value}",
            f"{action.error}\n\nPayload: {action.payload}",
            priority="urgent",
            tags="rotating_light",
        )

    def action_unverified(self, action) -> None:
        # Submitted but unconfirmed is the dangerous state: it looks like
        # success from the outside. Treat it as loudly as an outright failure.
        self.send(
            f"UNVERIFIED: {action.kind.value}",
            "Submitted, but reading the platform back did not confirm it.\n"
            f"{action.error or ''}\nVerification: {action.verification}",
            priority="urgent",
            tags="warning",
        )

    def review_needed(self, title: str, body: str) -> None:
        self.send(title, body, priority="high", tags="mag")
