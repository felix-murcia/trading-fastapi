from fastapi import APIRouter, Depends
from models.context import ContextValidateRequest, ContextValidateResponse
from services import context_validator
from .deps import verify_token

router = APIRouter(prefix="/v1/context", tags=["context"])


@router.post("/validate", response_model=ContextValidateResponse)
async def validate_context(
    req: ContextValidateRequest,
    _: None = Depends(verify_token),
) -> ContextValidateResponse:
    return await context_validator.validate(req)
