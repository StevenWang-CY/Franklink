"""Shared helpers for deriving safe, high-level email context signals.

This is a small wrapper around the onboarding email-context utilities so other
agents (or scripts) can import a stable path: `app.agents.email_context`.
"""

from __future__ import annotations

from app.agents.execution.onboarding.utils.email_context import (  # noqa: F401
    build_email_signals,
    ensure_email_signals,
    fetch_email_signals,
    should_refresh_email_signals,
)

__all__ = [
    "build_email_signals",
    "ensure_email_signals",
    "fetch_email_signals",
    "should_refresh_email_signals",
]

