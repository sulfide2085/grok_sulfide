from __future__ import annotations

from typing import Callable, Protocol


class MailProvider(Protocol):
    name: str
    aliases: tuple[str, ...]

    def get_email_and_token(self, api_key: str | None = None) -> tuple[str, str]:
        """Return (email_address, mailbox_token)."""
        ...

    def get_oai_code(
        self,
        dev_token: str,
        email: str,
        *,
        timeout: float = 180,
        poll_interval: float = 3,
        log_callback: Callable | None = None,
        cancel_callback: Callable | None = None,
        resend_callback: Callable | None = None,
    ) -> str | None:
        """Poll mailbox and return xAI verification code or None/raise."""
        ...
