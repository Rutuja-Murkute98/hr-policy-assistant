"""Create the chat and embedding models, so the provider is a setting and not a code change.

Each provider's package is imported only when it's used, so a deployment installs just
the one it needs. Google is the default and is already in requirements-api.txt.
"""
from importlib import import_module

from app import config

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


def _create(kind: str, setting: str, classes: dict, provider: str, model: str):
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
    return model_class(model=model)


def get_chat_model():
    return _create("chat", "LLM_PROVIDER", CHAT_MODELS, config.LLM_PROVIDER, config.CHAT_MODEL)


def get_embeddings():
    return _create("embeddings", "EMBEDDING_PROVIDER", EMBEDDING_MODELS,
                   config.EMBEDDING_PROVIDER, config.EMBEDDING_MODEL)
