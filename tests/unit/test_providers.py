"""Choosing the chat and embedding models from settings. No API is called."""
import sys
from types import ModuleType

import pytest

from app import config
from app.rag import providers


class FakeModel:
    def __init__(self, model):
        self.model = model


@pytest.fixture
def fake_packages(monkeypatch):
    for package, class_name in [("langchain_anthropic", "ChatAnthropic"), ("langchain_openai", "ChatOpenAI")]:
        module = ModuleType(package)
        setattr(module, class_name, FakeModel)
        setattr(module, "OpenAIEmbeddings", FakeModel)
        monkeypatch.setitem(sys.modules, package, module)


def test_chat_provider_and_model_come_from_settings(fake_packages, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHAT_MODEL", "claude-sonnet-5-5")
    assert providers.get_chat_model().model == "claude-sonnet-5-5"


def test_embedding_provider_and_model_come_from_settings(fake_packages, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "some-embedding-model")
    assert providers.get_embeddings().model == "some-embedding-model"


def test_unknown_provider_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "nope")
    with pytest.raises(ValueError, match="LLM_PROVIDER='nope' isn't supported"):
        providers.get_chat_model()


def test_anthropic_has_no_embeddings_option(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "anything")
    with pytest.raises(ValueError, match="isn't supported for embeddings"):
        providers.get_embeddings()


def test_provider_without_a_default_needs_a_model_name(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "CHAT_MODEL", "")
    with pytest.raises(ValueError, match="Set the model name"):
        providers.get_chat_model()


def test_missing_package_says_what_to_install(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "CHAT_MODEL", "some-model")
    monkeypatch.setitem(sys.modules, "langchain_ollama", None)
    with pytest.raises(ValueError, match="pip install langchain-ollama"):
        providers.get_chat_model()
