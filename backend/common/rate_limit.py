"""Per-IP attempt limiting for the login endpoint.

palworld-lens has no rate limiting; satisfactory-lens does (backend/src/loginRateLimit.ts).
ENABLE_LOGIN=true implies the app is reachable by something you don't fully trust,
so a plain credential check is not enough on its own.

In-memory and per-process, which is correct here: there is exactly one uvicorn worker.
"""
import time
from collections import defaultdict, deque

from backend.common.config import config
from backend.common.logging_config import get_logger

logger = get_logger(__name__)


class AttemptLimiter:
    """Sliding-window counter of failed attempts, keyed on client IP."""

    def __init__(self, max_attempts: int, window_s: int):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.time())) >= self.max_attempts

    def retry_after(self, key: str) -> int:
        q = self._hits.get(key)
        if not q:
            return 0
        return max(0, int(self.window_s - (time.time() - q[0])) + 1)

    def record_failure(self, key: str) -> None:
        now = time.time()
        q = self._prune(key, now)
        q.append(now)
        if len(q) >= self.max_attempts:
            logger.warning("🔒 Login attempts from %s exhausted (%d in %ds)",
                           key, len(q), self.window_s)

    def reset(self, key: str) -> None:
        """Called on a successful login so a legitimate user isn't penalised
        for earlier typos."""
        self._hits.pop(key, None)


login_limiter = AttemptLimiter(config.LOGIN_MAX_ATTEMPTS, config.LOGIN_WINDOW_S)


def client_key(request) -> str:
    """Client identity for rate limiting. nginx sets X-Real-IP; fall back to the
    socket peer when running uvicorn directly in dev."""
    return request.headers.get("x-real-ip") or (
        request.client.host if request.client else "unknown"
    )
