"""POST /api/auth/sync — sync Firebase user to PostgreSQL after OTP login."""

from fastapi import APIRouter, Depends

from app.auth.firebase import get_firebase_user
from app.dependencies import get_user_service
from app.models.schemas import FirebasePrincipal
from app.services.user_service import UserService

router = APIRouter()


@router.post("/api/auth/sync")
async def auth_sync(
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
):
    """Ensure a user record exists in PostgreSQL after successful Firebase login.

    Returns the normalised phone and whether the record was newly created.
    """
    _user, created = await user_service.ensure_from_firebase(
        principal.phone_number, principal.uid
    )
    return {"phone": principal.phone_number, "created": created}
