"""Unit tests for cleanup task side effects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType, OccurrenceKind
from app.models.user import User
from app.tasks import cleanup


class SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_cleanup_old_transcripts_deletes_local_file_and_clears_reference(
    session,
    tmp_path,
    monkeypatch,
):
    user = User(phone="+15559998888", timezone="UTC", onboarding_complete=True)
    session.add(user)
    await session.commit()
    await session.refresh(user)

    old_file = tmp_path / "old_transcript.json"
    old_file.write_text("{}", encoding="utf-8")
    recent_file = tmp_path / "recent_transcript.json"
    recent_file.write_text("{}", encoding="utf-8")

    old_log = CallLog(
        user_id=user.id,
        call_type=CallType.MORNING.value,
        call_date=datetime.now(timezone.utc).date(),
        scheduled_time=datetime.now(timezone.utc),
        scheduled_timezone="UTC",
        status=CallLogStatus.COMPLETED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        transcript_filename=old_file.name,
    )
    old_log.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    recent_log = CallLog(
        user_id=user.id,
        call_type=CallType.AFTERNOON.value,
        call_date=datetime.now(timezone.utc).date(),
        scheduled_time=datetime.now(timezone.utc),
        scheduled_timezone="UTC",
        status=CallLogStatus.COMPLETED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        transcript_filename=recent_file.name,
    )
    recent_log.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.add(old_log)
    session.add(recent_log)
    await session.commit()
    await session.refresh(old_log)
    await session.refresh(recent_log)

    monkeypatch.setattr(cleanup, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path))

    result = await cleanup._run_cleanup_old_transcripts()

    assert "cleared 1 transcript reference" in result
    assert not old_file.exists()
    assert recent_file.exists()
    await session.refresh(old_log)
    await session.refresh(recent_log)
    assert old_log.transcript_filename is None
    assert recent_log.transcript_filename == recent_file.name
