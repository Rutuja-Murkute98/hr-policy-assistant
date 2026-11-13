"""Create the chat and embedding models, so the provider is a setting and not a code change.

Each provider's package is imported only when it's used, so a deployment installs just
the one it needs. Google is the default and is already in requirements-api.txt.
"""
import logging
from importlib import import_module

from app import config

logger = logging.getLogger("hr_helpdesk")
MAX_TOKENS_NAME = {"google": "max_output_tokens", "ollama": "num_predict"}
CHAT_MODELS = {
    "google": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic"),
    "openai": ("langchain_openai", "ChatOpenAI"),
    "ollama": ("langchain_ollama", "ChatOllama"),
}
EMBEDDING_MODELS = {
    "google": ("langchain_google_genai", "GoogleGenerativeAIEmbeddings"),
    "openai": ("langchain_openai", "OpenAIEmbeddings"),
    "ollama": ("langchain_ollama", "OllamaEmbeddings"),
}


def _create(kind: str, setting: str, classes: dict, provider: str, model: str, **options):
    if provider not in classes:
        raise ValueError(f"{setting}={provider!r} isn't supported for {kind}. "
                         f"Choose one of: {', '.join(sorted(classes))}")
    if not model:
        raise ValueError(f"Set the model name to use with {setting}={provider}")
    package, class_name = classes[provider]
    try:
        model_class = getattr(import_module(package), class_name)
    except ImportError as error:
        raise ValueError(f"{setting}={provider} needs this package: "
                         f"pip install {package.replace('_', '-')}") from error
    return model_class(model=model, **options)


def chat_options(temperature: float | None = None) -> dict:
    """Translate the settings into each provider's own names for them. A temperature given here wins."""
    options: dict = {}
    temperature = config.LLM_TEMPERATURE if temperature is None else temperature
    if temperature is not None:
        options["temperature"] = temperature
    return options


def provider_options(provider: str, temperature: float | None = None) -> dict:
    options = chat_options(temperature)
    if config.LLM_MAX_TOKENS is not None:
        options[MAX_TOKENS_NAME.get(provider, "max_tokens")] = config.LLM_MAX_TOKENS
    if provider != "ollama":  # Ollama runs locally and has neither setting
        if config.LLM_TIMEOUT_SECONDS is not None:
            options["timeout"] = config.LLM_TIMEOUT_SECONDS
        if config.LLM_MAX_RETRIES is not None:
            options["max_retries"] = config.LLM_MAX_RETRIES
    return options


class FallbackModel:
    """Try the main model, then another provider's, only if a fallback was explicitly set up."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def invoke(self, messages):
        try:
            return self.primary.invoke(messages)
        except Exception:
            logger.exception("The main chat model failed, so the fallback was used")
            return self.fallback.invoke(messages)

    def stream(self, messages):
        started = False
        try:
            for piece in self.primary.stream(messages):
                started = True
                yield piece
        except Exception:
            if started:  # half an answer from each model would be wrong, so this has to fail
                raise
            logger.exception("The main chat model failed, so the fallback was used")
            yield from self.fallback.stream(messages)

    def with_structured_output(self, schema):
        return FallbackModel(self.primary.with_structured_output(schema), self.fallback.with_structured_output(schema))

    def bind_tools(self, tools):
        return FallbackModel(self.primary.bind_tools(tools), self.fallback.bind_tools(tools))


def get_chat_model(model: str | None = None, temperature: float | None = None):
    """The answer model. An organisation can choose another of the deployment's provider, and its temperature."""
    primary = _create("chat", "LLM_PROVIDER", CHAT_MODELS, config.LLM_PROVIDER, model or config.CHAT_MODEL,
                      **provider_options(config.LLM_PROVIDER, temperature))
    if not config.LLM_FALLBACK_PROVIDER:
        return primary
    fallback = _create("chat", "LLM_FALLBACK_PROVIDER", CHAT_MODELS, config.LLM_FALLBACK_PROVIDER,
                       config.LLM_FALLBACK_MODEL, **provider_options(config.LLM_FALLBACK_PROVIDER))
    return FallbackModel(primary, fallback)


def get_router_model():
    """A separate model for reading messages, if one is set. It's called once per message, so a faster one helps."""
    if not config.ROUTER_MODEL:
        return None
    provider = config.ROUTER_PROVIDER or config.LLM_PROVIDER
    return _create("chat", "ROUTER_PROVIDER", CHAT_MODELS, provider, config.ROUTER_MODEL, **provider_options(provider))


def get_embeddings():
    return _create("embeddings", "EMBEDDING_PROVIDER", EMBEDDING_MODELS,
                   config.EMBEDDING_PROVIDER, config.EMBEDDING_MODEL)
