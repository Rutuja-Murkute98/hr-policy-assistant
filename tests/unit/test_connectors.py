"""Sources of policies, with Google Drive and Microsoft Graph replaced by stand-ins. See docs/DECISIONS.md, D59."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import config
from app.connectors.base import ConnectorError, SourceFile, changes, policy_name
from app.connectors.folder import FolderConnector, organisation_folder
from app.connectors.google_drive import GOOGLE_DOC, GoogleDriveConnector
from app.connectors.sharepoint import SharePointConnector
from app.sources import SourceSettings, available_kinds, check_settings


def test_only_new_and_changed_files_are_fetched_and_gone_ones_are_found():
    files = [SourceFile("a", "A.pdf", "1"), SourceFile("b", "B.pdf", "2"), SourceFile("c", "C.pdf", "1")]
    changed, gone = changes(files, {"a": "1", "b": "1", "d": "1"})
    assert [file.key for file in changed] == ["b", "c"] and gone == ["d"]


@pytest.mark.parametrize(("name", "policy"), [
    ("Leave Policy (2026).PDF", "Leave_Policy_2026.pdf"), ("leave-policy.docx", "leave-policy.docx"),
    ("  ###.md", "Document.md"), ("Política de Licença.md", "Pol_tica_de_Licen_a.md"),
])
def test_a_file_name_becomes_a_policy_name(name, policy):
    assert policy_name(name) == policy


# A watched folder

def test_a_folder_must_be_inside_the_organisations_own(tmp_path):
    (tmp_path / "acme" / "hr").mkdir(parents=True)
    assert organisation_folder(tmp_path, "acme", "hr") == (tmp_path / "acme" / "hr").resolve()
    assert organisation_folder(tmp_path, "acme", "") == (tmp_path / "acme").resolve()
    for escape in ("../globex", "hr/../../globex", str(tmp_path / "globex"), "../../etc"):
        with pytest.raises(ConnectorError):
            organisation_folder(tmp_path, "acme", escape)


def test_a_folder_lists_readable_files_with_versions_that_change(tmp_path):
    (tmp_path / "Old").mkdir()
    (tmp_path / "Leave.md").write_text("# Leave\n\n## 1. Days\n\nEighteen days.", encoding="utf-8")
    (tmp_path / "Old" / "Travel.docx").write_bytes(b"x")
    (tmp_path / "notes.exe").write_bytes(b"x")
    connector = FolderConnector(tmp_path)
    files = connector.list_files()
    assert [(file.key, file.name) for file in files] == [("Leave.md", "Leave.md"), ("Old/Travel.docx", "Travel.docx")]
    before = files[0].version
    (tmp_path / "Leave.md").write_text("# Leave\n\n## 1. Days\n\nTwenty days.", encoding="utf-8")
    os.utime(tmp_path / "Leave.md", ns=(10**18, 10**18))
    assert connector.list_files()[0].version != before
    assert connector.fetch(files[0]).startswith(b"# Leave")


def test_a_folder_never_reads_outside_itself(tmp_path):
    inside = tmp_path / "acme"
    inside.mkdir()
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    with pytest.raises(ConnectorError):
        FolderConnector(inside).fetch(SourceFile("../secret.md", "secret.md", "1"))
    with pytest.raises(ConnectorError, match="doesn't exist"):
        FolderConnector(tmp_path / "missing").list_files()


def test_a_link_out_of_the_folder_is_skipped(tmp_path):
    inside = tmp_path / "acme"
    inside.mkdir()
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    try:
        (inside / "Link.md").symlink_to(tmp_path / "secret.md")
    except OSError:
        pytest.skip("This system doesn't allow making links")
    assert FolderConnector(inside).list_files() == []


# Google Drive

@pytest.fixture(scope="module")
def service_account():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return {"client_email": "helpdesk@project.iam.gserviceaccount.com", "private_key": private,
            "public_key": key.public_key()}


def drive(handler, account) -> GoogleDriveConnector:
    return GoogleDriveConnector("folder_0123456789", {k: v for k, v in account.items() if k != "public_key"},
                                client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_drive_lists_a_folder_page_by_page_and_exports_google_docs(service_account):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            claims = jwt.decode(form["assertion"][0], service_account["public_key"], algorithms=["RS256"],
                                audience="https://oauth2.googleapis.com/token")
            assert claims["scope"].endswith("drive.readonly") and claims["iss"].startswith("helpdesk@")
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer token"
        query = parse_qs(urlparse(str(request.url)).query)
        if request.url.path == "/drive/v3/files" and "pageToken" not in query:
            assert query["q"] == ["'folder_0123456789' in parents and trashed = false"]
            return httpx.Response(200, json={"nextPageToken": "next", "files": [
                {"id": "1", "name": "Leave.pdf", "mimeType": "application/pdf", "md5Checksum": "abc"},
                {"id": "2", "name": "Logo.png", "mimeType": "image/png", "md5Checksum": "def"}]})
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": "3", "name": "Travel Policy", "mimeType": GOOGLE_DOC, "modifiedTime": "2026-11-01T10:00:00Z"}]})
        if request.url.path == "/drive/v3/files/3/export":
            assert query["mimeType"] == ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
            return httpx.Response(200, content=b"word")
        assert query.get("alt") == ["media"]
        return httpx.Response(200, content=b"%PDF-1.4")

    connector = drive(handler, service_account)
    files = connector.list_files()
    assert [(file.key, file.name, file.version) for file in files] == [
        ("1", "Leave.pdf", "abc"), ("3", "Travel Policy.docx", "doc-2026-11-01T10:00:00Z")]
    assert connector.fetch(files[0]) == b"%PDF-1.4" and connector.fetch(files[1]) == b"word"
    assert sum(request.url.host == "oauth2.googleapis.com" for request in seen) == 1  # the token is reused


@pytest.mark.parametrize(("status", "message"), [(404, "shared with the service account"), (403, "refused")])
def test_drive_problems_are_explained(service_account, status, message):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(status)

    with pytest.raises(ConnectorError, match=message):
        drive(handler, service_account).list_files()


def test_a_service_account_google_refuses_is_explained(service_account):
    connector = drive(lambda request: httpx.Response(400, json={"error": "invalid_grant"}), service_account)
    with pytest.raises(ConnectorError, match="service account"):
        connector.list_files()


# SharePoint, through Microsoft Graph

def test_sharepoint_lists_a_folder_page_by_page_and_fetches_a_file():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            assert request.url.path == "/contoso-tenant/oauth2/v2.0/token"
            assert parse_qs(request.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer graph-token"
        path = request.url.raw_path.decode()
        if "children" in path:
            assert "/sites/site%2C1/drives/drive-1/root:/HR%20Policies:/children" in path
            return httpx.Response(200, json={"@odata.nextLink": "https://graph.microsoft.com/v1.0/next", "value": [
                {"id": "a", "name": "Leave.docx", "file": {}, "cTag": "c1"},
                {"id": "f", "name": "Archive", "folder": {}}]})
        if path.endswith("/next"):
            return httpx.Response(200, json={"value": [{"id": "b", "name": "Travel.pdf", "file": {}, "eTag": "e2"}]})
        assert path.endswith("/items/a/content")
        return httpx.Response(200, content=b"word file")

    connector = SharePointConnector("contoso-tenant", "client", "secret", "site,1", "drive-1", "/HR Policies/",
                                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    files = connector.list_files()
    assert [(file.key, file.name, file.version) for file in files] == [("a", "Leave.docx", "c1"),
                                                                       ("b", "Travel.pdf", "e2")]
    assert connector.fetch(files[0]) == b"word file"


def test_a_missing_sharepoint_folder_is_explained():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(404)

    connector = SharePointConnector("t", "c", "s", "site", "drive", "Nope",
                                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ConnectorError, match="wasn't found"):
        connector.list_files()


# Which sources a deployment allows

def test_a_kind_is_only_offered_once_its_owner_has_set_it_up(monkeypatch, tmp_path):
    for name in ("SOURCES_ROOT", "GOOGLE_DRIVE_CREDENTIALS_FILE", "SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID",
                 "SHAREPOINT_CLIENT_SECRET"):
        monkeypatch.setattr(config, name, "")
    assert available_kinds() == []
    with pytest.raises(ConnectorError, match="isn't set up"):
        check_settings("folder", SourceSettings(path="hr"), "acme")
    monkeypatch.setattr(config, "SOURCES_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "GOOGLE_DRIVE_CREDENTIALS_FILE", str(tmp_path / "key.json"))
    assert available_kinds() == ["folder", "google_drive"]


@pytest.mark.parametrize(("kind", "settings"), [
    ("google_drive", {"folder_id": "abc"}), ("google_drive", {"folder_id": "x' or name contains '"}),
    ("sharepoint", {"site_id": "", "drive_id": "d"}), ("sharepoint", {"site_id": "s", "drive_id": "d", "folder": "a/../b"}),
    ("folder", {"path": "../globex"}),
])
def test_settings_that_could_reach_elsewhere_are_refused(monkeypatch, tmp_path, kind, settings):
    monkeypatch.setattr(config, "SOURCES_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "GOOGLE_DRIVE_CREDENTIALS_FILE", "key.json")
    for name in ("SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET"):
        monkeypatch.setattr(config, name, "set")
    with pytest.raises(ConnectorError):
        check_settings(kind, SourceSettings(**settings), "acme")


def test_good_settings_are_tidied(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SOURCES_ROOT", str(tmp_path))
    assert check_settings("folder", SourceSettings(path=" /hr/ "), "acme") == SourceSettings(path="hr")
    assert json.loads(SourceSettings(path="hr").model_dump_json(exclude_defaults=True)) == {"path": "hr"}
    assert Path(config.SOURCES_ROOT) == tmp_path
