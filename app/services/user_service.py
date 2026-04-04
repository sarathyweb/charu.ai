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


class UserService:
    """Handles user lookup, creation, and cross-channel identity linking."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_phone(self, phone: str) -> User | None:
        """Return the user with the given phone, or ``None``."""
        result = await self.session.exec(select(User).where(User.phone == phone))
        return result.first()

    async def ensure_from_firebase(self, phone: str, firebase_uid: str) -> User:
        """Get-or-create a user from a verified Firebase login.

        * New phone → create with phone + firebase_uid + last_login_at.
        * Existing user, firebase_uid is None → link UID, update last_login_at.
        * Existing user, same UID → update last_login_at.
        * Existing user, *different* UID → log security event, raise HTTP 409.

        Uses IntegrityError handling for race-condition safety.
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
                return user
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
                return user

        if user.firebase_uid == firebase_uid:
            # Same UID — just update last_login_at
            user.last_login_at = now
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
            return user

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

    async def ensure_from_whatsapp(self, phone: str) -> User:
        """Get-or-create a user from an inbound WhatsApp message.

        * New phone → create with phone, firebase_uid=None.
        * Existing phone → return existing (no UID changes from WhatsApp).

        Uses IntegrityError handling for race-condition safety.
        """
        phone = normalize_phone(phone)

        user = await self.get_by_phone(phone)
        if user is not None:
            return user

        user = User(phone=phone, firebase_uid=None)
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
            return user
