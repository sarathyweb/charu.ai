"""Celery tasks for Gmail-driven calls and task creation."""

from __future__ import annotations

import logging

from app.celery_app import celery_app, run_async
from app.db import async_session_factory
from app.services.email_automation_service import EmailAutomationService

logger = logging.getLogger(__name__)


async def _run_email_automation_sweep() -> dict:
    """Run Gmail automation for all eligible users."""
    async with async_session_factory() as session:
        summary = await EmailAutomationService(session).run_sweep()

    result = {
        "users_scanned": summary.users_scanned,
        "emails_scanned": summary.emails_scanned,
        "urgent_calls_scheduled": summary.urgent_calls_scheduled,
        "tasks_created": summary.tasks_created,
        "skipped": summary.skipped,
        "errors": summary.errors,
    }
    logger.info("email_automation_sweep: %s", result)
    return result


@celery_app.task(name="app.tasks.email_automation.email_automation_sweep")
def email_automation_sweep() -> dict:
    """Periodic Gmail automation sweep."""
    return run_async(_run_email_automation_sweep())
