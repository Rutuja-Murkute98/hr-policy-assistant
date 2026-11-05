"""API-key check and rate limiting for the API."""
import secrets
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request

from app import config

WINDOW_SECONDS = 60
MAX_TRACKED_CLIENTS = 1000


def verify_api_key(x_api_key: str = Header(default="")) -> str:
    """Reject requests without the right X-API-Key header."""
    if not config.API_KEY:
        raise HTTPException(503, "The server has no HELPDESK_API_KEY set")
    if not secrets.compare_digest(x_api_key.encode(), config.API_KEY.encode()):
        raise HTTPException(401, "Invalid or missing API key")
    return x_api_key


def verify_admin_key(x_admin_key: str = Header(default="")) -> str:
    """Reject requests without the right X-Admin-Key header. Admin is off until a key is set."""
    if not config.ADMIN_KEY:
        raise HTTPException(503, "Admin is switched off. Set HELPDESK_ADMIN_KEY to use it.")
    if not secrets.compare_digest(x_admin_key.encode(), config.ADMIN_KEY.encode()):
        raise HTTPException(401, "Invalid or missing admin key")
    return x_admin_key


def _drop_old(window: deque[float], now: float) -> None:
    while window and now - window[0] > WINDOW_SECONDS:
        window.popleft()


class RateLimiter:
    """Limit each client, and all clients together, over a sliding 60-second window.

    The chat screen sends every question from one server, so it identifies each chat
    session with an X-Client-Id header. Only callers that passed the API key check get
    here, so that header can be trusted.

    Counts are kept in memory, so they reset on restart and aren't shared between
    server processes. A larger deployment would use something like Redis.
    """

    def __init__(self, per_client: int, total: int):
        self.per_client = per_client
        self.total = total
        self.reset()

    def reset(self) -> None:
        self.clients: dict[str, deque[float]] = defaultdict(deque)
        self.everyone: deque[float] = deque()

    def _forget_idle_clients(self, now: float) -> None:
        for client, window in list(self.clients.items()):
            _drop_old(window, now)
            if not window:
                del self.clients[client]

    def __call__(self, request: Request, x_client_id: str = Header(default="")) -> None:
        now = time.monotonic()
        if len(self.clients) > MAX_TRACKED_CLIENTS:
            self._forget_idle_clients(now)

        client = x_client_id[:64] or (request.client.host if request.client else "unknown")
        window = self.clients[client]
        _drop_old(window, now)
        _drop_old(self.everyone, now)

        if len(window) >= self.per_client:
            raise HTTPException(429, "Too many questions. Please wait a minute and try again.",
                                headers={"Retry-After": "60"})
        if len(self.everyone) >= self.total:
            raise HTTPException(429, "The helpdesk is busy. Please try again in a minute.",
                                headers={"Retry-After": "60"})
        window.append(now)
        self.everyone.append(now)


rate_limit = RateLimiter(config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_TOTAL_PER_MINUTE)
