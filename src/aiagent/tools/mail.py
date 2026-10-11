"""Sending e-mail over SMTP. Only registered when SMTP_HOST is configured."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ..config import Config
from . import Tool, ToolError, schema


def build(config: Config) -> list[Tool]:
    smtp = config.smtp
    assert smtp is not None

    def send_email(to: list[str], subject: str, body: str, cc: list[str] | None = None) -> str:
        if not to:
            raise ToolError("Chybí příjemce.")
        msg = EmailMessage()
        msg["From"] = smtp.sender
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as server:
                if smtp.starttls:
                    server.starttls()
                if smtp.user:
                    server.login(smtp.user, smtp.password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ToolError(f"Odeslání e-mailu selhalo: {exc}") from exc
        return f"E-mail '{subject}' odeslán na {', '.join(to + (cc or []))}."

    return [
        Tool(
            name="send_email",
            description=(
                f"Odešle e-mail z adresy {smtp.sender}. Vyžaduje schválení uživatelem; "
                "text e-mailu napiš kompletně, uživatel ho uvidí před odesláním."
            ),
            input_schema=schema(
                {
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Text e-mailu (prostý text)."},
                },
                ["to", "subject", "body"],
            ),
            handler=send_email,
            needs_approval=True,
        )
    ]
