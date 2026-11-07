"""All project settings in one place, checked when the app starts.

Secrets and deployment choices come from environment variables or .env. Details about the
company, such as its name and HR email, come from the tenant file in config/.

The rest of the code reads the plain names at the bottom of this file.
"""
import re
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHAT_MODELS = {"google": "gemini-3.5-flash-lite", "anthropic": "claude-sonnet-5-5"}
DEFAULT_EMBEDDING_MODELS = {"google": "gemini-embedding-2"}
ORIGINAL_COLLECTION = "hr_policies_by_section"  # the name for the default embedding model

# Model providers read their API keys, such as GOOGLE_API_KEY, straight from the environment
load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    """Settings from the environment. Each field reads the variable with the same name in capitals."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # Tenant and database
    tenant_config_file: Path = PROJECT_ROOT / "config" / "tenant.toml"
    policy_dir: Path = PROJECT_ROOT / "policies" / "policies"
    database_url: str = ""  # empty = run PostgreSQL locally, in pgdata

    # Models. An empty model name means the provider's default, where it has one.
    llm_provider: Literal["google", "anthropic", "openai", "ollama"] = "google"
    llm_model: str = ""
    llm_temperature: float | None = Field(default=None, ge=0, le=2)  # None = the model's default
    embedding_provider: Literal["google", "openai", "ollama"] = "google"
    embedding_model: str = ""
    llm_max_tokens: int | None = Field(default=None, ge=1)
    # A busy provider held one answer open for 5 minutes with its own defaults: see docs/DECISIONS.md, D35
    llm_timeout_seconds: float | None = Field(default=20.0, gt=0)
    llm_max_retries: int | None = Field(default=1, ge=0)
    # A fallback is only used if it's set here: sending text to a second provider is a real decision
    llm_fallback_provider: Literal["", "google", "anthropic", "openai", "ollama"] = ""
    llm_fallback_model: str = ""

    # Search. See docs/DECISIONS.md for how these were chosen.
    hybrid_search: bool = True
    fusion_method: Literal["rrf", "weighted"] = "weighted"
    use_mmr: bool = True
    fetch_k: int = Field(default=20, ge=1, le=200)
    retrieve_k: int = Field(default=5, ge=1, le=20)
    mmr_lambda: float = Field(default=0.85, ge=0, le=1)
    rrf_k: int = Field(default=60, ge=1)
    keyword_weight: float = Field(default=0.05, ge=0)
    reranker_weight: float = Field(default=0.02, ge=0)
    min_body_chars: int = Field(default=100, ge=0)

    # Deciding whether there's enough evidence to answer
    abstention_mode: Literal["confidence", "distance"] = "confidence"
    max_distance: float = Field(default=0.42, gt=0, le=2)  # only for "distance" mode
    min_confidence: float = 3.0        # the best chunk must stand out this much, or the bot abstains
    min_chunk_confidence: float = 2.5  # each chunk sent to the LLM must stand out this much

    # The workflow: see docs/DECISIONS.md
    use_category_routing: bool = False  # measured worse: see docs/DECISIONS.md, D18
    grade_min_confidence: float = 4.0  # below this, the search is rewritten and tried once more
    out_of_scope_override_confidence: float = 4.5  # a search this strong overrules an "out of scope" label
    self_service_enabled: bool = True

    # Stored conversations
    conversation_retention_days: int = Field(default=90, ge=1)
    audit_retention_days: int = Field(default=365, ge=1)
    summarise_after_messages: int = Field(default=12, ge=2)
    keep_recent_messages: int = Field(default=6, ge=1)

    # Self-service tools
    approval_expiry_hours: int = Field(default=24, ge=1)
    tool_max_attempts: int = Field(default=3, ge=1, le=10)
    tool_backoff_seconds: float = Field(default=0.5, ge=0)

    # Reranking
    reranker: Literal["", "flashrank", "cohere"] = ""
    reranker_model: str = ""
    cohere_api_key: str = ""

    # API
    helpdesk_api_key: str = ""
    helpdesk_admin_key: str = ""  # empty = the admin endpoints are switched off
    helpdesk_api_url: str = "http://127.0.0.1:8000"
    rate_limit_per_minute: int = Field(default=10, ge=1)
    rate_limit_total_per_minute: int = Field(default=30, ge=1)  # protects the model quota
    max_question_chars: int = Field(default=500, ge=2)
    max_history_messages: int = Field(default=6, ge=0)
    max_message_chars: int = Field(default=2000, ge=1)
    max_upload_chars: int = Field(default=60_000, ge=1)
    max_upload_bytes: int = Field(default=5_000_000, ge=1)
    embedding_wait_seconds: int = Field(default=65, ge=0)

    # Logs, metrics and tracing: see docs/DECISIONS.md, D31 to D32
    log_format: Literal["json", "text"] = "json"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    metrics_retention_days: int = Field(default=90, ge=1)
    llm_price_input_per_million: float = Field(default=0.0, ge=0)  # 0 = a free tier
    llm_price_output_per_million: float = Field(default=0.0, ge=0)
    langfuse_public_key: str = ""  # tracing sends prompts to Langfuse, so it's off unless both keys are set
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""

    # Sign-in: see docs/DECISIONS.md, D27. "demo" keeps shared keys and a chosen demo employee.
    auth_mode: Literal["demo", "oidc"] = "demo"
    oidc_issuer: str = ""  # such as https://accounts.google.com
    oidc_client_id: str = ""
    platform_admin_emails: str = ""  # comma-separated: the first platform admins, who can add organisations
    redis_url: str = ""  # empty = rate limits are counted in PostgreSQL
    rate_limit_tenant_per_minute: int = Field(default=30, ge=1)
    rate_limit_login_per_minute: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def check_combinations(self) -> "Settings":
        if self.retrieve_k > self.fetch_k:
            raise ValueError("RETRIEVE_K can't be larger than FETCH_K")
        if self.rate_limit_per_minute > self.rate_limit_total_per_minute:
            raise ValueError("RATE_LIMIT_PER_MINUTE can't be larger than RATE_LIMIT_TOTAL_PER_MINUTE")
        if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_client_id):
            raise ValueError("AUTH_MODE=oidc needs OIDC_ISSUER and OIDC_CLIENT_ID")
        if self.llm_fallback_provider and not self.llm_fallback_model:
            raise ValueError("LLM_FALLBACK_PROVIDER needs LLM_FALLBACK_MODEL")
        if self.reranker == "cohere" and not self.cohere_api_key:
            raise ValueError("RERANKER=cohere needs COHERE_API_KEY")
        return self


class Tenant(BaseModel):
    """The company this deployment is for, from the tenant file."""

    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    company_name: str = Field(min_length=1)
    legal_name: str = Field(min_length=1)
    hr_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    product_name: str = Field(min_length=1)
    logo_initials: str = Field(min_length=1, max_length=3)
    default_category: str = "general"
    document_categories: dict[str, str] = Field(default_factory=dict)  # file name -> category


def load_tenant(path: Path) -> Tenant:
    with path.open("rb") as file:
        return Tenant(**tomllib.load(file))


settings = Settings()
tenant = load_tenant(settings.tenant_config_file)

TENANT_ID = tenant.id
POLICY_DIR = settings.policy_dir
DATABASE_URL = settings.database_url
PGDATA_DIR = PROJECT_ROOT / "pgdata"

LLM_PROVIDER: str = settings.llm_provider
CHAT_MODEL = settings.llm_model or DEFAULT_CHAT_MODELS.get(LLM_PROVIDER, "")
LLM_TEMPERATURE = settings.llm_temperature
LLM_MAX_TOKENS = settings.llm_max_tokens
LLM_TIMEOUT_SECONDS = settings.llm_timeout_seconds
LLM_MAX_RETRIES = settings.llm_max_retries
LLM_FALLBACK_PROVIDER: str = settings.llm_fallback_provider
LLM_FALLBACK_MODEL = settings.llm_fallback_model
EMBEDDING_PROVIDER: str = settings.embedding_provider
EMBEDDING_MODEL = settings.embedding_model or DEFAULT_EMBEDDING_MODELS.get(EMBEDDING_PROVIDER, "")

# Vectors from different embedding models can't be compared, so each model has its own collection
COLLECTION_NAME = (
    ORIGINAL_COLLECTION if (EMBEDDING_PROVIDER, EMBEDDING_MODEL) == ("google", "gemini-embedding-2")
    else re.sub(r"\W+", "_", f"hr_policies_{EMBEDDING_PROVIDER}_{EMBEDDING_MODEL}")
)

HYBRID_SEARCH = settings.hybrid_search
FUSION_METHOD = settings.fusion_method
USE_MMR = settings.use_mmr
FETCH_K = settings.fetch_k
RETRIEVE_K = settings.retrieve_k
MMR_LAMBDA = settings.mmr_lambda  # 1.0 = only relevance, 0.0 = only variety
RRF_K = settings.rrf_k
KEYWORD_WEIGHT = settings.keyword_weight
RERANKER_WEIGHT = settings.reranker_weight
MIN_BODY_CHARS = settings.min_body_chars  # ignore chunks that are only a one-line lead-in
ABSTENTION_MODE = settings.abstention_mode
MAX_DISTANCE = settings.max_distance  # tied to one embedding model, so it's only for "distance" mode
MIN_CONFIDENCE = settings.min_confidence
MIN_CHUNK_CONFIDENCE = settings.min_chunk_confidence

USE_CATEGORY_ROUTING = settings.use_category_routing
GRADE_MIN_CONFIDENCE = settings.grade_min_confidence
OUT_OF_SCOPE_OVERRIDE_CONFIDENCE = settings.out_of_scope_override_confidence
SELF_SERVICE_ENABLED = settings.self_service_enabled
CONVERSATION_RETENTION_DAYS = settings.conversation_retention_days
AUDIT_RETENTION_DAYS = settings.audit_retention_days
SUMMARISE_AFTER_MESSAGES = settings.summarise_after_messages
KEEP_RECENT_MESSAGES = settings.keep_recent_messages
APPROVAL_EXPIRY_HOURS = settings.approval_expiry_hours
TOOL_MAX_ATTEMPTS = settings.tool_max_attempts
TOOL_BACKOFF_SECONDS = settings.tool_backoff_seconds

RERANKER: str = settings.reranker
RERANKER_MODEL = settings.reranker_model
COHERE_API_KEY = settings.cohere_api_key
RERANKER_CACHE_DIR = PROJECT_ROOT / ".cache" / "flashrank"

API_KEY = settings.helpdesk_api_key
ADMIN_KEY = settings.helpdesk_admin_key
API_URL = settings.helpdesk_api_url
RATE_LIMIT_PER_MINUTE = settings.rate_limit_per_minute
RATE_LIMIT_TOTAL_PER_MINUTE = settings.rate_limit_total_per_minute
MAX_QUESTION_CHARS = settings.max_question_chars
MAX_HISTORY_MESSAGES = settings.max_history_messages
MAX_MESSAGE_CHARS = settings.max_message_chars
MAX_UPLOAD_CHARS = settings.max_upload_chars
MAX_UPLOAD_BYTES = settings.max_upload_bytes
EMBEDDING_WAIT_SECONDS = settings.embedding_wait_seconds

LOG_FORMAT: str = settings.log_format
LOG_LEVEL: str = settings.log_level
METRICS_RETENTION_DAYS = settings.metrics_retention_days
LLM_PRICE_INPUT_PER_MILLION = settings.llm_price_input_per_million
LLM_PRICE_OUTPUT_PER_MILLION = settings.llm_price_output_per_million
LANGFUSE_PUBLIC_KEY = settings.langfuse_public_key
LANGFUSE_SECRET_KEY = settings.langfuse_secret_key
LANGFUSE_BASE_URL = settings.langfuse_base_url

AUTH_MODE: str = settings.auth_mode
OIDC_ISSUER = settings.oidc_issuer
OIDC_CLIENT_ID = settings.oidc_client_id
PLATFORM_ADMIN_EMAILS = [email.strip() for email in settings.platform_admin_emails.split(",") if email.strip()]
REDIS_URL = settings.redis_url
RATE_LIMIT_TENANT_PER_MINUTE = settings.rate_limit_tenant_per_minute
RATE_LIMIT_LOGIN_PER_MINUTE = settings.rate_limit_login_per_minute
