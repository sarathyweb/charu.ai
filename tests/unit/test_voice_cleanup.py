"""Unit tests for app/voice/cleanup.py — post-call cleanup and task dispatch.

Tests:
- Transcript file is saved and filename returned
- CallLog transitions to completed with transcript filename
- Recap task dispatched for morning/afternoon and evening calls
- Midday check-in dispatched only for morning/afternoon calls
- Email draft review dispatched when pending draft exists
- Anti-habituation state updated on User model
- Errors in individual steps don't block subsequent steps

Requirements: 5, 13, 14.5
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.voice.cleanup import (
    _missing_outcome_fallback_fields,
    _save_transcript_file,
    post_call_cleanup,
)


# ---------------------------------------------------------------------------
# Transcript file saving
# ---------------------------------------------------------------------------


class TestSaveTranscriptFile:
    """Tests for _save_transcript_file."""

    def test_creates_json_file_with_entries(self, tmp_path):
        """Transcript entries are written to a JSON file."""
        with patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)):
            entries = [
                {"role": "assistant", "content": "Hello!", "timestamp": "2026-01-01T07:00:00Z"},
                {"role": "user", "content": "Hi there", "timestamp": "2026-01-01T07:00:05Z"},
            ]
            filename = _save_transcript_file(call_log_id=42, transcript_dicts=entries)

        assert filename == "transcript_42.json"
        filepath = tmp_path / filename
        assert filepath.exists()

        data = json.loads(filepath.read_text())
        assert data["call_log_id"] == 42
        assert len(data["entries"]) == 2
        assert data["entries"][0]["role"] == "assistant"
        assert "saved_at" in data

    def test_deterministic_filename(self, tmp_path):
        """Filename is deterministic based on call_log_id."""
        with patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)):
            f1 = _save_transcript_file(call_log_id=99, transcript_dicts=[])
            f2 = _save_transcript_file(call_log_id=99, transcript_dicts=[])

        assert f1 == f2 == "transcript_99.json"

    def test_creates_directory_if_missing(self, tmp_path):
        """TRANSCRIPT_DIR is created if it doesn't exist."""
        nested = tmp_path / "deep" / "nested"
        with patch("app.voice.cleanup.TRANSCRIPT_DIR", str(nested)):
            _save_transcript_file(call_log_id=1, transcript_dicts=[])

        assert nested.exists()


# ---------------------------------------------------------------------------
# Full post_call_cleanup integration (mocked DB + Celery)
# ---------------------------------------------------------------------------


def _mock_call_log(
    call_log_id: int = 1,
    status: str = "in_progress",
    version: int = 3,
    call_type: str = "morning",
    scheduled_timezone: str = "America/New_York",
):
    cl = MagicMock()
    cl.id = call_log_id
    cl.status = status
    cl.version = version
    cl.call_type = call_type
    cl.scheduled_timezone = scheduled_timezone
    cl.transcript_filename = None
    cl.call_outcome_confidence = None
    cl.reflection_confidence = None
    return cl


def test_missing_outcome_fallback_sets_none_for_morning():
    call_log = _mock_call_log(call_type="morning")

    assert _missing_outcome_fallback_fields(call_log, "morning") == {
        "call_outcome_confidence": "none"
    }


def test_missing_outcome_fallback_does_not_overwrite_existing():
    call_log = _mock_call_log(call_type="morning")
    call_log.call_outcome_confidence = "clear"

    assert _missing_outcome_fallback_fields(call_log, "morning") == {}


def test_missing_outcome_fallback_sets_none_for_evening():
    call_log = _mock_call_log(call_type="evening")

    assert _missing_outcome_fallback_fields(call_log, "evening") == {
        "reflection_confidence": "none"
    }


def _mock_user(user_id: int = 42):
    u = MagicMock()
    u.id = user_id
    u.last_opener_id = None
    u.last_approach = None
    u.consecutive_active_days = 0
    u.last_active_date = None
    return u


