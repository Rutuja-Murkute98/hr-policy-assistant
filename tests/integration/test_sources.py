"""Syncing policies from a watched folder, through the API, on a real PostgreSQL. No model API is called.

Uses the isolation tests' two organisations, Acme and Globex. Google Drive and SharePoint are tested with stand-ins in
tests/unit/test_connectors.py: a sync treats every connector the same.
"""
import os

import pytest

from app import config
from app.connectors.base import ConnectorError, SourceFile
from app.source_scheduler import SourceScheduler
from app.sources import SourceStore, sync_source
from tests.integration import test_tenant_isolation as isolation
from tests.integration.test_tenant_isolation import alice, gina, harriet, owner_rows

pytestmark = pytest.mark.integration
world = isolation.world
client = isolation.client

POLICY = """# Gym Policy

## 1. Membership

Staff get a free gym membership at the partner gym near each office, renewed every year by HR.
"""
NOT_A_PDF = "This file doesn't look like a real PDF."


@pytest.fixture
def folder(tmp_path, monkeypatch, client):
    monkeypatch.setattr(config, "SOURCES_ROOT", str(tmp_path))
    hr = tmp_path / "acme" / "hr"
    hr.mkdir(parents=True)
    (tmp_path / "globex").mkdir()
    return hr


def add(client, headers, **fields):
    body = {"kind": "folder", "name": "HR folder", "settings": {"path": "hr"}, **fields}
    response = client.post("/admin/sources", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def sync(client, headers, source):
    response = client.post(f"/admin/sources/{source['id']}/sync", headers=headers)
    assert response.status_code == 202, response.text
    return client.get("/admin/sources", headers=headers).json()["sources"][0]


def documents(client, headers):
    return {document["file"] for document in client.get("/admin/documents", headers=headers).json()}


def touch(path, body):
    path.write_text(body, encoding="utf-8")
    stamp = path.stat().st_mtime_ns + 10**9
    os.utime(path, ns=(stamp, stamp))


def test_a_folder_syncs_new_changed_and_removed_policies(world, client, folder):
    touch(folder / "Gym Policy.md", POLICY)
    source = add(client, harriet(world))
    synced = sync(client, harriet(world), source)
    assert synced["status"] == "ok" and synced["last_summary"]["added"] == 1
    assert "Gym_Policy.md" in documents(client, harriet(world))
    files = client.get(f"/admin/sources/{source['id']}/files", headers=harriet(world)).json()
    assert files[0]["policy_file"] == "Gym_Policy.md"

    unchanged = sync(client, harriet(world), source)["last_summary"]
    assert unchanged["unchanged"] == 1 and unchanged["added"] == unchanged["updated"] == 0

    touch(folder / "Gym Policy.md", POLICY.replace("every year", "every two years"))
    assert sync(client, harriet(world), source)["last_summary"]["updated"] == 1
    versions = client.get("/admin/documents/Gym_Policy.md/versions", headers=harriet(world)).json()
    assert len(versions) == 2

    (folder / "Gym Policy.md").unlink()
    assert sync(client, harriet(world), source)["last_summary"]["removed"] == 1
    assert "Gym_Policy.md" not in documents(client, harriet(world))


def test_a_source_can_keep_policies_whose_file_is_removed(world, client, folder):
    touch(folder / "Gym Policy.md", POLICY)
    source = add(client, harriet(world), keep_removed=True)
    sync(client, harriet(world), source)
    (folder / "Gym Policy.md").unlink()
    assert sync(client, harriet(world), source)["last_summary"]["removed"] == 0
    assert "Gym_Policy.md" in documents(client, harriet(world))
    client.delete("/admin/documents/Gym_Policy.md?purge=true", headers=harriet(world))


def test_a_file_that_cannot_be_read_is_reported_and_the_rest_sync(world, client, folder):
    touch(folder / "Gym Policy.md", POLICY)
    (folder / "Broken.pdf").write_bytes(b"not a pdf")
    synced = sync(client, harriet(world), add(client, harriet(world)))
    assert synced["status"] == "ok" and synced["last_summary"]["added"] == 1
    assert synced["last_summary"]["failed"] == [{"file": "Broken.pdf", "reason": NOT_A_PDF}]
    client.delete("/admin/documents/Gym_Policy.md?purge=true", headers=harriet(world))


def test_a_folder_outside_the_organisations_own_is_refused(world, client, folder):
    for path in ("../globex", "hr/../../globex"):
        response = client.post("/admin/sources", headers=harriet(world),
                               json={"kind": "folder", "name": "Sneaky", "settings": {"path": path}})
        assert response.status_code == 422
    unavailable = client.post("/admin/sources", headers=harriet(world),
                              json={"kind": "google_drive", "name": "Drive", "settings": {"folder_id": "a" * 20}})
    assert unavailable.status_code == 422 and "isn't set up" in unavailable.json()["detail"]


def test_one_organisations_sources_are_hidden_from_another(world, client, folder):
    source = add(client, harriet(world))
    listing = client.get("/admin/sources", headers=gina(world)).json()
    assert listing == {"sources": [], "available_kinds": ["folder"]}
    for method, path in (("post", f"/admin/sources/{source['id']}/sync"), ("delete", f"/admin/sources/{source['id']}"),
                         ("get", f"/admin/sources/{source['id']}/files")):
        assert getattr(client, method)(path, headers=gina(world)).status_code == 404
    changed = client.patch(f"/admin/sources/{source['id']}", headers=gina(world), json={"enabled": False})
    assert changed.status_code == 404
    assert client.get("/admin/sources", headers=alice(world)).status_code == 403


def test_changes_to_sources_are_audited_and_removing_one_keeps_its_policies(world, client, folder):
    touch(folder / "Gym Policy.md", POLICY)
    source = add(client, harriet(world))
    sync(client, harriet(world), source)
    paused = client.patch(f"/admin/sources/{source['id']}", headers=harriet(world), json={"enabled": False}).json()
    assert paused["enabled"] is False
    assert client.delete(f"/admin/sources/{source['id']}", headers=harriet(world)).status_code == 200
    assert "Gym_Policy.md" in documents(client, harriet(world))
    assert owner_rows(world, "SELECT count(*) AS n FROM source_files")[0].n == 0
    actions = {event["action"] for event in client.get("/admin/audit", headers=harriet(world)).json()}
    assert {"source_added", "source_synced", "source_changed", "source_removed"} <= actions
    client.delete("/admin/documents/Gym_Policy.md?purge=true", headers=harriet(world))


def test_a_source_already_syncing_is_not_started_twice(world, client, folder):
    source = add(client, harriet(world))
    assert SourceStore(world.engine, "acme").claim(source["id"])
    assert client.post(f"/admin/sources/{source['id']}/sync", headers=harriet(world)).status_code == 409


def test_the_scheduler_syncs_what_is_due_and_skips_what_is_not(world, client, folder):
    touch(folder / "Gym Policy.md", POLICY)
    source = add(client, harriet(world))
    scheduler = SourceScheduler(world.registry, 60)
    assert scheduler.tick() == 1  # never synced, so it's due
    assert scheduler.tick() == 0  # synced a moment ago, and due again in an hour
    store = SourceStore(world.engine, "acme")
    assert store.get(source["id"])["status"] == "ok"
    client.patch(f"/admin/sources/{source['id']}", headers=harriet(world), json={"enabled": False})
    assert store.due() == []
    client.delete("/admin/documents/Gym_Policy.md?purge=true", headers=harriet(world))


class Unreachable:
    def list_files(self) -> list[SourceFile]:
        raise ConnectorError("Google Drive didn't answer. The next sync will try again.")

    def fetch(self, file: SourceFile) -> bytes:
        raise AssertionError("not reached")


def test_a_source_that_cannot_be_reached_is_marked_failed(world, client, folder):
    source = add(client, harriet(world))
    store = SourceStore(world.engine, "acme")
    store.claim(source["id"])
    sync_source(store, world.registry.for_tenant("acme").knowledge, store.get(source["id"]), Unreachable())
    failed = store.get(source["id"])
    assert failed["status"] == "failed" and "didn't answer" in failed["last_error"]
