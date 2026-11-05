"""API-key check and rate limiting for the API."""
import secrets
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request

from app import config


def verify_api_key(x_api_key: str = Header(default="")) -> str:
    """Reject requests without the right X-API-Key header."""
    if not config.API_KEY:
        raise HTTPException(503, "The server has no HELPDESK_API_KEY set in .env")
    if not secrets.compare_digest(x_api_key.encode(), config.API_KEY.encode()):
        raise HTTPException(401, "Invalid or missing API key")
    return x_api_key


class RateLimiter:
    """Allow each client a limited number of requests in a sliding 60-second window.

    Counts are kept in memory, so they reset on restart and aren't shared between
    server processes. That's fine for one process; production would use something like Redis.
    """

    def __init__(self, limit_per_minute: int):
        self.limit = limit_per_minute
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def __call__(self, request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = self.requests[client]
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= self.limit:
            raise HTTPException(429, "Too many questions. Please wait a minute and try again.",
                                headers={"Retry-After": "60"})
        recent.append(now)


rate_limit = RateLimiter(config.RATE_LIMIT_PER_MINUTE)
