"""Gmail automation service for urgent-call and auto-task workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_settings
from app.models.call_log import CallLog
from app.models.email_automation_event import EmailAutomationEvent
from app.models.enums import (
    CallLogStatus,
    CallType,
    EmailAutomationEventType,
    EmailAutomationStatus,
    OccurrenceKind,
)
from app.models.user import User
from app.services.call_log_service import CallLogService
from app.services.gmail_read_service import (
    _is_no_reply_sender,
    get_email_for_reply,
    search_emails,
)
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_URGENT_TERMS: dict[str, float] = {
    "urgent": 0.45,
    "asap": 0.4,
    "immediately": 0.35,
    "emergency": 0.45,
    "critical": 0.35,
    "time sensitive": 0.35,
    "action required": 0.35,
    "blocked": 0.3,
    "blocker": 0.3,
    "deadline": 0.28,
    "due today": 0.35,
    "due by": 0.25,
    "please respond": 0.25,
    "need your response": 0.3,
}

_TASK_TERMS: dict[str, float] = {
    "please": 0.2,
    "can you": 0.3,
    "could you": 0.3,
    "need you to": 0.35,
    "action required": 0.4,
    "todo": 0.35,
    "to-do": 0.35,
    "follow up": 0.3,
    "review": 0.3,
    "approve": 0.35,
    "send": 0.25,
    "schedule": 0.25,
    "complete": 0.25,
    "reply": 0.2,
    "respond": 0.25,
    "deadline": 0.25,
    "due": 0.2,
}


@dataclass(frozen=True, slots=True)
class EmailScore:
    """Score and reasons for one email automation classifier."""

    score: float
    reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class EmailAutomationRunSummary:
    """Counters returned by an email automation sweep."""

    users_scanned: int = 0
    emails_scanned: int = 0
    urgent_calls_scheduled: int = 0
    tasks_created: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class EmailAutomationService:
    """Scans Gmail and turns actionable/urgent emails into Charu actions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        settings = get_settings()
        self.email_automation_enabled = settings.EMAIL_AUTOMATION_ENABLED
        self.urgent_calls_enabled = settings.URGENT_EMAIL_CALLS_ENABLED
        self.auto_tasks_enabled = settings.AUTO_TASK_FROM_EMAILS_ENABLED
        self.lookback_days = max(1, settings.EMAIL_AUTOMATION_LOOKBACK_DAYS)
        self.max_messages = min(
            10,
            max(1, settings.EMAIL_AUTOMATION_MAX_MESSAGES_PER_USER),
        )
        self.urgent_call_delay_minutes = min(
            120,
            max(1, settings.URGENT_EMAIL_CALL_DELAY_MINUTES),
        )
        self.urgent_call_cooldown_minutes = max(
            1,
            settings.URGENT_EMAIL_CALL_COOLDOWN_MINUTES,
        )
        self.urgent_call_max_per_day = max(0, settings.URGENT_EMAIL_CALL_MAX_PER_DAY)
        self.urgent_email_min_score = min(1.0, max(0.0, settings.URGENT_EMAIL_MIN_SCORE))
        self.auto_task_min_score = min(1.0, max(0.0, settings.AUTO_TASK_EMAIL_MIN_SCORE))

    async def run_sweep(
        self,
        *,
        now: datetime | None = None,
    ) -> EmailAutomationRunSummary:
        """Process all opted-in Gmail-connected users."""
        summary = EmailAutomationRunSummary()
        if not self.email_automation_enabled:
            summary.skipped.append("email_automation_disabled")
            return summary

        users = await self._list_eligible_users()
        for user in users:
            user_summary = await self.process_user(user, now=now)
            summary.users_scanned += user_summary.users_scanned
            summary.emails_scanned += user_summary.emails_scanned
            summary.urgent_calls_scheduled += user_summary.urgent_calls_scheduled
            summary.tasks_created += user_summary.tasks_created
            summary.skipped.extend(user_summary.skipped)
            summary.errors.extend(user_summary.errors)

        return summary

    async def process_user(
        self,
        user: User,
        *,
        now: datetime | None = None,
    ) -> EmailAutomationRunSummary:
        """Process one Gmail-connected user."""
        summary = EmailAutomationRunSummary(users_scanned=1)
        if not self.email_automation_enabled:
            summary.skipped.append("email_automation_disabled")
            return summary
        if not _has_gmail_connected(user):
            summary.skipped.append(f"user:{user.id}:gmail_not_connected")
            return summary

        wants_urgent = (
            self.urgent_calls_enabled and user.urgent_email_calls_enabled
        )
        wants_tasks = (
            self.auto_tasks_enabled and user.auto_task_from_emails_enabled
        )
        if not wants_urgent and not wants_tasks:
            summary.skipped.append(f"user:{user.id}:automation_not_opted_in")
            return summary

        search_result = await search_emails(
            user,
            self.session,
            query=_candidate_query(self.lookback_days),
            max_results=self.max_messages,
        )
        if isinstance(search_result, dict) and "error" in search_result:
            summary.errors.append(
                f"user:{user.id}:gmail_search:{search_result.get('error')}"
            )
            return summary

        for email_summary in search_result:
            if _is_no_reply_sender(email_summary.get("from", "")):
                summary.skipped.append(f"message:{email_summary.get('id')}:no_reply")
                continue

            full_email = await get_email_for_reply(
                user,
                self.session,
                message_id=email_summary["id"],
            )
            if "error" in full_email:
                summary.errors.append(
                    f"message:{email_summary['id']}:read:{full_email.get('error')}"
                )
                continue

            summary.emails_scanned += 1

            if wants_tasks:
                task_created = await self._maybe_create_task_from_email(
                    user=user,
                    email=full_email,
                )
                if task_created:
                    summary.tasks_created += 1

            if wants_urgent:
                call_scheduled = await self._maybe_schedule_urgent_email_call(
                    user=user,
                    email=full_email,
                    now=now,
                )
                if call_scheduled:
                    summary.urgent_calls_scheduled += 1

        return summary

    async def _maybe_create_task_from_email(
        self,
        *,
        user: User,
        email: dict,
    ) -> bool:
        user_id = _require_user_id(user)
        task_score = score_task_email(email)
        if task_score.score < self.auto_task_min_score:
            return False

        event = await self._claim_event(
            user_id=user_id,
            event_type=EmailAutomationEventType.AUTO_TASK,
            email=email,
            confidence=task_score.score,
            reason=", ".join(task_score.reasons),
        )
        if event is None:
            return False

        try:
            title = build_task_title_from_email(email)
            urgent_score = score_urgent_email(email).score
            priority = 90 if urgent_score >= self.urgent_email_min_score else 70
            task, _created = await TaskService(self.session).save_task(
                user_id=user_id,
                title=title,
                priority=priority,
                source="gmail",
            )
            event.task_id = task.id
            await self._complete_event(event, EmailAutomationStatus.CREATED)
            return True
        except Exception as exc:
            await self._complete_event(
                event,
                EmailAutomationStatus.FAILED,
                reason=f"task_create_failed:{type(exc).__name__}",
            )
            logger.exception(
                "Auto-task creation failed for user_id=%s message_id=%s",
                user_id,
                email.get("id"),
            )
            return False

    async def _maybe_schedule_urgent_email_call(
        self,
        *,
        user: User,
        email: dict,
        now: datetime | None,
    ) -> bool:
        user_id = _require_user_id(user)
        urgent_score = score_urgent_email(email)
        if urgent_score.score < self.urgent_email_min_score:
            return False

        now_utc = now or datetime.now(timezone.utc)
        eta = now_utc + timedelta(minutes=self.urgent_call_delay_minutes)
        if _is_in_quiet_hours(user, now_utc) or _is_in_quiet_hours(user, eta):
            return False
        if await self._urgent_call_rate_limited(user, now_utc):
            return False
        if await CallLogService(self.session).find_active_on_demand(user_id):
            return False

        event = await self._claim_event(
            user_id=user_id,
            event_type=EmailAutomationEventType.URGENT_CALL,
            email=email,
            confidence=urgent_score.score,
            reason=", ".join(urgent_score.reasons),
        )
        if event is None:
            return False

        try:
            local_tz = _user_zoneinfo(user)
            local_date = eta.astimezone(local_tz).date()
            subject = _clean_subject(email.get("subject", ""))
            sender = _sender_name(email.get("from", ""))
            call_log = CallLog(
                user_id=user_id,
                call_type=CallType.ON_DEMAND.value,
                call_date=local_date,
                scheduled_time=eta,
                scheduled_timezone=user.timezone or "UTC",
                status=CallLogStatus.SCHEDULED.value,
                occurrence_kind=OccurrenceKind.ON_DEMAND.value,
                goal=f"Handle urgent email from {sender}: {subject}",
                next_action="Decide the response or next action for this urgent email.",
                commitments=[
                    f"Gmail message: {email.get('id', '')}",
                    f"Gmail thread: {email.get('thread_id', '')}",
                ],
            )
            created = await CallLogService(self.session).create_call_log(call_log)
            event.call_log_id = created.id
            await self._complete_event(event, EmailAutomationStatus.CREATED)
            return True
        except Exception as exc:
            await self._complete_event(
                event,
                EmailAutomationStatus.FAILED,
                reason=f"call_schedule_failed:{type(exc).__name__}",
            )
            logger.exception(
                "Urgent email call scheduling failed for user_id=%s message_id=%s",
                user_id,
                email.get("id"),
            )
            return False

    async def _claim_event(
        self,
        *,
        user_id: int,
        event_type: EmailAutomationEventType,
        email: dict,
        confidence: float,
        reason: str,
    ) -> EmailAutomationEvent | None:
        """Insert a processing marker, returning None on thread dedupe hit."""
        event = EmailAutomationEvent(
            user_id=user_id,
            event_type=event_type.value,
            gmail_message_id=email["id"],
            gmail_thread_id=email["thread_id"],
            status=EmailAutomationStatus.PROCESSING.value,
            confidence=confidence,
            reason=reason[:512] if reason else None,
        )
        self.session.add(event)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None

        await self.session.refresh(event)
        return event

    async def _complete_event(
        self,
        event: EmailAutomationEvent,
        status: EmailAutomationStatus,
        *,
        reason: str | None = None,
    ) -> None:
        """Mark an event terminal after its side effect is done."""
        event.status = status.value
        if reason:
            event.reason = reason[:512]
        event.completed_at = datetime.now(timezone.utc)
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)

    async def _urgent_call_rate_limited(self, user: User, now_utc: datetime) -> bool:
        """Return True if urgent calls are blocked by cooldown or daily cap."""
        user_id = _require_user_id(user)
        if self.urgent_call_max_per_day == 0:
            return True

        cooldown_start = now_utc - timedelta(
            minutes=self.urgent_call_cooldown_minutes
        )
        recent = await self.session.exec(
            select(EmailAutomationEvent).where(
                EmailAutomationEvent.user_id == user_id,
                EmailAutomationEvent.event_type
                == EmailAutomationEventType.URGENT_CALL.value,
                EmailAutomationEvent.status == EmailAutomationStatus.CREATED.value,
                EmailAutomationEvent.created_at >= cooldown_start,
            )
        )
        if recent.first() is not None:
            return True

        start_utc, end_utc = _local_day_bounds(user, now_utc)
        today_events = await self.session.exec(
            select(EmailAutomationEvent).where(
                EmailAutomationEvent.user_id == user_id,
                EmailAutomationEvent.event_type
                == EmailAutomationEventType.URGENT_CALL.value,
                EmailAutomationEvent.status == EmailAutomationStatus.CREATED.value,
                EmailAutomationEvent.created_at >= start_utc,
                EmailAutomationEvent.created_at < end_utc,
            )
        )
        return len(list(today_events.all())) >= self.urgent_call_max_per_day

    async def _list_eligible_users(self) -> list[User]:
        result = await self.session.exec(
            select(User).where(
                User.google_refresh_token_encrypted.is_not(None),  # type: ignore[union-attr]
                User.google_granted_scopes.contains("gmail.modify"),  # type: ignore[union-attr]
                or_(
                    User.urgent_email_calls_enabled == True,  # noqa: E712
                    User.auto_task_from_emails_enabled == True,  # noqa: E712
                ),
            )
        )
        return list(result.all())


