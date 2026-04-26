"""Opt-in staging smoke checks for external dependencies.

Run with:
    STAGING_SMOKE=1 STAGING_BASE_URL=https://... uv run pytest tests/smoke -q
"""

from __future__ import annotations

import os

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("STAGING_SMOKE") != "1",
    reason="Set STAGING_SMOKE=1 to run staging smoke checks.",
)


@pytest.mark.asyncio
async def test_staging_health_and_readiness():
    base_url = os.environ["STAGING_BASE_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        health = await client.get(f"{base_url}/health")
        ready = await client.get(f"{base_url}/health/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ready"
    assert all(check["ok"] for check in payload["checks"])
