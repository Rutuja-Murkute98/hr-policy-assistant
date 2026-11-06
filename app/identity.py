"""Organisations, users, API keys, sign-in tokens and the audit log.

Finding which organisation a login or key belongs to has to look across tenants, so those few lookups
run as the database owner. Everything an organisation does after that runs inside its own tenant.
"""
import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx
import jwt
from sqlalchemy import Engine, text

from app import config
from app.config import Tenant
from app.pii import mask, mask_value
from app.tenancy import TenantEngine, check_tenant_id

logger = logging.getLogger("hr_helpdesk")

Role = Literal["employee", "hr_admin", "platform_admin"]
ROLES = ("employee", "hr_admin", "platform_admin")
KEY_PREFIX = "hrk_"
JWKS_TIMEOUT_SECONDS = 10


@dataclass
class Principal:
    """Who is making this request. The tenant always comes from here, never from what the caller sends."""

    tenant_id: str
    user_id: str | None
    role: Role
    source: Literal["oidc", "api_key", "demo_key"]
    email: str = ""


class IdentityError(Exception):
    """Sign-in failed. The message is safe to show, and doesn't say which check failed."""


class OrganisationUnavailable(Exception):
    """The organisation doesn't exist or is suspended."""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AuditLog:
    """Record who did what. Requests can only add to it: the app's database role can't change or delete entries."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def record(self, tenant_id: str, actor: str, action: str, target: str = "", details: dict | None = None) -> None:
        """Add an entry. The actor is an id, kept as it is: an audit log has to say who did it."""
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO audit_events (tenant_id, actor, action, target, details) "
                "VALUES (:tenant_id, :actor, :action, :target, CAST(:details AS jsonb))"
            ), {"tenant_id": tenant_id, "actor": actor, "action": action, "target": mask(target),
                "details": json.dumps(mask_value(details or {}), default=str)})

    def purge_older_than(self, days: int) -> int:
        """Remove entries past the retention period, at startup, so personal data isn't kept for ever."""
        removed = 0
        with self.engine.begin() as connection:
            for table in ("audit_events", "tool_audit_log"):
                removed += connection.execute(text(
                    f"DELETE FROM {table} WHERE created_at < now() - make_interval(days => :days)"
                ), {"days": days}).rowcount
        return removed

    def recent(self, tenant_id: str, limit: int = 100) -> list[dict]:
        with TenantEngine(self.engine, tenant_id).begin() as connection:
            rows = connection.execute(text(
                "SELECT actor, action, target, details, created_at FROM audit_events "
                "WHERE tenant_id = :tenant_id ORDER BY id DESC LIMIT :limit"
            ), {"tenant_id": tenant_id, "limit": limit}).all()
        return [{"actor": row.actor, "action": row.action, "target": row.target, "details": row.details,
                 "created_at": row.created_at.isoformat()} for row in rows]


class Organisations:
    def __init__(self, engine: Engine):
        self.engine = engine

    def ensure(self, profile: Tenant) -> None:
        """Add the organisation from a tenant file. An existing one is left as it is."""
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO organisations (id, company_name, legal_name, hr_email, product_name, logo_initials, "
                "default_category, document_categories) VALUES (:id, :company_name, :legal_name, :hr_email, "
                ":product_name, :logo_initials, :default_category, CAST(:document_categories AS jsonb)) "
                "ON CONFLICT (id) DO NOTHING"
            ), {**profile.model_dump(), "document_categories": json.dumps(profile.document_categories)})

    def get(self, tenant_id: str) -> Tenant | None:
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT * FROM organisations WHERE id = :id AND active"
            ), {"id": tenant_id}).first()
        if not row:
            return None
        return Tenant(id=row.id, company_name=row.company_name, legal_name=row.legal_name, hr_email=row.hr_email,
                      product_name=row.product_name, logo_initials=row.logo_initials,
                      default_category=row.default_category, document_categories=row.document_categories)

    def list_all(self) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, company_name, active, created_at FROM organisations ORDER BY id"
            )).all()
        return [{"id": row.id, "company_name": row.company_name, "active": row.active,
                 "created_at": row.created_at.isoformat()} for row in rows]

    def active_ids(self) -> list[str]:
        with self.engine.connect() as connection:
            return [row.id for row in connection.execute(text("SELECT id FROM organisations WHERE active")).all()]

    def create(self, profile: Tenant) -> None:
        check_tenant_id(profile.id)
        with self.engine.connect() as connection:
            if connection.execute(text("SELECT 1 FROM organisations WHERE id = :id"), {"id": profile.id}).first():
                raise ValueError("That organisation already exists")
        self.ensure(profile)

    def set_active(self, tenant_id: str, active: bool) -> bool:
        """Suspend or restore an organisation. Suspending stops its logins and API keys straight away."""
        with self.engine.begin() as connection:
            return connection.execute(text("UPDATE organisations SET active = :active WHERE id = :id"),
                                      {"id": tenant_id, "active": active}).rowcount > 0


