"""Each organisation's own settings, on top of the deployment's.

An organisation stores only what it changed. Every setting is read through setting(): the organisation's value if it
set one, otherwise the environment variable, otherwise the default in app/config.py. See docs/DECISIONS.md, D52.
"""
import json
import logging
import re
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Engine, text

from app import config
from app.config import Tenant
from app.tenancy import TenantEngine, tenant_scoped

logger = logging.getLogger("hr_helpdesk")

CATEGORY_PATTERN = r"^[a-z][a-z0-9_]{0,29}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
BRANDING = ("company_name", "legal_name", "hr_email", "product_name", "logo_initials")
# The deployment setting each organisation setting falls back to
FROM_DEPLOYMENT = {
    "chat_model": "CHAT_MODEL", "temperature": "LLM_TEMPERATURE",
    "min_confidence": "MIN_CONFIDENCE", "min_chunk_confidence": "MIN_CHUNK_CONFIDENCE",
    "grade_min_confidence": "GRADE_MIN_CONFIDENCE",
    "out_of_scope_override_confidence": "OUT_OF_SCOPE_OVERRIDE_CONFIDENCE",
    "skip_understand_confidence": "SKIP_UNDERSTAND_CONFIDENCE", "retrieve_k": "RETRIEVE_K",
    "self_service_enabled": "SELF_SERVICE_ENABLED", "cache_answers": "CACHE_ANSWERS",
    "skip_understand_when_clear": "SKIP_UNDERSTAND_WHEN_CLEAR",
}
BUILT_IN = {"refusal_wording": "", "tone": "neutral", "answer_length": "normal"}


