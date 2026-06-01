from fastapi import Header, HTTPException
from config import settings


async def verify_token(x_internal_token: str = Header(...)) -> None:
    if x_internal_token != settings.internal_token:
        raise HTTPException(status_code=401, detail="Invalid internal token")
