"""Sign-in, users, API keys, the audit log and organisations.

An HR admin manages their own organisation only: the tenant always comes from their sign-in. A platform
admin can add and suspend organisations, but has no route to read what's inside one.
"""
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, SecretStr

from app import config
from app.config import Tenant
from app.identity import MAX_PHOTO_BYTES, Identity, IdentityError, Principal, Profiles, photo_type
from app.passwords import MAX_PASSWORD_CHARS, AccountProblem, PasswordAccounts, SignedIn
from app.security import admin_principal, audit, current_principal, platform_principal, rate_limit, service_for

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
    return {"user_id": principal.user_id, "email": principal.email, "display_name": principal.name,
            "role": principal.role, "tenant_id": principal.tenant_id, "organisation": profile.model_dump()}


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


def passwords_of(request: Request) -> PasswordAccounts:
    passwords = identity_of(request).passwords
    if passwords is None:
        raise HTTPException(404, "Password sign-in isn't switched on")
    return passwords


def signed_in_reply(request: Request, signed_in: SignedIn) -> dict:
    return {"token": signed_in.token, "max_age_seconds": signed_in.max_age_seconds,
            "session": session_details(request, signed_in.principal)}


class SignIn(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    password: SecretStr = Field(max_length=MAX_PASSWORD_CHARS)
    remember: bool = False


class SignUp(SignIn):
    display_name: str = Field(max_length=100)


class SetPassword(SignIn):
    code: str = Field(max_length=40)


@router.get("/auth/options", tags=["accounts"])
def sign_in_options():
    """What the sign-in page needs to know before anyone is signed in."""
    return {"mode": config.AUTH_MODE, "signup_enabled": config.ALLOW_SIGNUP,
            "min_password_chars": config.MIN_PASSWORD_CHARS, "signup_email_domains": config.SIGNUP_EMAIL_DOMAINS,
            "session_remember_days": config.SESSION_REMEMBER_DAYS}


@router.post("/auth/signup", status_code=201, tags=["accounts"])
def sign_up(body: SignUp, request: Request):
    """Create an employee account and sign in. It can't claim an existing account or an employee record."""
    rate_limit.check_login(request)
    rate_limit.check_account(body.email)
    try:
        result = passwords_of(request).sign_up(body.email, body.password.get_secret_value(), body.display_name,
                                               body.remember)
    except AccountProblem as problem:
        raise HTTPException(409, str(problem)) from None
    audit(request, result.principal, "signup")
    return signed_in_reply(request, result)


@router.post("/auth/login", tags=["accounts"])
def log_in(body: SignIn, request: Request):
    rate_limit.check_login(request)
    rate_limit.check_account(body.email)
    try:
        result = passwords_of(request).sign_in(body.email, body.password.get_secret_value(), body.remember)
    except IdentityError as error:
        identity_of(request).audit.record("unknown", "unknown", "login_failed", details={"reason": str(error)})
        raise HTTPException(401, str(error)) from None
    audit(request, result.principal, "login")
    return signed_in_reply(request, result)


@router.post("/auth/password", tags=["accounts"])
def set_password_with_code(body: SetPassword, request: Request):
    """Set a password with a one-time code from an HR admin, then sign in. Older sessions stop working."""
    rate_limit.check_login(request)
    rate_limit.check_account(body.email)
    try:
        result = passwords_of(request).set_with_code(body.email, body.code, body.password.get_secret_value(),
                                                     body.remember)
    except IdentityError as error:
        identity_of(request).audit.record("unknown", "unknown", "setup_code_failed", details={"reason": str(error)})
        raise HTTPException(401, str(error)) from None
    except AccountProblem as problem:
        raise HTTPException(409, str(problem)) from None
    audit(request, result.principal, "password_set")
    return signed_in_reply(request, result)


@router.post("/auth/logout", tags=["accounts"])
def log_out(request: Request, authorization: str = Header(default=""),
            principal: Principal = Depends(current_principal)):
    """End this session, so its token stops working everywhere."""
    if principal.source != "password":
        raise HTTPException(404, "Password sign-in isn't in use here")
    passwords_of(request).sign_out(authorization.removeprefix("Bearer ").strip())
    audit(request, principal, "logout")
    return {"signed_out": True}


PROFILE_TEXT = r"^[^<>\x00-\x1f]*$"  # plain text: no markup and no control characters


class ProfileChange(BaseModel):
    display_name: str = Field(min_length=1, max_length=100, pattern=PROFILE_TEXT)
    job_title: str = Field(default="", max_length=100, pattern=PROFILE_TEXT)
    department: str = Field(default="", max_length=100, pattern=PROFILE_TEXT)
    location: str = Field(default="", max_length=100, pattern=PROFILE_TEXT)


def own_profile(request: Request, principal: Principal = Depends(current_principal)) -> tuple[Profiles, Principal]:
    """Only someone signed in to their own account has a profile. A shared key or an integration doesn't."""
    profiles = identity_of(request).profiles
    if profiles is None or not principal.account_id:
        raise HTTPException(404, "Profiles need you to be signed in")
    return profiles, principal


@router.get("/me/profile", tags=["accounts"])
def read_profile(found: tuple[Profiles, Principal] = Depends(own_profile)):
    profiles, principal = found
    return profiles.get(principal.tenant_id, principal.account_id)


@router.put("/me/profile", tags=["accounts"], dependencies=[Depends(rate_limit)])
def change_profile(body: ProfileChange, request: Request, found: tuple[Profiles, Principal] = Depends(own_profile)):
    profiles, principal = found
    fields = {name: " ".join(value.split()) for name, value in body.model_dump().items()}
    if not fields["display_name"]:
        raise HTTPException(422, "Please enter your name")
    profiles.update(principal.tenant_id, principal.account_id, **fields)
    audit(request, principal, "profile_changed")
    return profiles.get(principal.tenant_id, principal.account_id)


@router.put("/me/profile/photo", tags=["accounts"], dependencies=[Depends(rate_limit)])
def change_photo(request: Request, photo: UploadFile = File(...),
                 found: tuple[Profiles, Principal] = Depends(own_profile)):
    """Set a profile photo: PNG, JPEG or WebP, up to 300 KB. The chat screen shrinks a photo before sending it."""
    profiles, principal = found
    data = photo.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Please use a photo under 300 KB")
    content_type = photo_type(data)
    if content_type is None:
        raise HTTPException(415, "Please use a PNG, JPEG or WebP photo")
    profiles.set_photo(principal.tenant_id, principal.account_id, data, content_type)
    audit(request, principal, "profile_photo_changed")
    return profiles.get(principal.tenant_id, principal.account_id)


@router.delete("/me/profile/photo", tags=["accounts"])
def remove_photo(request: Request, found: tuple[Profiles, Principal] = Depends(own_profile)):
    profiles, principal = found
    profiles.set_photo(principal.tenant_id, principal.account_id, None, None)
    audit(request, principal, "profile_photo_removed")
    return profiles.get(principal.tenant_id, principal.account_id)


class NewUser(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    role: Literal["employee", "hr_admin"] = "employee"  # a platform admin can only come from the server's settings
    employee_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    display_name: str = Field(default="", max_length=200)


class UserChange(BaseModel):
    role: Literal["employee", "hr_admin"] | None = None
    active: bool | None = None
    employee_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


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
    try:
        changed = identity_of(request).users.update(principal.tenant_id, user_id, body.role, body.active,
                                                    body.employee_id)
    except ValueError as problem:
        raise HTTPException(409, str(problem)) from None
    if not changed:
        raise HTTPException(404, "User not found")
    audit(request, principal, "user_changed", user_id, role=body.role, active=body.active,
          employee_id=body.employee_id)
    return {"updated": user_id}


@admin_router.post("/users/{user_id}/setup-code", status_code=201)
def create_setup_code(user_id: str, request: Request, principal: Principal = Depends(admin_principal)):
    """Make a one-time code so someone can set a password. This response is the only time it's shown."""
    code = passwords_of(request).issue_setup_code(principal.tenant_id, user_id)
    if code is None:
        raise HTTPException(404, "User not found")
    audit(request, principal, "setup_code_created", user_id)
    return {"code": code, "expires_in_hours": config.SETUP_CODE_HOURS}


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


@admin_router.get("/metrics")
def usage_metrics(request: Request, days: int = Query(default=7, ge=1, le=365),
                  principal: Principal = Depends(admin_principal)):
    """Report requests, speed, tokens, cost, refusals, tool failures and feedback for this organisation."""
    return service_for(request, principal).metrics.summary(days)


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
