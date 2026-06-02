from fastapi import Header, HTTPException, Request
from config import settings


async def verify_token(
    request: Request,
    x_internal_token: str = Header(...),
    x_cycle_id: str | None = Header(default=None),
) -> None:
    if x_internal_token != settings.internal_token:
        raise HTTPException(status_code=401, detail="Invalid internal token")
    # Attach cycle_id to request state so routers can log it without re-parsing
    request.state.cycle_id = x_cycle_id or "unknown"