class OrgSettings(BaseModel):
    """The settings an organisation changed. A missing value means the deployment's."""

    model_config = ConfigDict(extra="forbid")

    # Branding, shown on the screens and in answers
    company_name: str | None = Field(default=None, min_length=1, max_length=100)
    legal_name: str | None = Field(default=None, min_length=1, max_length=150)
    hr_email: str | None = Field(default=None, pattern=EMAIL_PATTERN, max_length=200)
    product_name: str | None = Field(default=None, min_length=1, max_length=60)
    logo_initials: str | None = Field(default=None, min_length=1, max_length=3)
    # Topics the policies are filed under
    categories: list[str] | None = Field(default=None, min_length=1, max_length=30)
    default_category: str | None = Field(default=None, pattern=CATEGORY_PATTERN)
    # The answer model, from the ones the deployment allows
    chat_model: str | None = Field(default=None, min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    # Search: how strongly the evidence must stand out, and how many excerpts the model reads
    min_confidence: float | None = Field(default=None, ge=0, le=10)
    min_chunk_confidence: float | None = Field(default=None, ge=0, le=10)
    grade_min_confidence: float | None = Field(default=None, ge=0, le=10)
    out_of_scope_override_confidence: float | None = Field(default=None, ge=0, le=10)
    skip_understand_confidence: float | None = Field(default=None, ge=0, le=10)
    retrieve_k: int | None = Field(default=None, ge=1, le=20)
    # How answers read
    refusal_wording: str | None = Field(default=None, min_length=1, max_length=400)
    tone: Literal["neutral", "friendly", "formal"] | None = None
    answer_length: Literal["short", "normal", "detailed"] | None = None
    # Switches
    self_service_enabled: bool | None = None
    cache_answers: bool | None = None
    skip_understand_when_clear: bool | None = None

    @field_validator("categories")
    @classmethod
    def check_categories(cls, categories: list[str] | None) -> list[str] | None:
        if categories is None:
            return None
        cleaned = [category.strip().lower() for category in categories]
        for category in cleaned:
            if not re.fullmatch(CATEGORY_PATTERN, category):
                raise ValueError(f"'{category}' isn't a valid category: start with a letter, then use lowercase "
                                 "letters, numbers and _, up to 30 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Each category can only be listed once")
        return cleaned


class Profile(Tenant):
    """An organisation as answers see it: its branding, and the settings it changed."""

    overrides: OrgSettings = Field(default_factory=OrgSettings)
    version: str = ""  # changes whenever the organisation's settings do


class SettingsError(ValueError):
    """A setting the deployment doesn't allow, such as a model it didn't list."""


def setting(profile: Tenant | None, name: str) -> Any:
    """The organisation's value if it set one, otherwise the deployment's. Works on a plain Tenant too."""
    value = getattr(getattr(profile, "overrides", None), name, None)
    if name == "chat_model" and value is not None and value not in config.ALLOWED_CHAT_MODELS:
        # The owner removed it from the list after it was chosen, so the deployment's model is used
        logger.warning("The chat model %s is no longer allowed, so the deployment's model is used", value)
        value = None
    if value is not None:
        return value
    if name in FROM_DEPLOYMENT:
        return getattr(config, FROM_DEPLOYMENT[name])
    return BUILT_IN[name]


def effective_settings(profile: Tenant) -> dict:
    """Every setting an organisation can change, with the value answers use now."""
    values = {name: setting(profile, name) for name in (*FROM_DEPLOYMENT, *BUILT_IN)}
    values.update({name: getattr(profile, name) for name in BRANDING})
    values["categories"] = categories_of(profile)
    values["default_category"] = profile.default_category
    return values


def categories_of(profile: Tenant) -> list[str]:
    """The organisation's own list if it set one, otherwise the categories its documents were given."""
    chosen = getattr(getattr(profile, "overrides", None), "categories", None)
    return sorted({*(chosen or profile.document_categories.values()), profile.default_category})


def resolve(base: Tenant, overrides: OrgSettings | None = None, version: str = "") -> Profile:
    """Apply an organisation's changes to its profile from the tenant file or the organisations table."""
    overrides = overrides or OrgSettings()
    data = base.model_dump(exclude={"overrides", "version"})
    for name in (*BRANDING, "default_category"):
        value = getattr(overrides, name)
        if value is not None:
            data[name] = value
    return Profile(**data, overrides=overrides, version=version)


def public_profile(profile: Tenant) -> dict:
    """What the screens need: branding and categories, but not the model or thresholds."""
    return {**profile.model_dump(include={*BRANDING, "id", "default_category", "document_categories"}),
            "categories": categories_of(profile)}


def check_allowed(overrides: OrgSettings) -> None:
    """Refuse values the deployment doesn't allow. Only checked on saving, as the deployment can change later."""
    if overrides.chat_model is not None and overrides.chat_model not in config.ALLOWED_CHAT_MODELS:
        raise SettingsError(f"The chat model must be one of: {', '.join(config.ALLOWED_CHAT_MODELS)}")
    if overrides.retrieve_k is not None and overrides.retrieve_k > config.FETCH_K:
        raise SettingsError(f"The number of excerpts can't be more than {config.FETCH_K}, the deployment's FETCH_K")


def load_overrides(stored: dict | None) -> OrgSettings:
    """Read stored settings. A value that no longer passes the checks is ignored, rather than stopping answers."""
    stored = dict(stored or {})
    try:
        return OrgSettings.model_validate(stored)
    except ValidationError as error:
        bad = {str(problem["loc"][0]) for problem in error.errors() if problem["loc"]}
        logger.warning("Ignoring stored settings that are no longer valid: %s", ", ".join(sorted(bad)))
        return OrgSettings.model_validate({name: value for name, value in stored.items()
                                           if name not in bad and name in OrgSettings.model_fields})


class SettingsStore:
    """Read and change organisations' settings. A read is reused for SETTINGS_CACHE_SECONDS, and a change clears it."""

    def __init__(self, engine: Engine | TenantEngine, cache_seconds: float | None = None):
        self.engine = engine
        self.cache_seconds = config.SETTINGS_CACHE_SECONDS if cache_seconds is None else cache_seconds
        self._cache: dict[str, tuple[float, OrgSettings, str]] = {}
        self._lock = threading.Lock()

    def read(self, tenant_id: str) -> tuple[OrgSettings, str]:
        """An organisation's changed settings, and their version."""
        with self._lock:
            cached = self._cache.get(tenant_id)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1], cached[2]
        with tenant_scoped(self.engine, tenant_id).connect() as connection:
            row = connection.execute(text(
                "SELECT settings, updated_at FROM tenant_settings WHERE tenant_id = :tenant_id"
            ), {"tenant_id": tenant_id}).first()
        overrides = load_overrides(row.settings if row else None)
        version = row.updated_at.isoformat() if row else ""
        with self._lock:
            self._cache[tenant_id] = (time.monotonic(), overrides, version)
        return overrides, version

    def profile(self, tenant_id: str, base: Tenant) -> Profile:
        overrides, version = self.read(tenant_id)
        return resolve(base, overrides, version)

    def forget(self, tenant_id: str) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)

    def save(self, tenant_id: str, changes: dict, actor: str) -> tuple[OrgSettings, dict]:
        """Apply changes, where None puts a setting back to the deployment's. Returns the result and what changed.

        Raises ValidationError or SettingsError for a value that isn't allowed, and then nothing is saved.
        """
        unknown = sorted(set(changes) - set(OrgSettings.model_fields))
        if unknown:
            raise SettingsError(f"These aren't settings an organisation can change: {', '.join(unknown)}")
        with tenant_scoped(self.engine, tenant_id).begin() as connection:
            # The row is locked, so two admins saving at once can't lose each other's changes
            connection.execute(text(
                "INSERT INTO tenant_settings (tenant_id) VALUES (:tenant_id) ON CONFLICT (tenant_id) DO NOTHING"
            ), {"tenant_id": tenant_id})
            stored = connection.execute(text(
                "SELECT settings FROM tenant_settings WHERE tenant_id = :tenant_id FOR UPDATE"
            ), {"tenant_id": tenant_id}).scalar_one()
            before = load_overrides(stored)
            merged = before.model_dump(exclude_none=True)
            for name, value in changes.items():
                if value is None:
                    merged.pop(name, None)
                else:
                    merged[name] = value
            after = OrgSettings.model_validate(merged)
            check_allowed(after)
            connection.execute(text(
                "UPDATE tenant_settings SET settings = CAST(:settings AS jsonb), updated_at = clock_timestamp(), "
                "updated_by = :actor WHERE tenant_id = :tenant_id"
            ), {"tenant_id": tenant_id, "settings": json.dumps(after.model_dump(exclude_none=True)), "actor": actor})
        self.forget(tenant_id)
        changed = {name: [getattr(before, name), getattr(after, name)] for name in OrgSettings.model_fields
                   if getattr(before, name) != getattr(after, name)}
        return after, changed
