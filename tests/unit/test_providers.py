"""Choosing the chat and embedding models from settings. No API is called."""
import sys
from types import ModuleType

import pytest

from app import config
from app.rag import providers


class FakeModel:
    def __init__(self, model, **options):
        self.model = model
        self.options = options


@pytest.fixture
def fake_packages(monkeypatch):
    for package, class_name in [("langchain_anthropic", "ChatAnthropic"), ("langchain_openai", "ChatOpenAI")]:
        module = ModuleType(package)
        setattr(module, class_name, FakeModel)
        module.OpenAIEmbeddings = FakeModel
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


def test_model_settings_use_each_providers_own_names(fake_packages, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHAT_MODEL", "x")
    monkeypatch.setattr(config, "LLM_TEMPERATURE", 0.1)
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 500)
    monkeypatch.setattr(config, "LLM_TIMEOUT_SECONDS", 20)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 2)
    assert providers.get_chat_model().options == {"temperature": 0.1, "max_tokens": 500, "timeout": 20, "max_retries": 2}
    assert providers.provider_options("google")["max_output_tokens"] == 500
    assert providers.provider_options("ollama") == {"temperature": 0.1, "num_predict": 500}


def test_model_calls_have_a_time_limit_by_default():
    """A busy provider held one answer open for 5 minutes with its own defaults."""
    assert providers.provider_options("google") == {"timeout": 20.0, "max_retries": 1}


def test_clearing_the_settings_means_each_providers_defaults(monkeypatch):
    monkeypatch.setattr(config, "LLM_TIMEOUT_SECONDS", None)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", None)
    assert providers.provider_options("google") == {}


def test_fallback_is_off_unless_set(fake_packages, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHAT_MODEL", "x")
    assert isinstance(providers.get_chat_model(), FakeModel)


def test_fallback_is_used_when_set(fake_packages, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHAT_MODEL", "main")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "backup")
    model = providers.get_chat_model()
    assert isinstance(model, providers.FallbackModel)
    assert (model.primary.model, model.fallback.model) == ("main", "backup")


class Scripted:
    def __init__(self, reply=None, error=None, pieces=(), fail_after=None):
        self.reply, self.error, self.pieces, self.fail_after = reply, error, pieces, fail_after

    def invoke(self, messages):
        if self.error:
            raise self.error
        return self.reply

    def stream(self, messages):
        for number, piece in enumerate(self.pieces):
            if self.fail_after == number:
                raise RuntimeError("stopped")
            yield piece
        if self.error:
            raise self.error

    def with_structured_output(self, schema):
        return self

    def bind_tools(self, tools):
        return self


def test_fallback_answers_when_the_main_model_fails():
    model = providers.FallbackModel(Scripted(error=RuntimeError("quota")), Scripted(reply="backup"))
    assert model.invoke([]) == "backup"
    assert model.with_structured_output(dict).invoke([]) == "backup"
    assert model.bind_tools([]).invoke([]) == "backup"


def test_fallback_isnt_called_when_the_main_model_works():
    model = providers.FallbackModel(Scripted(reply="main"), Scripted(error=AssertionError("not used")))
    assert model.invoke([]) == "main"


def test_stream_falls_back_only_before_any_text():
    model = providers.FallbackModel(Scripted(error=RuntimeError("quota")), Scripted(pieces=["b", "c"]))
    assert list(model.stream([])) == ["b", "c"]


def test_stream_never_mixes_two_models_in_one_answer():
    model = providers.FallbackModel(Scripted(pieces=["a", "b"], fail_after=1), Scripted(pieces=["x"]))
    with pytest.raises(RuntimeError):
        list(model.stream([]))

