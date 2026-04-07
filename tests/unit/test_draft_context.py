"""Unit tests for app.services.draft_context — intent classification."""

import pytest

from app.services.draft_context import DraftIntent, classify_draft_intent


class TestClassifyDraftIntent:
    """Tests for classify_draft_intent signal detection."""

    # --- Approval signals ---

    @pytest.mark.parametrize(
        "body",
        [
            "send it",
            "Send it",
            "SEND IT",
            "send",
            "yes",
            "Yes",
            "YES",
            "yep",
            "yeah",
            "looks good",
            "Looks good",
            "look good",
            "go ahead",
            "Go ahead",
            "approve",
            "Approve",
            "perfect",
            "Perfect",
            "lgtm",
            "LGTM",
            "ok",
            "OK",
            "okay",
            "sure",
            "do it",
            "Do it",
            "ship it",
            "👍",
            "✅",
        ],
    )
    def test_approval_signals(self, body: str) -> None:
        assert classify_draft_intent(body) == DraftIntent.APPROVE

    # --- Abandonment signals ---

    @pytest.mark.parametrize(
        "body",
        [
            "cancel",
            "Cancel",
            "never mind",
            "nevermind",
            "skip",
            "skip it",
            "skip this",
            "don't send",
            "dont send",
            "forget it",
            "forget about it",
            "nah",
            "no",
            "No",
            "no thanks",
            "no, don't",
            "nah forget it",
            "abandon",
            "drop it",
            "drop",
            "❌",
        ],
    )
    def test_abandon_signals(self, body: str) -> None:
        assert classify_draft_intent(body) == DraftIntent.ABANDON

    # --- Revision signals (anything else) ---

    @pytest.mark.parametrize(
        "body",
        [
            "make it shorter",
            "change the tone to more casual",
            "add a greeting at the top",
            "remove the last paragraph",
            "too formal, make it friendlier",
            "can you mention the deadline?",
            "I want to say thanks first",
            "rewrite the second sentence",
            "no, make it shorter",
            "nah, mention the deadline",
            "no make it more formal",
            "nah use a different greeting",
        ],
    )
    def test_revision_signals(self, body: str) -> None:
        assert classify_draft_intent(body) == DraftIntent.REVISE

    # --- Edge cases ---

    def test_whitespace_stripped(self) -> None:
        assert classify_draft_intent("  send it  ") == DraftIntent.APPROVE

    def test_empty_string_is_revision(self) -> None:
        # Empty string doesn't match any pattern → defaults to revise
        assert classify_draft_intent("") == DraftIntent.REVISE

    def test_send_it_with_extra_words(self) -> None:
        # "send it please" should still match "send it"
        assert classify_draft_intent("send it please") == DraftIntent.APPROVE

    def test_ok_with_trailing_text(self) -> None:
        # "ok send" should match "ok" at start
        assert classify_draft_intent("ok send") == DraftIntent.APPROVE
