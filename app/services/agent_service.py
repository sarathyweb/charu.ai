"""AgentService — ADK Runner wrapper with session resolution."""

import logging
import time
import uuid
from datetime import datetime, timezone

from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai.types import Content, Part
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.current_session import CurrentSession
from app.models.schemas import AgentRunResult
from app.services.user_service import hydrate_session_state
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

APP_NAME = "productivity_assistant"


class AgentService:
    """Wraps the ADK Runner with O(1) session resolution by phone number."""

    def __init__(
        self,
        runner: Runner,
        session_service: DatabaseSessionService,
        session: AsyncSession,
    ) -> None:
        self.runner = runner
        self.session_service = session_service
        self.session = session

    async def run(self, user_id: str, message: str, channel: str) -> AgentRunResult:
        """Route a user message through the ADK agent and return the reply.

        Args:
            user_id: Raw phone number (will be normalised to E.164).
            message: The user's message text.
            channel: Origin channel — ``"web"`` or ``"whatsapp"``.

        Returns:
            AgentRunResult with the agent's reply text and session id.
        """
        phone = normalize_phone(user_id)
        session_id = await self._resolve_session(phone)

        # Build the user message in ADK Content format
        user_content = Content(
            parts=[Part(text=message)],
            role="user",
        )

        # Stream events from the runner and collect the final response text.
        # When multiple agents participate (e.g. root → onboarding), each
        # emits its own is_final_response() event.  We collect ALL non-empty
        # text parts so that both the onboarding summary and any follow-up
        # from the parent agent are included.  Empty/whitespace parts from
        # skip callbacks and transfer markers are filtered out.
        reply_parts: list[str] = []
        async for event in self.runner.run_async(
            user_id=phone,
            session_id=session_id,
            new_message=user_content,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text and part.text.strip():
                            reply_parts.append(part.text)

        # Update the timestamp on the current_sessions mapping
        mapping = (
            await self.session.exec(
                select(CurrentSession).where(CurrentSession.phone == phone)
            )
        ).first()
        if mapping:
            mapping.updated_at = datetime.now(timezone.utc)
            self.session.add(mapping)
            await self.session.commit()

        collected_text = "\n".join(reply_parts) if reply_parts else ""
        if not collected_text:
            logger.warning(
                "ADK agent returned no text for phone=%s session=%s message=%.100s",
                phone,
                session_id,
                message,
            )
        return AgentRunResult(reply=collected_text, session_id=session_id)

    async def _resolve_session(self, phone: str) -> str:
        """Look up or create the active ADK session for *phone*.

        Uses the ``current_sessions`` table for O(1) lookup.  On first
        contact a new ADK session is created and the mapping is stored
        with IntegrityError handling for race-condition safety.
        """
        # 1. Check the mapping table
        mapping = (
            await self.session.exec(
                select(CurrentSession).where(CurrentSession.phone == phone)
            )
        ).first()

        if mapping is not None:
            # Verify the ADK session still exists (heal stale mappings)
            adk_session = await self.session_service.get_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=mapping.session_id,
            )
            if adk_session is not None:
                # Re-hydrate: compute fresh state from DB and patch any
                # changes into the live session via append_event so that
                # preference edits (name, timezone, etc.) are visible
                # immediately without requiring a new session.
                await self._sync_state_from_db(phone, adk_session)
                return mapping.session_id

            # Stale mapping — ADK session was deleted; remove and recreate
            logger.warning(
                "Stale session mapping for %s (session %s deleted); recreating",
                phone,
                mapping.session_id,
            )
            await self.session.delete(mapping)
            await self.session.commit()

        # 2. No mapping — create a new ADK session with hydrated state
        state = await hydrate_session_state(phone, self.session)
        new_session_id = str(uuid.uuid4())
        adk_session = await self.session_service.create_session(
            app_name=APP_NAME,
            user_id=phone,
            session_id=new_session_id,
            state=state,
        )
        session_id = adk_session.id

        # 3. Persist the mapping (IntegrityError handles races)
        mapping = CurrentSession(
            phone=phone,
            session_id=session_id,
            updated_at=datetime.now(timezone.utc),
        )
        self.session.add(mapping)
        try:
            await self.session.commit()
            return session_id
        except IntegrityError:
            # Another request created the mapping first — use theirs
            await self.session.rollback()
            existing = (
                await self.session.exec(
                    select(CurrentSession).where(CurrentSession.phone == phone)
                )
            ).first()

            # Clean up the orphaned ADK session we just created
            try:
                await self.session_service.delete_session(
                    app_name=APP_NAME,
                    user_id=phone,
                    session_id=session_id,
                )
            except Exception:
                logger.warning(
                    "Failed to delete orphaned ADK session %s for %s",
                    session_id,
                    phone,
                )

            return existing.session_id if existing else session_id

    async def _sync_state_from_db(self, phone: str, adk_session: object) -> None:
        """Re-hydrate ADK session state from DB, patching any drift.

        Computes the canonical ``user:``-prefixed state from the database
        and compares it to the live session state.  If any keys differ,
        an ``Event`` with a ``state_delta`` is appended so the change is
        tracked and persisted by the ``DatabaseSessionService``.

        This ensures that preference edits made outside the agent (e.g.
        via ``update_preferences``) are reflected in the next agent run
        without requiring a brand-new session.
        """
        fresh = await hydrate_session_state(phone, self.session)
        current = adk_session.state or {}

        # Compute delta: only keys whose values actually changed
        delta: dict[str, object] = {}
        for key, value in fresh.items():
            if current.get(key) != value:
                delta[key] = value

        # Also detect keys that were removed (e.g. name set to None)
        # We represent removal by setting the key to None in the delta.
        for key in current:
            if key.startswith("user:") and key not in fresh:
                delta[key] = None

        if not delta:
            return  # Nothing changed — skip the append

        logger.debug(
            "Syncing %d state key(s) from DB for %s: %s",
            len(delta),
            phone,
            list(delta.keys()),
        )

        sync_event = Event(
            invocation_id=f"state_sync_{uuid.uuid4().hex[:8]}",
            author="system",
            actions=EventActions(state_delta=delta),
            timestamp=time.time(),
        )
        await self.session_service.append_event(adk_session, sync_event)
