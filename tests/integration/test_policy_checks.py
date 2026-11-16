"""The checks after an upload, through the API on a real PostgreSQL, with a stand-in model. See D60.

Uses the isolation tests' organisations, Acme and Globex.
"""
from types import SimpleNamespace

import pytest

from app import config
from app.audience import DocumentTags
from app.policy_checks import ChangeSummary, PairVerdict, PairVerdicts
from tests.integration import test_tenant_isolation as isolation
from tests.integration.test_tenant_isolation import alice, gina, harriet

pytestmark = pytest.mark.integration
world = isolation.world
client = isolation.client

LEAVE = """# Leave Rules

**Version:** 1.0 | **Effective Date:** 1 April 2026 | **Next Review:** 1 April 2030

## 1. Sick Leave

All staff including those on probation are entitled to 12 days of Sick Leave per calendar year under these rules.
"""
PROBATION = """# Probation Rules

**Version:** 1.0 | **Next Review:** 1 January 2020

## 1. Entitlements During Probation

Sick Leave of up to 6 days per year is available from the date of joining under these probation rules.
"""


class Model:
    """Calls a clash when the two sides give different days of sick leave, and writes a fixed summary."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.pairs_seen: list[str] = []

    def with_structured_output(self, schema):
        def invoke(messages):
            if self.fail:
                raise RuntimeError("quota")
            if schema is PairVerdicts:
                self.pairs_seen.append(messages[1].content)
                count = messages[1].content.count("Pair ")
                return PairVerdicts(pairs=[PairVerdict(pair=number, clash=True,
                                                       explanation="12 days in one, up to 6 days in the other.")
                                           for number in range(1, count + 1)])
            if schema is ChangeSummary:
                return ChangeSummary(summary="Sick leave rose from 12 to 14 days a year.")
            if schema is DocumentTags:
                return DocumentTags()
            raise AssertionError(schema)

        return SimpleNamespace(invoke=invoke)


@pytest.fixture
def model(world, client, monkeypatch):
    stand_in = Model()
    monkeypatch.setattr(world.llm, "with_structured_output", stand_in.with_structured_output)
    yield stand_in
    for name in ("Leave_Rules.md", "Probation_Rules.md"):
        client.delete(f"/admin/documents/{name}?purge=true", headers=harriet(world))


def upload(client, headers, name, body, **fields):
    response = client.post("/admin/documents", headers=headers, files={"file": (name, body.encode())}, data=fields)
    assert response.status_code == 202, response.text
    return response.json()


def clashes(client, headers, status="open"):
    response = client.get(f"/admin/clashes?status={status}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_two_policies_that_contradict_each_other_are_flagged(world, client, model):
    upload(client, harriet(world), "Leave_Rules.md", LEAVE)
    upload(client, harriet(world), "Probation_Rules.md", PROBATION)
    found = clashes(client, harriet(world))
    assert len(found) == 1
    assert {found[0]["file_a"], found[0]["file_b"]} == {"Leave_Rules.md", "Probation_Rules.md"}
    assert found[0]["explanation"] == "12 days in one, up to 6 days in the other."
    assert "1. Entitlements During Probation" in (found[0]["section_a"], found[0]["section_b"])


def test_a_dismissed_clash_is_not_raised_again_until_a_text_changes(world, client, model):
    upload(client, harriet(world), "Leave_Rules.md", LEAVE)
    upload(client, harriet(world), "Probation_Rules.md", PROBATION)
    clash = clashes(client, harriet(world))[0]
    decided = client.patch("/admin/clashes", headers=harriet(world), json={
        "chunk_a": clash["chunk_a"], "chunk_b": clash["chunk_b"], "status": "dismissed"})
    assert decided.status_code == 200
    assert clashes(client, harriet(world)) == [] and len(clashes(client, harriet(world), "dismissed")) == 1
    asked = len(model.pairs_seen)
    assert client.post("/admin/clashes/check", headers=harriet(world)).status_code == 202
    assert len(model.pairs_seen) == asked  # already judged, with the same text
    assert any(event["action"] == "clash_decided" for event in client.get("/admin/audit", headers=harriet(world)).json())

    upload(client, harriet(world), "Leave_Rules.md", LEAVE.replace("12 days", "14 days"))
    reopened = clashes(client, harriet(world))
    assert len(reopened) == 1 and reopened[0]["status"] == "open"  # the text changed, so HR looks again


def test_a_new_version_gets_a_summary_of_what_changed(world, client, model):
    upload(client, harriet(world), "Leave_Rules.md", LEAVE)
    upload(client, harriet(world), "Leave_Rules.md", LEAVE.replace("12 days", "14 days"))
    versions = client.get("/admin/documents/Leave_Rules.md/versions", headers=harriet(world)).json()
    assert versions[0]["what_changed"] == "Sick leave rose from 12 to 14 days a year."
    assert versions[1]["what_changed"] is None  # the first version has nothing to compare with


def test_review_dates_come_from_the_document_or_the_form(world, client, model):
    upload(client, harriet(world), "Probation_Rules.md", PROBATION)
    upload(client, harriet(world), "Leave_Rules.md", LEAVE)
    listed = {document["file"]: document for document in client.get("/admin/documents", headers=harriet(world)).json()}
    assert listed["Probation_Rules.md"]["review_date"] == "2020-01-01"
    assert listed["Probation_Rules.md"]["review_overdue"] is True
    assert listed["Leave_Rules.md"]["review_overdue"] is False
    upload(client, harriet(world), "Probation_Rules.md", PROBATION, review_date="2031-06-30")
    listed = {document["file"]: document for document in client.get("/admin/documents", headers=harriet(world)).json()}
    assert listed["Probation_Rules.md"]["review_date"] == "2031-06-30"
    assert listed["Probation_Rules.md"]["review_overdue"] is False


def test_one_organisations_clashes_are_hidden_from_another(world, client, model):
    upload(client, harriet(world), "Leave_Rules.md", LEAVE)
    upload(client, harriet(world), "Probation_Rules.md", PROBATION)
    clash = clashes(client, harriet(world))[0]
    assert clashes(client, gina(world)) == []
    refused = client.patch("/admin/clashes", headers=gina(world), json={
        "chunk_a": clash["chunk_a"], "chunk_b": clash["chunk_b"], "status": "dismissed"})
    assert refused.status_code == 404
    assert client.get("/admin/clashes", headers=alice(world)).status_code == 403


def test_a_model_failure_in_the_checks_never_fails_the_upload(world, client, model, monkeypatch):
    monkeypatch.setattr(model, "fail", True)
    upload(client, harriet(world), "Leave_Rules.md", LEAVE)
    assert "Leave_Rules.md" in {document["file"] for document in
                                client.get("/admin/documents", headers=harriet(world)).json()}
    assert clashes(client, harriet(world)) == []
    assert config.TENANT_ID  # the default organisation is untouched by these uploads