class Users:
    def __init__(self, engine: Engine):
        self.engine = engine

    def find_by_email(self, email: str):
        with self.engine.connect() as connection:
            return connection.execute(text(
                "SELECT u.*, o.active AS organisation_active FROM users u JOIN organisations o ON o.id = u.tenant_id "
                "WHERE lower(u.email) = lower(:email)"
            ), {"email": email}).first()

    def pin_subject(self, user_id: str, subject: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE users SET oidc_subject = :subject WHERE id = :id"),
                               {"id": user_id, "subject": subject})

    def create(self, tenant_id: str, email: str, role: str, employee_id: str | None = None,
               display_name: str = "") -> dict:
        if role not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
        if self.find_by_email(email):
            raise ValueError("That email is already registered")
        user_id = uuid.uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (id, tenant_id, email, employee_id, display_name, role) "
                "VALUES (:id, :tenant_id, :email, :employee_id, :display_name, :role)"
            ), {"id": user_id, "tenant_id": tenant_id, "email": email, "employee_id": employee_id,
                "display_name": display_name, "role": role})
        return {"id": user_id, "email": email, "role": role, "employee_id": employee_id}

    def list_for_tenant(self, tenant_id: str) -> list[dict]:
        with TenantEngine(self.engine, tenant_id).begin() as connection:
            rows = connection.execute(text(
                "SELECT id, email, employee_id, display_name, role, active FROM users "
                "WHERE tenant_id = :tenant_id ORDER BY email"
            ), {"tenant_id": tenant_id}).all()
        return [{"id": row.id, "email": row.email, "employee_id": row.employee_id,
                 "display_name": row.display_name, "role": row.role, "active": row.active} for row in rows]

    def update(self, tenant_id: str, user_id: str, role: str | None, active: bool | None) -> bool:
        if role is not None and role not in ("employee", "hr_admin"):
            raise ValueError("Role must be employee or hr_admin")
        with TenantEngine(self.engine, tenant_id).begin() as connection:
            result = connection.execute(text(
                "UPDATE users SET role = COALESCE(:role, role), active = COALESCE(:active, active) "
                "WHERE id = :id AND tenant_id = :tenant_id AND role <> 'platform_admin'"
            ), {"id": user_id, "tenant_id": tenant_id, "role": role, "active": active})
        return result.rowcount > 0