def _has_gmail_connected(user: User) -> bool:
    scopes = (user.google_granted_scopes or "").split()
    return bool(user.google_refresh_token_encrypted) and any(
        "gmail.modify" in scope for scope in scopes
    )


def _require_user_id(user: User) -> int:
    if user.id is None:
        raise ValueError("User must be persisted before email automation runs.")
    return user.id


def _candidate_query(lookback_days: int) -> str:
    return (
        "in:inbox is:unread -from:me "
        "-category:promotions -category:social "
        f"newer_than:{lookback_days}d"
    )


def score_urgent_email(email: dict) -> EmailScore:
    """Score whether an email is urgent enough to trigger a call."""
    if _is_no_reply_sender(email.get("from", "")):
        return EmailScore(0.0, ("no_reply_sender",))
    return _score_terms(email, _URGENT_TERMS)


def score_task_email(email: dict) -> EmailScore:
    """Score whether an email should become a task."""
    if _is_no_reply_sender(email.get("from", "")):
        return EmailScore(0.0, ("no_reply_sender",))
    return _score_terms(email, _TASK_TERMS)


def build_task_title_from_email(email: dict) -> str:
    """Build a concise task title from a Gmail message."""
    subject = _clean_subject(email.get("subject", "")) or "(no subject)"
    sender = _sender_name(email.get("from", ""))
    combined = f"{subject}\n{email.get('body', '')}".lower()
    if any(term in combined for term in ("reply", "respond", "please respond")):
        title = f"Reply to {sender}: {subject}"
    else:
        title = f"Handle email from {sender}: {subject}"
    return title[:180].rstrip()


