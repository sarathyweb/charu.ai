"""Property tests for email filtering (P19).

Property 19 — Email filtering excludes automated senders:
  For any set of emails, the ``get_emails_needing_reply`` filtering logic
  should exclude all emails from senders matching the no-reply heuristic
  (noreply, no-reply, notifications@, etc.) and return at most 3 results.

These are pure-function tests — no database or Gmail API required.
We test the ``_is_no_reply_sender`` helper and the ``NO_REPLY_PATTERNS``
tuple directly.

Validates: Requirements 11.2, 11.6
"""

from hypothesis import given, settings, strategies as st

from app.services.gmail_read_service import NO_REPLY_PATTERNS, _is_no_reply_sender


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A strategy that generates From headers containing a known no-reply pattern.
_no_reply_pattern = st.sampled_from(NO_REPLY_PATTERNS)

# Random display-name prefix (may be empty).
_display_name = st.text(
    alphabet=st.characters(categories=("L", "N", "Z")),
    min_size=0,
    max_size=40,
)

# Random domain suffix.
_domain = st.from_regex(r"[a-z]{3,12}\.(com|org|net|io)", fullmatch=True)

# A From header that embeds a no-reply pattern somewhere in the address.
_automated_from_header = st.builds(
    lambda name, pattern, domain: (
        f"{name} <{pattern}@{domain}>" if name else f"{pattern}@{domain}"
    ),
    name=_display_name,
    pattern=_no_reply_pattern,
    domain=_domain,
)

# A From header with a clearly human-looking address (no pattern overlap).
_human_from_header = st.builds(
    lambda first, last, domain: f"{first} {last} <{first}.{last}@{domain}>",
    first=st.from_regex(r"[a-z]{3,8}", fullmatch=True),
    last=st.from_regex(r"[a-z]{3,10}", fullmatch=True),
    domain=_domain,
)


# ---------------------------------------------------------------------------
# P19a: Every known no-reply pattern is detected
# ---------------------------------------------------------------------------


@given(pattern=_no_reply_pattern, domain=_domain)
@settings(max_examples=200)
def test_bare_pattern_detected(pattern: str, domain: str):
    """A From header containing any NO_REPLY_PATTERN is flagged."""
    header = f"{pattern}@{domain}"
    assert _is_no_reply_sender(header), (
        f"Expected pattern '{pattern}' to be detected in '{header}'"
    )


# ---------------------------------------------------------------------------
# P19b: Patterns detected regardless of case
# ---------------------------------------------------------------------------


@given(pattern=_no_reply_pattern, domain=_domain)
@settings(max_examples=200)
def test_pattern_case_insensitive(pattern: str, domain: str):
    """Detection is case-insensitive — mixed-case variants are caught."""
    mixed = "".join(
        c.upper() if i % 2 == 0 else c.lower()
        for i, c in enumerate(pattern)
    )
    header = f"Some Name <{mixed}@{domain}>"
    assert _is_no_reply_sender(header), (
        f"Expected mixed-case '{mixed}' to be detected in '{header}'"
    )


# ---------------------------------------------------------------------------
# P19c: Patterns detected when embedded in display-name + angle-bracket form
# ---------------------------------------------------------------------------


@given(from_header=_automated_from_header)
@settings(max_examples=200)
def test_automated_from_header_detected(from_header: str):
    """Generated automated From headers are always flagged."""
    assert _is_no_reply_sender(from_header), (
        f"Expected automated header to be detected: '{from_header}'"
    )


# ---------------------------------------------------------------------------
# P19d: Human-looking addresses are NOT flagged
# ---------------------------------------------------------------------------


@given(from_header=_human_from_header)
@settings(max_examples=200)
def test_human_from_header_not_flagged(from_header: str):
    """Generated human-looking From headers are never flagged."""
    assert not _is_no_reply_sender(from_header), (
        f"Human header should not be flagged: '{from_header}'"
    )


# ---------------------------------------------------------------------------
# P19e: Empty or whitespace-only From header is not flagged
# ---------------------------------------------------------------------------


@given(header=st.from_regex(r"\s{0,10}", fullmatch=True))
@settings(max_examples=50)
def test_empty_header_not_flagged(header: str):
    """Empty or whitespace-only From headers are not flagged."""
    assert not _is_no_reply_sender(header)


# ---------------------------------------------------------------------------
# P19f: Max results cap — filtering a list of N emails returns at most 3
# ---------------------------------------------------------------------------


@given(
    human_count=st.integers(min_value=0, max_value=20),
    automated_count=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=200)
def test_max_results_cap(human_count: int, automated_count: int):
    """Simulating the filtering + cap logic: after removing automated
    senders, at most ``max_results`` (3) emails are returned."""
    max_results = 3

    # Build a mixed list of From headers.
    human_headers = [f"person{i}@example.com" for i in range(human_count)]
    automated_headers = [f"noreply{i}@example.com" for i in range(automated_count)]
    all_headers = human_headers + automated_headers

    # Apply the same filtering logic used in get_emails_needing_reply.
    filtered = [h for h in all_headers if not _is_no_reply_sender(h)]
    capped = filtered[:max_results]

    assert len(capped) <= max_results
    # All automated senders must be excluded.
    for h in capped:
        assert not _is_no_reply_sender(h), f"Automated sender leaked through: {h}"
    # The capped count should equal min(human_count, max_results).
    assert len(capped) == min(human_count, max_results)


# ---------------------------------------------------------------------------
# P19g: Each individual pattern in NO_REPLY_PATTERNS is effective
# ---------------------------------------------------------------------------


def test_all_patterns_individually():
    """Exhaustive check: every pattern in NO_REPLY_PATTERNS triggers
    detection when used as a bare email address."""
    for pattern in NO_REPLY_PATTERNS:
        header = f"{pattern}@example.com"
        assert _is_no_reply_sender(header), (
            f"Pattern '{pattern}' was not detected in '{header}'"
        )


# ---------------------------------------------------------------------------
# P19h: Patterns with surrounding text are still detected
# ---------------------------------------------------------------------------


@given(
    pattern=_no_reply_pattern,
    prefix=st.from_regex(r"[a-z]{0,5}", fullmatch=True),
    suffix=st.from_regex(r"[a-z]{0,5}", fullmatch=True),
    domain=_domain,
)
@settings(max_examples=200)
def test_pattern_with_surrounding_text(
    pattern: str, prefix: str, suffix: str, domain: str
):
    """Patterns embedded within a larger local-part are still detected
    (substring match, not exact match)."""
    header = f"{prefix}{pattern}{suffix}@{domain}"
    assert _is_no_reply_sender(header), (
        f"Expected pattern '{pattern}' to be detected in '{header}'"
    )
