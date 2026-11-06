"""Sign-in, users, API keys, the audit log and organisations.

An HR admin manages their own organisation only: the tenant always comes from their sign-in. A platform
admin can add and suspend organisations, but has no route to read what's inside one.
"""
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import config
from app.config import Tenant
from app.identity import Identity, IdentityError, Principal
from app.security import admin_principal, audit, current_principal, platform_principal, rate_limit

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
router = APIRouter()
admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_principal)])
platform_router = APIRouter(prefix="/platform", tags=["platform"], dependencies=[Depends(platform_principal)])


def identity_of(request: Request) -> Identity:
    identity = getattr(request.app.state, "identity", None)
    if identity is None:
        raise HTTPException(503, "Accounts aren't available")
    return identity


def session_details(request: Request, principal: Principal) -> dict:
    """What the screens need to know about the signed-in person and their organisation."""
    identity = getattr(request.app.state, "identity", None)
    profile = identity.organisations.get(principal.tenant_id) if identity else None
    if profile is None and principal.tenant_id == config.TENANT_ID:
        profile = config.tenant
    if profile is None:
        raise HTTPException(403, "This organisation isn't available")
    return {"user_id": principal.user_id, "email": principal.email, "role": principal.role,
            "tenant_id": principal.tenant_id, "organisation": profile.model_dump()}


@router.post("/auth/session", tags=["accounts"])
def start_session(request: Request, authorization: str = Header(default="")):
    """Record a sign-in, and say who it is. A screen calls this once, just after signing in."""
    rate_limit.check_login(request)
    identity = identity_of(request)
    if not identity.verifier:
        raise HTTPException(404, "Sign-in isn't switched on")
    try:
        principal = identity.verifier.verify(authorization.removeprefix("Bearer ").strip())
    except IdentityError as error:
        # The organisation isn't known, so only a platform admin's own queries can see these
        identity.audit.record("unknown", "unknown", "login_failed", details={"reason": str(error)})
        raise HTTPException(401, str(error)) from None
    audit(request, principal, "login")
    return session_details(request, principal)


@router.get("/me", tags=["accounts"])
def who_am_i(request: Request, principal: Principal = Depends(current_principal)):
    return session_details(request, principal)


class NewUser(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    role: Literal["employee", "hr_admin"] = "employee"  # a platform admin can only come from the server's settings
    employee_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    display_name: str = Field(default="", max_length=200)


class UserChange(BaseModel):
    role: Literal["employee", "hr_admin"] | None = None
    active: bool | None = None


@admin_router.get("/users")
def list_users(request: Request, principal: Principal = Depends(admin_principal)):
    return identity_of(request).users.list_for_tenant(principal.tenant_id)


@admin_router.post("/users", status_code=201)
def add_user(body: NewUser, request: Request, principal: Principal = Depends(admin_principal)):
    """Allow someone to sign in. They use their own account with the sign-in provider."""
    try:
        user = identity_of(request).users.create(principal.tenant_id, body.email, body.role, body.employee_id,
                                                 body.display_name)
    except ValueError as problem:
        raise HTTPException(409, str(problem)) from None
    audit(request, principal, "user_added", user["id"], role=body.role)
    return user


@admin_router.patch("/users/{user_id}")
def change_user(user_id: str, body: UserChange, request: Request, principal: Principal = Depends(admin_principal)):
    if not identity_of(request).users.update(principal.tenant_id, user_id, body.role, body.active):
        raise HTTPException(404, "User not found")
    audit(request, principal, "user_changed", user_id, role=body.role, active=body.active)
    return {"updated": user_id}


class NewApiKey(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@admin_router.get("/api-keys")
def list_api_keys(request: Request, principal: Principal = Depends(admin_principal)):
    return identity_of(request).api_keys.list_for_tenant(principal.tenant_id)


@admin_router.post("/api-keys", status_code=201)
def create_api_key(body: NewApiKey, request: Request, principal: Principal = Depends(admin_principal)):
    """Create a key for an integration. This response is the only time the key is shown."""
    created_by = principal.user_id or principal.source
    key = identity_of(request).api_keys.create(principal.tenant_id, body.name, created_by)
    audit(request, principal, "api_key_created", key["id"], name=body.name)
    return key


@admin_router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: str, request: Request, principal: Principal = Depends(admin_principal)):
    if not identity_of(request).api_keys.revoke(principal.tenant_id, key_id):
        raise HTTPException(404, "API key not found")
    audit(request, principal, "api_key_revoked", key_id)
    return {"revoked": key_id}


@admin_router.get("/audit")
def audit_log(request: Request, limit: int = Query(default=100, ge=1, le=500),
              principal: Principal = Depends(admin_principal)):
    """List what's happened in this organisation, newest first."""
    return identity_of(request).audit.recent(principal.tenant_id, limit)


class NewOrganisation(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=40)
    company_name: str = Field(min_length=1, max_length=200)
    legal_name: str = Field(min_length=1, max_length=200)
    hr_email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    product_name: str = Field(default="HR Helpdesk", min_length=1, max_length=100)
    logo_initials: str = Field(min_length=1, max_length=3)
    first_admin_email: str = Field(pattern=EMAIL_PATTERN, max_length=254)


class OrganisationChange(BaseModel):
    active: bool


@platform_router.get("/organisations")
def list_organisations(request: Request):
    return identity_of(request).organisations.list_all()


@platform_router.post("/organisations", status_code=201)
def add_organisation(body: NewOrganisation, request: Request, principal: Principal = Depends(platform_principal)):
    identity = identity_of(request)
    if identity.users.find_by_email(body.first_admin_email):
        raise HTTPException(409, "That email is already registered")
    profile = Tenant(**body.model_dump(exclude={"first_admin_email"}))
    try:
        identity.organisations.create(profile)
    except ValueError as problem:
        raise HTTPException(409, str(problem)) from None
    identity.users.create(profile.id, body.first_admin_email, "hr_admin")
    audit(request, principal, "organisation_added", profile.id)
    return {"id": profile.id}


@platform_router.patch("/organisations/{tenant_id}")
def change_organisation(tenant_id: str, body: OrganisationChange, request: Request,
                        principal: Principal = Depends(platform_principal)):
    """Suspend or restore an organisation. Its sign-ins and API keys stop working straight away."""
    if not identity_of(request).organisations.set_active(tenant_id, body.active):
        raise HTTPException(404, "Organisation not found")
    audit(request, principal, "organisation_changed", tenant_id, active=body.active)
    return {"updated": tenant_id}