def _score_terms(email: dict, terms: dict[str, float]) -> EmailScore:
    subject = _clean_subject(email.get("subject", ""))
    body = email.get("body", "") or ""
    snippet = email.get("snippet", "") or ""
    subject_lower = subject.lower()
    body_lower = f"{body}\n{snippet}".lower()

    score = 0.0
    reasons: list[str] = []
    for term, weight in terms.items():
        if term in subject_lower:
            score += weight * 1.35
            reasons.append(f"subject:{term}")
        elif term in body_lower:
            score += weight
            reasons.append(f"body:{term}")

    if "?" in body_lower and score > 0:
        score += 0.08
        reasons.append("body:question")

    return EmailScore(min(1.0, score), tuple(reasons))


def _clean_subject(subject: str) -> str:
    clean = " ".join((subject or "").split())
    while clean.lower().startswith(("re:", "fw:", "fwd:")):
        clean = clean.split(":", 1)[1].strip()
    return clean


def _sender_name(from_header: str) -> str:
    name = (from_header or "Unknown").split("<", 1)[0].strip().strip('"')
    return name or from_header or "Unknown"


def _user_zoneinfo(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except (KeyError, Exception):
        return ZoneInfo("UTC")


def _is_in_quiet_hours(user: User, now_utc: datetime) -> bool:
    tz = _user_zoneinfo(user)
    local_t = now_utc.astimezone(tz).time()
    start = user.email_automation_quiet_hours_start or time(21, 0)
    end = user.email_automation_quiet_hours_end or time(8, 0)
    if start == end:
        return False
    if start < end:
        return start <= local_t < end
    return local_t >= start or local_t < end


def _local_day_bounds(user: User, now_utc: datetime) -> tuple[datetime, datetime]:
    tz = _user_zoneinfo(user)
    local_now = now_utc.astimezone(tz)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
