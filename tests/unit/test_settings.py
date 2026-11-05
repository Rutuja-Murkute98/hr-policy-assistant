"""Settings are checked at startup, and company details come from the tenant file."""
import pytest
from pydantic import ValidationError

from app.config import Settings, Tenant, load_tenant, settings, tenant


def make(**values):
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("values", [
    {"llm_provider": "nope"},
    {"embedding_provider": "anthropic"},  # Anthropic has no embeddings
    {"fusion_method": "nope"},
    {"abstention_mode": "nope"},
    {"reranker": "nope"},
    {"mmr_lambda": 1.5},
    {"fetch_k": 0},
    {"retrieve_k": 30, "fetch_k": 20},
    {"rate_limit_per_minute": 50, "rate_limit_total_per_minute": 30},
    {"reranker": "cohere", "cohere_api_key": ""},
    {"max_distance": 0},
    {"llm_temperature": 3},
])
def test_bad_settings_stop_the_app_at_startup(values):
    with pytest.raises(ValidationError):
        make(**values)


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("FETCH_K", "33")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    loaded = make()
    assert loaded.fetch_k == 33
    assert loaded.llm_provider == "anthropic"


def test_tenant_file_is_valid_and_lists_every_policy():
    assert tenant.id == "default"
    files = {file.name for file in settings.policy_dir.glob("*.md")}
    assert files == set(tenant.document_categories)


@pytest.mark.parametrize("change", [{"hr_email": "not-an-email"}, {"id": "Has Spaces"}, {"company_name": ""}])
def test_bad_tenant_details_are_rejected(change):
    values = tenant.model_dump() | change
    with pytest.raises(ValidationError):
        Tenant(**values)


def test_another_tenant_file_can_be_loaded(tmp_path):
    file = tmp_path / "other.toml"
    file.write_text(
        'id = "acme"\ncompany_name = "Acme"\nlegal_name = "Acme Ltd."\nhr_email = "hr@acme.test"\n'
        'product_name = "Acme HR"\nlogo_initials = "AC"\n', encoding="utf-8")
    assert load_tenant(file).company_name == "Acme"


def test_no_company_details_left_in_the_code():
    from app.config import PROJECT_ROOT

    for folder in ("app", "pipeline", "ui", "integrations"):
        for file in (PROJECT_ROOT / folder).rglob("*.py"):
            text = file.read_text(encoding="utf-8")
            assert "Rutuja Talent" not in text, file
            assert "rutujatalentnexus" not in text, file
