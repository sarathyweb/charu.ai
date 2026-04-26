"""Health and readiness endpoints."""

from fastapi import APIRouter, Response, status

from app.services.runtime_checks import run_runtime_checks

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(response: Response):
    payload = await run_runtime_checks()
    if payload["status"] != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