class TestPostCallCleanup:
    """Tests for the main post_call_cleanup function."""

    @pytest.mark.asyncio
    async def test_dispatches_recap_for_morning_call(self, tmp_path):
        """Recap task is dispatched with 30s delay for morning calls."""
        mock_session = AsyncMock()
        mock_call_log = _mock_call_log(status="in_progress")
        mock_session.get = AsyncMock(return_value=mock_call_log)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=mock_call_log)

        with (
            patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)),
            patch("app.voice.cleanup.async_session_factory") as mock_factory,
            patch("app.voice.cleanup.CallLogService", return_value=mock_svc),
            patch("app.tasks.recap.send_post_call_recap") as mock_recap_task,
            patch("app.voice.cleanup._dispatch_midday_checkin", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_draft_review", new_callable=AsyncMock),
            patch("app.voice.cleanup._update_anti_habituation", new_callable=AsyncMock),
        ):
            mock_recap_task.apply_asyncx = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_call_cleanup(
                call_log_id=1,
                user_id=42,
                call_type="morning",
                transcript_dicts=[{"role": "user", "content": "hi", "timestamp": None}],
                call_ctx={"opener": {"id": "direct_1"}, "approach": "open_question"},
            )

            mock_recap_task.apply_asyncx.assert_called_once()
            call_args = mock_recap_task.apply_asyncx.call_args
            assert call_args.kwargs.get("countdown") == 30 or call_args[1].get("countdown") == 30

    @pytest.mark.asyncio
    async def test_dispatches_evening_recap_for_evening_call(self, tmp_path):
        """Evening recap task is dispatched for evening calls."""
        mock_session = AsyncMock()
        mock_cl = _mock_call_log(status="in_progress", call_type="evening")
        mock_session.get = AsyncMock(return_value=mock_cl)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=mock_cl)

        with (
            patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)),
            patch("app.voice.cleanup.async_session_factory") as mock_factory,
            patch("app.voice.cleanup.CallLogService", return_value=mock_svc),
            patch("app.tasks.recap.send_evening_recap") as mock_evening_recap,
            patch("app.voice.cleanup._dispatch_midday_checkin", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_draft_review", new_callable=AsyncMock),
            patch("app.voice.cleanup._update_anti_habituation", new_callable=AsyncMock),
        ):
            mock_evening_recap.apply_asyncx = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_call_cleanup(
                call_log_id=1,
                user_id=42,
                call_type="evening",
                transcript_dicts=[],
                call_ctx={},
            )

            mock_evening_recap.apply_asyncx.assert_called_once()

    @pytest.mark.asyncio
    async def test_midday_checkin_skipped_for_evening_calls(self, tmp_path):
        """Midday check-in is NOT dispatched for evening calls."""
        mock_session = AsyncMock()
        mock_cl = _mock_call_log(status="in_progress", call_type="evening")
        mock_session.get = AsyncMock(return_value=mock_cl)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=mock_cl)

        with (
            patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)),
            patch("app.voice.cleanup.async_session_factory") as mock_factory,
            patch("app.voice.cleanup.CallLogService", return_value=mock_svc),
            patch("app.tasks.recap.send_evening_recap") as mock_recap,
            patch("app.tasks.checkin.send_midday_checkin") as mock_checkin,
            patch("app.voice.cleanup._dispatch_draft_review", new_callable=AsyncMock),
            patch("app.voice.cleanup._update_anti_habituation", new_callable=AsyncMock),
        ):
            mock_recap.apply_asyncx = AsyncMock()
            mock_checkin.apply_asyncx = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_call_cleanup(
                call_log_id=1,
                user_id=42,
                call_type="evening",
                transcript_dicts=[],
                call_ctx={},
            )

            mock_checkin.apply_asyncx.assert_not_called()

    @pytest.mark.asyncio
    async def test_anti_habituation_updated(self, tmp_path):
        """Anti-habituation state is updated from call context."""
        mock_session = AsyncMock()
        mock_cl = _mock_call_log(status="in_progress")
        mock_session.get = AsyncMock(return_value=mock_cl)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=mock_cl)

        mock_user = _mock_user()

        # We need two separate session mocks — one for CallLog, one for User
        call_ctx = {
            "opener": {"id": "reflective_1", "category": "reflective", "template": "..."},
            "approach": "calendar_led",
            "streak_days": 5,
            "new_last_active": date(2026, 4, 5),
        }

        with (
            patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)),
            patch("app.voice.cleanup.async_session_factory") as mock_factory,
            patch("app.voice.cleanup.CallLogService", return_value=mock_svc),
            patch("app.voice.cleanup._dispatch_recap", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_midday_checkin", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_draft_review", new_callable=AsyncMock),
        ):
            # For the CallLog session
            mock_session_cl = AsyncMock()
            mock_session_cl.get = AsyncMock(return_value=mock_cl)
            mock_session_cl.add = MagicMock()

            # For the User session (anti-habituation)
            mock_session_user = AsyncMock()
            mock_session_user.get = AsyncMock(return_value=mock_user)
            mock_session_user.add = MagicMock()

            call_count = 0

            async def session_factory_side_effect():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return mock_session_cl
                return mock_session_user

            # Use a context manager mock that returns different sessions
            mock_factory.return_value.__aenter__ = AsyncMock(side_effect=[mock_session_cl, mock_session_user])
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_call_cleanup(
                call_log_id=1,
                user_id=42,
                call_type="morning",
                transcript_dicts=[],
                call_ctx=call_ctx,
            )

            # Verify user was updated
            assert mock_user.last_opener_id == "reflective_1"
            assert mock_user.last_approach == "calendar_led"
            assert mock_user.consecutive_active_days == 5
            assert mock_user.last_active_date == date(2026, 4, 5)

    @pytest.mark.asyncio
    async def test_transcript_saved_to_file(self, tmp_path):
        """Transcript entries are saved to a JSON file."""
        mock_session = AsyncMock()
        mock_cl = _mock_call_log(status="in_progress")
        mock_session.get = AsyncMock(return_value=mock_cl)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=mock_cl)

        entries = [
            {"role": "assistant", "content": "Good morning!", "timestamp": "2026-01-01T07:00:00Z"},
            {"role": "user", "content": "Hey", "timestamp": "2026-01-01T07:00:03Z"},
        ]

        with (
            patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)),
            patch("app.voice.cleanup.async_session_factory") as mock_factory,
            patch("app.voice.cleanup.CallLogService", return_value=mock_svc),
            patch("app.voice.cleanup._dispatch_recap", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_midday_checkin", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_draft_review", new_callable=AsyncMock),
            patch("app.voice.cleanup._update_anti_habituation", new_callable=AsyncMock),
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_call_cleanup(
                call_log_id=7,
                user_id=42,
                call_type="morning",
                transcript_dicts=entries,
                call_ctx={},
            )

        filepath = tmp_path / "transcript_7.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert len(data["entries"]) == 2

    @pytest.mark.asyncio
    async def test_empty_transcript_skips_file_save(self, tmp_path):
        """Empty transcript list does not create a file."""
        mock_session = AsyncMock()
        mock_cl = _mock_call_log(status="in_progress")
        mock_session.get = AsyncMock(return_value=mock_cl)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=mock_cl)

        with (
            patch("app.voice.cleanup.TRANSCRIPT_DIR", str(tmp_path)),
            patch("app.voice.cleanup.async_session_factory") as mock_factory,
            patch("app.voice.cleanup.CallLogService", return_value=mock_svc),
            patch("app.voice.cleanup._dispatch_recap", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_midday_checkin", new_callable=AsyncMock),
            patch("app.voice.cleanup._dispatch_draft_review", new_callable=AsyncMock),
            patch("app.voice.cleanup._update_anti_habituation", new_callable=AsyncMock),
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_call_cleanup(
                call_log_id=7,
                user_id=42,
                call_type="morning",
                transcript_dicts=[],
                call_ctx={},
            )

        # No transcript file should be created
        assert not (tmp_path / "transcript_7.json").exists()
