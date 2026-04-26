"""UserService — channel-aware user CRUD and identity resolution."""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

# Fields that update_preferences is allowed to modify.
# Sensitive fields (OAuth tokens, firebase_uid) are excluded.
_ALLOWED_PREFERENCE_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "timezone",
        "onboarding_complete",
        "urgent_email_calls_enabled",
        "auto_task_from_emails_enabled",
        "email_automation_quiet_hours_start",
        "email_automation_quiet_hours_end",
    }
)


class UserService:
    """Handles user lookup, creation, and cross-channel identity linking."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_phone(self, phone: str) -> User | None:
        """Return the user with the given phone, or ``None``."""
        result = await self.session.exec(select(User).where(User.phone == phone))
        return result.first()

    async def ensure_from_firebase(
        self, phone: str, firebase_uid: str
    ) -> tuple[User, bool]:
        """Get-or-create a user from a verified Firebase login.

        * New phone → create with phone + firebase_uid + last_login_at.
        * Existing user, firebase_uid is None → link UID, update last_login_at.
        * Existing user, same UID → update last_login_at.
        * Existing user, *different* UID → log security event, raise HTTP 409.

        Uses IntegrityError handling for race-condition safety.

        Returns:
            A tuple of (user, created) where *created* is True only when this
            call actually inserted a new row.
        """
        phone = normalize_phone(phone)
        now = datetime.now(timezone.utc)

        user = await self.get_by_phone(phone)

        if user is None:
            # New user — create
            user = User(phone=phone, firebase_uid=firebase_uid, last_login_at=now)
            self.session.add(user)
            try:
                await self.session.commit()
                await self.session.refresh(user)
                return user, True
            except IntegrityError:
                await self.session.rollback()
                # Race: another request created the user first — retry lookup
                user = await self.get_by_phone(phone)
                if user is None:
                    # Conflict was NOT on phone — must be firebase_uid
                    # (another phone already owns this UID)
                    logger.warning(
                        "Security: firebase_uid %s already linked to another phone "
                        "(attempted phone %s)",
                        firebase_uid,
                        phone,
                    )
                    raise HTTPException(
                        status_code=409,
                        detail="Phone linked to different account",
                    )

        # User exists — check UID scenarios
        if user.firebase_uid is None:
            # WhatsApp-only user → link Firebase UID atomically.
            # Use UPDATE ... WHERE firebase_uid IS NULL so only one concurrent
            # writer wins; the loser gets rowcount=0 and re-reads.
            from sqlalchemy import update

            try:
                stmt = (
                    update(User)
                    .where(User.phone == phone, User.firebase_uid.is_(None))
                    .values(firebase_uid=firebase_uid, last_login_at=now)
                )
                result = await self.session.exec(stmt)
                await self.session.commit()
            except IntegrityError:
                await self.session.rollback()
                # UID uniqueness conflict — another user already has this UID
                logger.warning(
                    "Security: firebase_uid %s already linked to another account "
                    "(attempted phone %s)",
                    firebase_uid,
                    phone,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Phone linked to different account",
                )

            if result.rowcount == 0:  # type: ignore[union-attr]
                # Another session linked a UID first — expire stale cache
                # so re-read fetches the actual firebase_uid from DB.
                self.session.expire_all()
                user = await self.get_by_phone(phone)
                if user is None:
                    raise HTTPException(status_code=500, detail="Unexpected state")
                # Fall through to the UID comparison below
            else:
                # We won the race — refresh and return
                self.session.expire_all()
                user = await self.get_by_phone(phone)
                return user, False

        if user.firebase_uid == firebase_uid:
            # Same UID — just update last_login_at
            user.last_login_at = now
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
            return user, False

        # Different UID — security event
        logger.warning(
            "Security: phone %s already linked to firebase_uid %s, "
            "but login attempted with firebase_uid %s",
            phone,
            user.firebase_uid,
            firebase_uid,
        )
        raise HTTPException(
            status_code=409,
            detail="Phone linked to different account",
        )

    async def get_or_create_by_phone(self, phone: str) -> User:
        """Get-or-create a user by phone number.

        Simple variant without channel-specific logic — used by tools and
        services that just need a user record to exist.

        Uses IntegrityError handling for race-condition safety.
        """
        phone = normalize_phone(phone)

        user = await self.get_by_phone(phone)
        if user is not None:
            return user

        user = User(phone=phone)
        self.session.add(user)
        try:
            await self.session.commit()
            await self.session.refresh(user)
            return user
        except IntegrityError:
            await self.session.rollback()
            user = await self.get_by_phone(phone)
            if user is None:
                raise  # pragma: no cover — unexpected
            return user

    async def update_preferences(self, phone: str, **kwargs: object) -> User:
        """Partial update of user preferences (write-through pattern).

        Only fields listed in ``_ALLOWED_PREFERENCE_FIELDS`` may be updated.
        Sensitive fields (OAuth tokens, firebase_uid) are rejected.

        The method is idempotent — setting a field to its current value is a
        no-op (no DB write).

        Returns:
            The (possibly updated) ``User`` object.

        Raises:
            ValueError: If *phone* does not match an existing user or if an
                invalid field name is supplied.
        """
        phone = normalize_phone(phone)

        invalid = set(kwargs) - _ALLOWED_PREFERENCE_FIELDS
        if invalid:
            raise ValueError(
                f"Cannot update restricted/unknown fields: {', '.join(sorted(invalid))}"
            )

        # Validate timezone if provided
        if "timezone" in kwargs and kwargs["timezone"] is not None:
            from zoneinfo import available_timezones

            tz_value = kwargs["timezone"]
            if tz_value not in available_timezones():
                raise ValueError(
                    f"Invalid timezone: {tz_value!r}. "
                    "Use an IANA identifier like America/New_York."
                )

        user = await self.get_by_phone(phone)
        if user is None:
            raise ValueError(f"No user found for phone {phone}")

        # Only write if at least one value actually changed.
        changed = False
        for field, value in kwargs.items():
            if getattr(user, field) != value:
                setattr(user, field, value)
                changed = True

        if changed:
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)

        return user

    async def ensure_from_whatsapp(self, phone: str) -> User:
        """Get-or-create a user from an inbound WhatsApp message.

        * New phone → create with phone, firebase_uid=None.
        * Existing phone → return existing (no UID changes from WhatsApp).

        Always updates ``last_user_whatsapp_message_at`` to open/extend
        the 24-hour customer service window.

        Uses IntegrityError handling for race-condition safety.
        """
        phone = normalize_phone(phone)
        now = datetime.now(timezone.utc)

        user = await self.get_by_phone(phone)
        if user is not None:
            user.last_user_whatsapp_message_at = now
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
            return user

        user = User(
            phone=phone,
            firebase_uid=None,
            last_user_whatsapp_message_at=now,
        )
        self.session.add(user)
        try:
            await self.session.commit()
            await self.session.refresh(user)
            return user
        except IntegrityError:
            await self.session.rollback()
            # Race: another request created the user first — return it
            user = await self.get_by_phone(phone)
            if user is None:
                raise  # pragma: no cover — unexpected
            # Still update the timestamp for the winner
            user.last_user_whatsapp_message_at = now
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
            return user


# ---------------------------------------------------------------------------
# Session hydration helper
# ---------------------------------------------------------------------------


async def hydrate_session_state(
    phone: str,
    session: AsyncSession,
) -> dict[str, object]:
    """Build a ``user:``-prefixed state dict from the DB for *phone*.

    Used when creating a new ADK session so that the agent has immediate
    access to all persisted user preferences.  Call-window times are
    included when the ``call_windows`` table has rows for the user.

    Returns a dict suitable for merging into the ADK session ``state``.
    The ``phone`` key (unprefixed) is always included.
    """
    from app.models.call_window import CallWindow  # local import to avoid circular

    svc = UserService(session)
    user = await svc.get_by_phone(phone)

    state: dict[str, object] = {"phone": phone}
    if user is None:
        return state

    # Core preferences
    if user.name:
        state["user:name"] = user.name
    if user.timezone:
        state["user:timezone"] = user.timezone
    state["user:onboarding_complete"] = user.onboarding_complete

    # Google connection flags — derived from granted scopes
    scopes = (user.google_granted_scopes or "").split()
    state["user:google_calendar_connected"] = any("calendar" in s for s in scopes)
    state["user:google_gmail_connected"] = any("gmail.modify" in s for s in scopes)

    # Call windows
    result = await session.exec(
        select(CallWindow).where(
            CallWindow.user_id == user.id,
            CallWindow.is_active == True,  # noqa: E712
        )
    )
    for window in result.all():
        wt = window.window_type  # e.g. "morning"
        state[f"user:{wt}_call_start"] = window.start_time.strftime("%H:%M")
        state[f"user:{wt}_call_end"] = window.end_time.strftime("%H:%M")

    return state
