"""Signed-cookie session auth.

itsdangerous is used directly rather than via Starlette's SessionMiddleware,
matching the house pattern. Auth is opt-in at runtime: when ENABLE_LOGIN is
false, require_auth is a no-op.
"""
import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.common.config import config
from backend.common.logging_config import get_logger

logger = get_logger(__name__)

SESSION_COOKIE_NAME = "plexlection_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET)


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time comparison on both fields.

    A plain `==` leaks length and prefix information through timing;
    palworld-lens does that and it's worth not copying.
    """
    user_ok = secrets.compare_digest(username or "", config.USERNAME)
    pass_ok = secrets.compare_digest(password or "", config.PASSWORD)
    # Evaluate both regardless of the first result, so timing doesn't reveal
    # whether the username alone was correct.
    return user_ok and pass_ok


def create_session_token(username: str) -> str:
    return _serializer.dumps({"u": username})


def verify_session_token(token: str) -> str | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None


def get_session_from_request(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return verify_session_token(token) if token else None


async def require_auth(request: Request) -> str | None:
    """FastAPI dependency. No-op when ENABLE_LOGIN is false."""
    if not config.ENABLE_LOGIN:
        return None
    username = get_session_from_request(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username