class ApiKeys:
    """Keys for integrations, one or more per organisation. Only a hash is stored."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, tenant_id: str, name: str, created_by: str) -> dict:
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex
        with TenantEngine(self.engine, tenant_id).begin() as connection:
            connection.execute(text(
                "INSERT INTO api_keys (id, tenant_id, name, key_hash, key_prefix, created_by) "
                "VALUES (:id, :tenant_id, :name, :hash, :prefix, :created_by)"
            ), {"id": key_id, "tenant_id": tenant_id, "name": name, "hash": hash_key(key), "prefix": key[:10],
                "created_by": created_by})
        return {"id": key_id, "name": name, "key": key}  # the only time the key is ever shown

    def tenant_for(self, key: str) -> str | None:
        with self.engine.begin() as connection:
            row = connection.execute(text(
                "UPDATE api_keys k SET last_used_at = now() FROM organisations o WHERE k.key_hash = :hash "
                "AND k.revoked_at IS NULL AND o.id = k.tenant_id AND o.active RETURNING k.tenant_id"
            ), {"hash": hash_key(key)}).first()
        return row.tenant_id if row else None

    def list_for_tenant(self, tenant_id: str) -> list[dict]:
        with TenantEngine(self.engine, tenant_id).begin() as connection:
            rows = connection.execute(text(
                "SELECT id, name, key_prefix, created_by, created_at, last_used_at, revoked_at FROM api_keys "
                "WHERE tenant_id = :tenant_id ORDER BY created_at DESC"
            ), {"tenant_id": tenant_id}).all()
        return [{"id": row.id, "name": row.name, "key_prefix": row.key_prefix, "created_by": row.created_by,
                 "created_at": row.created_at.isoformat(),
                 "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                 "revoked": row.revoked_at is not None} for row in rows]

    def revoke(self, tenant_id: str, key_id: str) -> bool:
        with TenantEngine(self.engine, tenant_id).begin() as connection:
            result = connection.execute(text(
                "UPDATE api_keys SET revoked_at = now() WHERE id = :id AND tenant_id = :tenant_id "
                "AND revoked_at IS NULL"
            ), {"id": key_id, "tenant_id": tenant_id})
        return result.rowcount > 0


def discover_signing_key_getter(issuer: str) -> Callable[[str], object]:
    """Find the provider's published signing keys, from its standard discovery document."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    jwks_uri = httpx.get(url, timeout=JWKS_TIMEOUT_SECONDS).raise_for_status().json()["jwks_uri"]
    client = jwt.PyJWKClient(jwks_uri, cache_keys=True, timeout=JWKS_TIMEOUT_SECONDS)
    return lambda token: client.get_signing_key_from_jwt(token).key


class TokenVerifier:
    """Check a sign-in token's signature, issuer, audience and expiry, then find its user."""

    def __init__(self, issuer: str, audience: str, users: Users, key_getter: Callable[[str], object] | None = None):
        self.issuer = issuer
        self.audience = audience
        self.users = users
        self._key_getter = key_getter

    def _signing_key(self, token: str):
        if self._key_getter is None:
            self._key_getter = discover_signing_key_getter(self.issuer)
        return self._key_getter(token)

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self._signing_key(token), algorithms=["RS256", "ES256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except Exception as error:  # a bad token, or the provider's keys can't be reached
            logger.warning("A sign-in token was refused: %s", type(error).__name__)
            raise IdentityError("Please sign in again.") from None
        email = claims.get("email")
        # Required, not assumed: the first sign-in fixes who an email belongs to, so it has to be proven
        if not email or str(claims.get("email_verified")).lower() != "true":
            raise IdentityError("Your sign-in provider didn't confirm your email address.")
        user = self.users.find_by_email(email)
        if not user or not user.active or not user.organisation_active:
            raise IdentityError("This account isn't set up for the helpdesk. Please contact your HR team.")
        if user.oidc_subject is None:
            self.users.pin_subject(user.id, claims["sub"])  # the first sign-in fixes who this email belongs to
        elif user.oidc_subject != claims["sub"]:
            raise IdentityError("Please contact your HR team about this account.")
        return Principal(user.tenant_id, user.employee_id or user.id, user.role, "oidc", email)


def bootstrap_platform_admins(users: Users, emails: list[str], tenant_id: str) -> None:
    """Create the first platform admins from a setting, as nobody exists yet to invite them."""
    for email in emails:
        if not users.find_by_email(email):
            users.create(tenant_id, email, "platform_admin")


@dataclass
class Identity:
    organisations: Organisations
    users: Users
    api_keys: ApiKeys
    audit: AuditLog
    verifier: TokenVerifier | None


def create_identity(engine: Engine) -> Identity:
    """Set up accounts at startup: the first organisation, the first platform admins and token checks."""
    organisations, users = Organisations(engine), Users(engine)
    organisations.ensure(config.tenant)
    bootstrap_platform_admins(users, config.PLATFORM_ADMIN_EMAILS, config.TENANT_ID)
    verifier = TokenVerifier(config.OIDC_ISSUER, config.OIDC_CLIENT_ID, users) if config.AUTH_MODE == "oidc" else None
    return Identity(organisations, users, ApiKeys(engine), AuditLog(engine), verifier)
