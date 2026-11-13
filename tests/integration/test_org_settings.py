"""Each organisation's own settings, changed from the admin routes while the API runs.

Uses the two organisations from the isolation tests, Acme and Globex, on a real PostgreSQL. No model API is called.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import config
from app.org_settings import SettingsStore
from app.rag.prompt import StructuredAnswer
from app.tenancy import TenantEngine
from tests.integration import test_tenant_isolation as isolation
from tests.integration.test_tenant_isolation import alice, ask, bob, gina, harriet, owner_rows
from tests.integration.test_workflow import ScriptedLLM

pytestmark = pytest.mark.integration
# The two organisations, Acme and Globex, and a signed-in API client, as the isolation tests set them up
world = isolation.world
client = isolation.client


class NamedLLM(ScriptedLLM):
    """A model an organisation chose, so a test can tell which one answered."""

    def __init__(self, model, temperature):
        super().__init__(answer=StructuredAnswer(answered=True, answer="Forty days [1].", cited_chunk_ids=["1"]))
        self.model, self.temperature = model, temperature


@pytest.fixture
def settings_client(world, client, monkeypatch):
    monkeypatch.setattr(world.registry.settings, "cache_seconds", 0)
    monkeypatch.setattr(world.registry, "model_factory", lambda model, temperature: NamedLLM(model, temperature))
    monkeypatch.setattr(config, "ALLOWED_CHAT_MODELS", [config.CHAT_MODEL, "model-b"])
    world.registry._models.clear()
    prompts: list[str] = []
    real = type(world.llm).with_structured_output

    def spying(schema):
        inner = real(world.llm, schema)

        def invoke(messages):
            prompts.append(messages[0].content)
            return inner.invoke(messages)

        def stream(messages):
            prompts.append(messages[0].content)
            yield from inner.stream(messages)

        return SimpleNamespace(invoke=invoke, stream=stream)

    monkeypatch.setattr(world.llm, "with_structured_output", spying)
    client.prompts = prompts
    return client


def change(client, headers, **changes):
    response = client.patch("/admin/settings", headers=headers, json=changes)
    assert response.status_code == 200, response.text
    return response.json()


def test_settings_start_as_the_deployments(world, settings_client):
    shown = settings_client.get("/admin/settings", headers=harriet(world)).json()
    assert shown["changed"] == {}
    assert shown["effective"]["tone"] == "neutral" and shown["effective"]["company_name"] == "Acme"
    assert shown["effective"]["min_confidence"] == config.MIN_CONFIDENCE
    assert shown["allowed_chat_models"] == [config.CHAT_MODEL, "model-b"]


def test_a_change_is_used_by_the_next_answer_without_a_restart(world, settings_client):
    ask(world, settings_client, alice(world))
    assert "Be short and direct." in settings_client.prompts[-1]
    shown = change(settings_client, harriet(world), tone="formal", answer_length="short",
                   refusal_wording="Please ask the {company_name} people team.")
    assert shown["changed"] == {"tone": "formal", "answer_length": "short",
                                "refusal_wording": "Please ask the {company_name} people team."}
    ask(world, settings_client, alice(world))
    assert "formal, professional tone" in settings_client.prompts[-1]
    world.llm.answer = StructuredAnswer(answered=False)
    refusal = ask(world, settings_client, alice(world), question="Is there a gym?")["answer"]
    assert refusal == "Please ask the Acme people team."


def test_one_organisations_settings_never_reach_another(world, settings_client):
    change(settings_client, harriet(world), company_name="Acme Group", tone="formal", min_confidence=4.4)
    assert settings_client.get("/admin/settings", headers=gina(world)).json()["changed"] == {}
    assert settings_client.get("/me", headers=alice(world)).json()["organisation"]["company_name"] == "Acme Group"
    assert settings_client.get("/me", headers=bob(world)).json()["organisation"]["company_name"] == "Globex"
    ask(world, settings_client, bob(world))
    assert "formal" not in settings_client.prompts[-1]
    world.llm.answer = StructuredAnswer(answered=False)
    assert "hr@globex.example" in ask(world, settings_client, bob(world), question="Is there a gym?")["answer"]
    # The database itself hides Acme's row from Globex
    with TenantEngine(world.engine, "globex").connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM tenant_settings")).scalar() == 0
    assert len(owner_rows(world, "SELECT tenant_id FROM tenant_settings")) == 1


def test_an_admin_cannot_change_another_organisations_settings(world, settings_client):
    response = settings_client.patch("/admin/settings", headers=harriet(world),
                                     json={"tenant_id": "globex", "company_name": "Hijacked"})
    assert response.status_code == 422
    assert owner_rows(world, "SELECT tenant_id FROM tenant_settings") == []


@pytest.mark.parametrize("changes", [
    {"hr_email": "not-an-email"}, {"min_confidence": -1}, {"tone": "rude"}, {"retrieve_k": 500},
    {"chat_model": "model-z"}, {"categories": ["Has Spaces"]}, {"llm_provider": "openai"}, {"api_key": "x"},
    {"company_name": "Acme Group", "temperature": 9},  # one bad value means nothing is saved
])
def test_invalid_settings_are_refused_and_nothing_is_saved(world, settings_client, changes):
    response = settings_client.patch("/admin/settings", headers=harriet(world), json=changes)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert settings_client.get("/admin/settings", headers=harriet(world)).json()["changed"] == {}
    assert all(event["action"] != "settings_changed"
               for event in settings_client.get("/admin/audit", headers=harriet(world)).json())


def test_only_an_hr_admin_can_see_or_change_settings(world, settings_client):
    assert settings_client.get("/admin/settings", headers=alice(world)).status_code == 403
    assert settings_client.patch("/admin/settings", headers=alice(world), json={"tone": "formal"}).status_code == 403
    assert settings_client.get("/admin/settings").status_code == 401


def test_each_change_is_in_the_audit_log(world, settings_client):
    change(settings_client, harriet(world), min_confidence=4.0, tone="friendly")
    change(settings_client, harriet(world), min_confidence=4.0)  # no change, so no entry
    change(settings_client, harriet(world), tone=None)
    events = [event for event in settings_client.get("/admin/audit", headers=harriet(world)).json()
              if event["action"] == "settings_changed"]
    assert [event["target"] for event in events] == ["tone", "min_confidence, tone"]
    assert events[0]["details"]["changes"] == {"tone": ["friendly", None]}
    assert events[1]["details"]["changes"] == {"min_confidence": [None, 4.0], "tone": [None, "friendly"]}
    actor = owner_rows(world, "SELECT updated_by FROM tenant_settings WHERE tenant_id = 'acme'")[0].updated_by
    assert actor == events[0]["actor"]


def test_null_puts_a_setting_back_to_the_deployments(world, settings_client):
    change(settings_client, harriet(world), tone="formal", retrieve_k=3)
    shown = change(settings_client, harriet(world), tone=None)
    assert shown["changed"] == {"retrieve_k": 3}
    assert shown["effective"]["tone"] == "neutral"


def test_two_organisations_use_different_branding_models_and_thresholds(world, settings_client):
    change(settings_client, harriet(world), company_name="Acme Group", logo_initials="AG", chat_model="model-b",
           min_confidence=4.4, retrieve_k=3)
    change(settings_client, gina(world), product_name="Globex People", temperature=0.2, min_chunk_confidence=1.5)
    acme, globex = world.registry.for_tenant("acme"), world.registry.for_tenant("globex")
    assert (acme.llm.model, acme.llm.temperature) == ("model-b", None)
    assert (globex.llm.model, globex.llm.temperature) == (config.CHAT_MODEL, 0.2)
    assert acme.rag.llm is acme.llm  # answers and the workflow use the same chosen model
    assert (acme.rag.profile.company_name, acme.rag.profile.logo_initials) == ("Acme Group", "AG")
    assert globex.rag.profile.product_name == "Globex People" and globex.rag.profile.company_name == "Globex"
    assert acme.rag.profile.overrides.min_confidence == 4.4 and globex.rag.profile.overrides.min_confidence is None
    assert globex.rag.profile.overrides.min_chunk_confidence == 1.5
    assert ask(world, settings_client, alice(world))["answer"] == "Forty days [1]."  # answered by model-b


def test_a_change_from_another_process_is_noticed_within_the_cache_time(world, settings_client, monkeypatch):
    before = world.registry.for_tenant("acme")
    monkeypatch.setattr(world.registry.settings, "cache_seconds", 3600)
    world.registry.settings.read("acme")
    SettingsStore(world.engine).save("acme", {"tone": "friendly"}, "another-process")
    assert world.registry.for_tenant("acme") is before  # this process hasn't looked again yet
    monkeypatch.setattr(world.registry.settings, "cache_seconds", 0)
    after = world.registry.for_tenant("acme")
    assert after is not before and after.rag.profile.overrides.tone == "friendly"
    assert world.registry.for_tenant("acme") is after  # rebuilt once, not on every request


def test_settings_from_another_version_of_the_app_do_not_stop_answers(world, settings_client):
    with world.engine.begin() as connection:
        connection.execute(text("INSERT INTO tenant_settings (tenant_id, settings) VALUES "
                                "('acme', CAST(:settings AS jsonb))"),
                           {"settings": '{"tone": "formal", "min_confidence": "lots", "retired": 1}'})
    shown = settings_client.get("/admin/settings", headers=harriet(world)).json()
    assert shown["changed"] == {"tone": "formal"}
    assert ask(world, settings_client, alice(world))["answer"] == "Forty days [1]."
