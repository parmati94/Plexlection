"""Authentication endpoints.

palworld-lens keeps these in main.py; they belong in a router.
"""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.common.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    create_session_token,
    get_session_from_request,
    verify_credentials,
)
from backend.common.config import config
from backend.common.logging_config import get_logger
from backend.common.rate_limit import client_key, login_limiter

logger = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/status")
async def auth_status(request: Request):
    if not config.ENABLE_LOGIN:
        return {"enabled": False, "authenticated": True, "username": None}
    username = get_session_from_request(request)
    return {
        "enabled": True,
        "authenticated": username is not None,
        "username": username,
    }


@router.post("/login")
async def login(login_data: LoginRequest, request: Request, response: Response):
    if not config.ENABLE_LOGIN:
        raise HTTPException(status_code=400, detail="Authentication is not enabled")

    key = client_key(request)
    if login_limiter.is_blocked(key):
        retry = login_limiter.retry_after(key)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry}s.",
            headers={"Retry-After": str(retry)},
        )

    if not verify_credentials(login_data.username, login_data.password):
        login_limiter.record_failure(key)
        logger.warning("Failed login for '%s' from %s", login_data.username, key)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    login_limiter.reset(key)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_token(login_data.username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # set True when serving over HTTPS
    )
    logger.info("User '%s' logged in", login_data.username)
    return {"success": True, "username": login_data.username}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"success": True}
