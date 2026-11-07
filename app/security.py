"""Who is calling, what they may do, and rate limits that survive restarts and work across server processes."""
import logging
import random
import secrets
import time
from collections import defaultdict, deque
from typing import Protocol

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import Engine, text

from app import config
from app.identity import KEY_PREFIX, IdentityError, OrganisationUnavailable, Principal

logger = logging.getLogger("hr_helpdesk")

WINDOW_SECONDS = 60
MAX_TRACKED_CLIENTS = 1000
CLEAN_UP_CHANCE = 0.01  # how often a request also clears out old counts for clients who've gone


def _signed_in(request: Request, authorization: str) -> Principal | None:
    """Return who a sign-in token belongs to, if the request has one and sign-in is switched on."""
    identity = getattr(request.app.state, "identity", None)
    if not (authorization.lower().startswith("bearer ") and identity and identity.verifier):
        return None
    try:
        return identity.verifier.verify(authorization[7:].strip())
    except IdentityError as error:
        raise HTTPException(401, str(error)) from None


def current_principal(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
    x_user_id: str = Header(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$"),
) -> Principal:
    """Work out who's calling, and which organisation they belong to. The caller can't choose the tenant."""
    principal = _signed_in(request, authorization)  # a signed-in user is who the token says: X-User-Id is ignored
    identity = getattr(request.app.state, "identity", None)
    if principal is None and x_api_key.startswith(KEY_PREFIX) and identity:
        tenant_id = identity.api_keys.tenant_for(x_api_key)
        if tenant_id:
            # An integration, such as a Slack bot, says which of its own organisation's users it's acting for
            principal = Principal(tenant_id, x_user_id or None, "employee", "api_key")
    if principal is None:
        if config.AUTH_MODE == "oidc":
            raise HTTPException(401, "Please sign in")
        if not config.API_KEY:
            raise HTTPException(503, "The server has no HELPDESK_API_KEY set")
        if not secrets.compare_digest(x_api_key.encode(), config.API_KEY.encode()):
            raise HTTPException(401, "Invalid or missing API key")
        # Demo mode only: anyone with the shared key can name any user of the default organisation
        principal = Principal(config.TENANT_ID, x_user_id or None, "employee", "demo_key")
    request.state.tenant_id = principal.tenant_id  # for this request's log line
    return principal


def admin_principal(
    request: Request,
    authorization: str = Header(default=""),
    x_admin_key: str = Header(default=""),
) -> Principal:
    """Allow HR admins and platform admins. In demo mode, the shared admin key stands in for a role."""
    principal = _signed_in(request, authorization)
    if principal is not None:
        if principal.role not in ("hr_admin", "platform_admin"):
            raise HTTPException(403, "This needs an HR admin account")
        request.state.tenant_id = principal.tenant_id
        return principal
    if config.AUTH_MODE == "oidc":
        raise HTTPException(401, "Please sign in")
    if not config.ADMIN_KEY:
        raise HTTPException(503, "Admin is switched off. Set HELPDESK_ADMIN_KEY to use it.")
    if not secrets.compare_digest(x_admin_key.encode(), config.ADMIN_KEY.encode()):
        raise HTTPException(401, "Invalid or missing admin key")
    request.state.tenant_id = config.TENANT_ID
    return Principal(config.TENANT_ID, "admin", "hr_admin", "demo_key")


def platform_principal(principal: Principal = Depends(admin_principal)) -> Principal:
    if principal.role != "platform_admin":
        raise HTTPException(403, "This needs a platform admin account")
    return principal


def required_user(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.user_id:
        raise HTTPException(422, "This needs an X-User-Id header, or a signed-in user")
    return principal


def service_for(request: Request, principal: Principal):
    """Return the workflow for the caller's own organisation. No request can reach another's."""
    service = request.app.state.service
    for_tenant = getattr(service, "for_tenant", None)
    if for_tenant is None:
        return service
    try:
        return for_tenant(principal.tenant_id)
    except OrganisationUnavailable:
        raise HTTPException(403, "This organisation isn't available") from None


def audit(request: Request, principal: Principal, action: str, target: str = "", **details) -> None:
    """Record something that happened. A failure here is logged: the action itself has already happened."""
    identity = getattr(request.app.state, "identity", None)
    if identity is None:
        return
    try:
        identity.audit.record(principal.tenant_id, principal.user_id or principal.source, action, target, details)
    except Exception:
        logger.exception("Could not write an audit entry for %s", action)


class RateStore(Protocol):
    def allow(self, limits: list[tuple[str, int]]) -> str | None:
        """Record a hit if every (key, limit) has room. Otherwise return the first key that's full."""


class MemoryRateStore:
    """Counts in this process only, for tests. A restart forgets them and other processes can't see them."""

    def __init__(self) -> None:
        self.windows: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, limits: list[tuple[str, int]]) -> str | None:
        now = time.monotonic()
        if len(self.windows) > MAX_TRACKED_CLIENTS:
            for key, window in list(self.windows.items()):
                self._drop_old(window, now)
                if not window:
                    del self.windows[key]
        for key, limit in limits:
            self._drop_old(self.windows[key], now)
            if len(self.windows[key]) >= limit:
                return key
        for key, _ in limits:
            self.windows[key].append(now)
        return None

    @staticmethod
    def _drop_old(window: deque[float], now: float) -> None:
        while window and now - window[0] > WINDOW_SECONDS:
            window.popleft()


class PostgresRateStore:
    """Counts in the database, so limits survive restarts and are shared between server processes."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def allow(self, limits: list[tuple[str, int]]) -> str | None:
        with self.engine.begin() as connection:
            # Locks are always taken in the same order, so two requests can't deadlock
            for key in sorted(key for key, _ in limits):
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
            if random.random() < CLEAN_UP_CHANCE:
                connection.execute(text(
                    "DELETE FROM rate_limit_hits WHERE hit_at < now() - make_interval(secs => :window)"
                ), {"window": WINDOW_SECONDS})
            for key, limit in limits:
                count = connection.execute(text(
                    "SELECT count(*) FROM rate_limit_hits WHERE key = :key "
                    "AND hit_at >= now() - make_interval(secs => :window)"
                ), {"key": key, "window": WINDOW_SECONDS}).scalar() or 0
                if count >= limit:
                    return key
            for key, _ in limits:
                connection.execute(text("INSERT INTO rate_limit_hits (key) VALUES (:key)"), {"key": key})
        return None


class RedisRateStore:
    """Counts in Redis, if one is available. Each key is a sorted set of hit times.

    A hit is added first and taken back if it went over, so two servers at once can't both slip through.
    """

    def __init__(self, client):
        self.client = client

    def allow(self, limits: list[tuple[str, int]]) -> str | None:
        now = time.time()
        member = secrets.token_hex(8)
        full = None
        for key, limit in limits:
            self.client.zremrangebyscore(key, 0, now - WINDOW_SECONDS)
            self.client.zadd(key, {member: now})
            self.client.expire(key, WINDOW_SECONDS * 2)
            if self.client.zcard(key) > limit:
                full = key
                break
        if full:
            for key, _ in limits:
                self.client.zrem(key, member)
        return full


def get_rate_store(engine: Engine | None = None) -> RateStore:
    """Use Redis if it's set up, and otherwise the database, which every deployment already has."""
    if config.REDIS_URL:
        import redis  # only installed by deployments that use it

        return RedisRateStore(redis.Redis.from_url(config.REDIS_URL, decode_responses=True))
    return PostgresRateStore(engine) if engine is not None else MemoryRateStore()


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Limit each client, each organisation and everyone together, over a sliding 60-second window.

    A signed-in user is counted as themself. Otherwise the caller's X-Client-Id is used, as the chat
    screen sends every question from one server. Client ids are kept within the organisation, so one
    company can't use up another's allowance, and only callers who passed the identity check get here.
    """

    def __init__(self, per_client: int, total: int, per_tenant: int | None = None, logins: int | None = None):
        self.per_client = per_client
        self.total = total
        self.per_tenant = per_tenant if per_tenant is not None else total
        self.logins = logins if logins is not None else total
        self.store: RateStore = MemoryRateStore()

    def reset(self) -> None:
        self.store = MemoryRateStore()

    def __call__(self, request: Request, x_client_id: str = Header(default=""),
                 principal: Principal = Depends(current_principal)) -> None:
        tenant_id = principal.tenant_id
        client = principal.user_id if principal.source == "oidc" else x_client_id[:64] or _client_address(request)
        client_key = f"client:{tenant_id}:{client}"
        limits = [(client_key, self.per_client), (f"tenant:{tenant_id}", self.per_tenant), ("all", self.total)]
        full = self.store.allow(limits)
        if full == client_key:
            raise HTTPException(429, "Too many questions. Please wait a minute and try again.",
                                headers={"Retry-After": "60"})
        if full:
            raise HTTPException(429, "The helpdesk is busy. Please try again in a minute.",
                                headers={"Retry-After": "60"})

    def check_login(self, request: Request) -> None:
        """Slow down sign-in attempts, which happen before the caller is known."""
        if self.store.allow([(f"login:{_client_address(request)}", self.logins)]):
            raise HTTPException(429, "Too many sign-in attempts. Please wait a minute and try again.",
                                headers={"Retry-After": "60"})


rate_limit = RateLimiter(config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_TOTAL_PER_MINUTE,
                         config.RATE_LIMIT_TENANT_PER_MINUTE, config.RATE_LIMIT_LOGIN_PER_MINUTE)
