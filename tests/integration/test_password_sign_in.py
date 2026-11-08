"""Password sign-in on a real PostgreSQL, through the API. No model API is called."""
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc, text

from app import config, security
from app.config import Tenant
from app.db import get_engine
from app.identity import ApiKeys, AuditLog, Identity, Organisations, Profiles, Users
from app.main import app
from app.passwords import PasswordAccounts
from app.rag.store import run_migrations
from app.tenancy import APP_ROLE, TenantEngine, grant_app_role
from ops.create_admin import create_admin

pytestmark = pytest.mark.integration

OTHER = Tenant(id="pw_other", company_name="Other", legal_name="Other Ltd", hr_email="hr@other.example",
               product_name="Other HR", logo_initials="O")
PASSWORD = "a-long-test-phrase-" + secrets.token_hex(4)
NEW_PASSWORD = "another-test-phrase-" + secrets.token_hex(4)


@pytest.fixture(scope="module")
def world(database_uri):
    run_migrations(database_uri)
    engine = get_engine(database_uri)
    organisations, users = Organisations(engine), Users(engine)
    organisations.ensure(config.tenant)
    organisations.ensure(OTHER)
    passwords = PasswordAccounts(engine, users)
    identity = Identity(organisations, users, ApiKeys(engine), AuditLog(engine), passwords, passwords,
                        profiles=Profiles(engine))
    yield SimpleNamespace(engine=engine, identity=identity, users=users, passwords=passwords)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users"))
        connection.execute(text("DELETE FROM organisations WHERE id = 'pw_other'"))
    engine.dispose()


@pytest.fixture
def client(world, monkeypatch):
    with world.engine.begin() as connection:
        connection.execute(text("DELETE FROM users"))
    security.rate_limit.reset()
    monkeypatch.setattr(config, "AUTH_MODE", "password")
    monkeypatch.setattr(config, "API_KEY", "shared-demo-key")
    monkeypatch.setattr(config, "ALLOW_SIGNUP", True)
    monkeypatch.setattr(config, "SIGNUP_EMAIL_DOMAINS", [])
    monkeypatch.setattr(app.state, "service", SimpleNamespace(), raising=False)
    monkeypatch.setattr(app.state, "identity", world.identity, raising=False)
    with TestClient(app) as test_client:
        yield test_client
    security.rate_limit.reset()


def sign_up(client, email="asha@example.test", password=PASSWORD, name="Asha Rao", remember=True):
    return client.post("/auth/signup", json={"email": email, "password": password, "display_name": name,
                                             "remember": remember})


def log_in(client, email="asha@example.test", password=PASSWORD, remember=True):
    return client.post("/auth/login", json={"email": email, "password": password, "remember": remember})


def bearer(reply) -> dict:
    return {"Authorization": f"Bearer {reply.json()['token']}"}


def test_someone_signs_up_and_can_sign_in_again_on_a_later_visit(client):
    created = sign_up(client)
    assert created.status_code == 201
    assert created.json()["session"]["email"] == "asha@example.test"
    assert created.json()["session"]["display_name"] == "Asha Rao"
    returning = log_in(client)
    assert returning.status_code == 200
    me = client.get("/me", headers=bearer(returning))
    assert me.status_code == 200 and me.json()["email"] == "asha@example.test"


def test_a_kept_session_still_works_for_a_new_visit(client):
    token = bearer(sign_up(client))
    with TestClient(app) as later_visit:  # a restart of the API changes nothing, as it's in the database
        assert later_visit.get("/me", headers=token).status_code == 200


def test_only_a_hash_of_the_password_and_token_are_stored(client, world):
    reply = sign_up(client)
    with world.engine.connect() as connection:
        stored = connection.execute(text("SELECT password_hash FROM user_credentials")).scalar_one()
        token_hash = connection.execute(text("SELECT token_hash FROM user_sessions")).scalar_one()
    assert stored.startswith("$argon2id$") and PASSWORD not in stored
    assert token_hash != reply.json()["token"]


def test_a_wrong_password_and_an_unknown_email_get_the_same_reply(client):
    sign_up(client)
    wrong = log_in(client, password="not-the-right-phrase")
    unknown = log_in(client, email="nobody@example.test")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_new_account_is_only_ever_an_employee_with_no_hr_record(client, world):
    reply = client.post("/auth/signup", json={"email": "sneaky@example.test", "password": PASSWORD, "display_name": "S",
                                              "role": "hr_admin", "employee_id": "E1001", "tenant_id": "pw_other"})
    session = reply.json()["session"]
    assert session["role"] == "employee"
    assert session["tenant_id"] == config.TENANT_ID
    assert session["user_id"] != "E1001"
    assert client.get("/admin/users", headers=bearer(reply)).status_code == 403


def test_signing_up_cannot_take_over_an_account_that_has_no_password_yet(client, world):
    world.users.create(config.TENANT_ID, "boss@example.test", "hr_admin")
    taken = sign_up(client, email="BOSS@example.test")
    assert taken.status_code == 409
    assert log_in(client, email="boss@example.test").status_code == 401


def test_a_weak_password_is_refused(client):
    for password in ("short", "1234567890", "aaaaaaaaaaaa", "asha@example.test!"):
        assert sign_up(client, password=password).status_code == 409


def test_sign_up_can_be_limited_or_switched_off(client, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_EMAIL_DOMAINS", ["company.test"])
    assert sign_up(client, email="asha@example.test").status_code == 409
    assert sign_up(client, email="asha@company.test").status_code == 201
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    assert sign_up(client, email="ravi@company.test").status_code == 409
    assert client.get("/auth/options").json()["signup_enabled"] is False


def test_signing_out_ends_the_session(client):
    headers = bearer(sign_up(client))
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/me", headers=headers).status_code == 401


def test_an_expired_session_stops_working(client, world):
    headers = bearer(sign_up(client))
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE user_sessions SET expires_at = now() - interval '1 minute'"))
    assert client.get("/me", headers=headers).status_code == 401
    assert world.passwords.purge_expired() == 1


def test_keep_me_signed_in_decides_how_long_a_session_lasts(client, world):
    kept = sign_up(client, remember=True).json()
    assert kept["max_age_seconds"] == config.SESSION_REMEMBER_DAYS * 86_400
    assert log_in(client, remember=False).json()["max_age_seconds"] is None
    with world.engine.connect() as connection:
        hours = connection.execute(text(
            "SELECT extract(epoch FROM expires_at - created_at) / 3600 FROM user_sessions ORDER BY expires_at"
        )).scalars().all()
    assert round(hours[0]) == config.SESSION_HOURS
    assert round(hours[1]) == config.SESSION_REMEMBER_DAYS * 24


def test_suspending_a_person_or_organisation_stops_them_at_once(client, world):
    reply = sign_up(client)
    headers, user_id = bearer(reply), None
    with world.engine.connect() as connection:
        user_id = connection.execute(text("SELECT id FROM users WHERE email = 'asha@example.test'")).scalar_one()
    world.users.update(config.TENANT_ID, user_id, None, False)
    assert client.get("/me", headers=headers).status_code == 401
    assert "isn't active" in log_in(client).json()["detail"]


def test_shared_keys_stop_working(client):
    assert client.get("/me", headers={"X-API-Key": "shared-demo-key"}).status_code == 401
    assert client.get("/admin/users", headers={"X-Admin-Key": "anything"}).status_code == 401


def test_too_many_tries_at_one_account_are_stopped(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ACCOUNT_PER_MINUTE", 3)
    sign_up(client, email="ravi@example.test")  # counts as one
    codes = [log_in(client, email="ravi@example.test", password="not-the-right-one").status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    assert log_in(client, email="other@example.test", password="not-the-right-one").status_code == 401


def admin_of(client, world, tenant_id=config.TENANT_ID, email="hr@example.test") -> dict:
    user = world.users.create(tenant_id, email, "hr_admin")
    world.passwords.set_password(user["id"], tenant_id, PASSWORD)
    return bearer(log_in(client, email=email))


def test_an_hr_admin_gives_a_code_so_someone_they_added_can_set_a_password(client, world):
    headers = admin_of(client, world)
    added = client.post("/admin/users", json={"email": "new@example.test", "employee_id": "E2000"}, headers=headers).json()
    assert log_in(client, email="new@example.test").status_code == 401

    code = client.post(f"/admin/users/{added['id']}/setup-code", headers=headers).json()["code"]
    reply = client.post("/auth/password", json={"email": "new@example.test", "code": code.lower(),
                                                "password": NEW_PASSWORD, "remember": True})
    assert reply.status_code == 200
    assert reply.json()["session"]["user_id"] == "E2000"
    assert log_in(client, email="new@example.test", password=NEW_PASSWORD).status_code == 200

    again = client.post("/auth/password", json={"email": "new@example.test", "code": code, "password": PASSWORD})
    assert again.status_code == 401  # a code only works once


def test_a_reset_ends_older_sessions(client, world):
    headers = admin_of(client, world)
    older = bearer(sign_up(client))
    user_id = next(user["id"] for user in client.get("/admin/users", headers=headers).json()
                   if user["email"] == "asha@example.test")
    code = client.post(f"/admin/users/{user_id}/setup-code", headers=headers).json()["code"]
    assert client.post("/auth/password", json={"email": "asha@example.test", "code": code,
                                               "password": NEW_PASSWORD}).status_code == 200
    assert client.get("/me", headers=older).status_code == 401


def test_a_wrong_or_expired_code_is_refused(client, world):
    headers = admin_of(client, world)
    added = client.post("/admin/users", json={"email": "new@example.test"}, headers=headers).json()
    code = client.post(f"/admin/users/{added['id']}/setup-code", headers=headers).json()["code"]
    fields = {"email": "new@example.test", "password": NEW_PASSWORD}
    assert client.post("/auth/password", json={**fields, "code": "AAAA-BBBB-CCCC"}).status_code == 401
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE user_credentials SET setup_code_expires_at = now() - interval '1 minute'"))
    assert client.post("/auth/password", json={**fields, "code": code}).status_code == 401


def test_an_hr_admin_cannot_make_a_code_for_another_organisation_or_a_platform_admin(client, world):
    headers = admin_of(client, world)
    outsider = world.users.create("pw_other", "bob@other.example", "employee")
    platform = world.users.create(config.TENANT_ID, "root@example.test", "platform_admin")
    assert client.post(f"/admin/users/{outsider['id']}/setup-code", headers=headers).status_code == 404
    assert client.post(f"/admin/users/{platform['id']}/setup-code", headers=headers).status_code == 404


def test_a_sign_in_lands_in_the_persons_own_organisation(client, world):
    headers = admin_of(client, world, "pw_other", "gina@other.example")
    me = client.get("/me", headers=headers).json()
    assert me["tenant_id"] == "pw_other" and me["organisation"]["company_name"] == "Other"
    assert [user["email"] for user in client.get("/admin/users", headers=headers).json()] == ["gina@other.example"]


def test_two_accounts_cannot_share_an_employee_record(client, world):
    headers = admin_of(client, world)
    client.post("/admin/users", json={"email": "first@example.test", "employee_id": "E3000"}, headers=headers)
    assert client.post("/admin/users", json={"email": "second@example.test", "employee_id": "E3000"},
                       headers=headers).status_code == 409
    joined = sign_up(client, email="third@example.test").json()["session"]
    assert client.patch(f"/admin/users/{joined['user_id']}", json={"employee_id": "E3000"},
                        headers=headers).status_code == 409
    assert client.patch(f"/admin/users/{joined['user_id']}", json={"employee_id": "E3001"},
                        headers=headers).status_code == 200
    assert client.get("/admin/users", headers=headers).status_code == 200


@pytest.mark.parametrize("table", ["user_credentials", "user_sessions"])
def test_the_apps_tenant_role_cannot_read_password_tables(client, world, table):
    sign_up(client)
    with pytest.raises(exc.ProgrammingError, match="permission denied"):
        with TenantEngine(world.engine, config.TENANT_ID).begin() as connection:
            connection.execute(text(f"SELECT * FROM {table}"))


def test_restoring_a_backup_does_not_give_the_app_access_to_password_tables(client, world):
    with world.engine.begin() as connection:
        grant_app_role(connection)
        allowed = connection.execute(text("SELECT has_table_privilege(:role, 'user_credentials', 'SELECT')"),
                                     {"role": APP_ROLE}).scalar()
    assert allowed is False


def test_the_operator_can_make_the_first_admin(client, world):
    create_admin(world.engine, "first@example.test", "First Admin", PASSWORD)
    headers = bearer(log_in(client, email="first@example.test"))
    assert client.get("/me", headers=headers).json()["role"] == "hr_admin"
    assert client.get("/admin/users", headers=headers).status_code == 200


def test_every_sign_in_step_is_in_the_audit_log(client, world):
    headers = admin_of(client, world)
    sign_up(client)
    log_in(client, password="not-the-right-phrase")
    client.post("/auth/logout", headers=headers)
    with world.engine.connect() as connection:
        actions = set(connection.execute(text("SELECT action FROM audit_events")).scalars())
        logged = " ".join(connection.execute(text("SELECT details::text FROM audit_events")).scalars())
    assert {"signup", "login", "login_failed", "logout"} <= actions
    assert PASSWORD not in logged


# Profiles

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
DETAILS = {"display_name": "Asha  Rao", "job_title": "Engineer", "department": "Platform", "location": "Pune"}


def test_a_new_account_has_a_profile_with_its_name(client):
    headers = bearer(sign_up(client))
    profile = client.get("/me/profile", headers=headers).json()
    assert profile == {"display_name": "Asha Rao", "email": "asha@example.test", "job_title": "", "department": "",
                       "location": "", "photo": ""}


def test_profile_details_are_saved_and_the_new_name_is_used_everywhere(client):
    headers = bearer(sign_up(client))
    saved = client.put("/me/profile", headers=headers, json=DETAILS)
    assert saved.status_code == 200
    assert saved.json()["display_name"] == "Asha Rao"  # extra spaces are tidied
    assert saved.json()["department"] == "Platform"
    assert client.get("/me", headers=headers).json()["display_name"] == "Asha Rao"
    assert log_in(client).json()["session"]["display_name"] == "Asha Rao"


@pytest.mark.parametrize("change", [{"display_name": ""}, {"display_name": "<script>x</script>"},
                                    {"job_title": "a" * 101}, {"location": "line\nbreak"}])
def test_profile_details_must_be_plain_text(client, change):
    headers = bearer(sign_up(client))
    assert client.put("/me/profile", headers=headers, json={**DETAILS, **change}).status_code == 422


def test_a_photo_is_saved_and_removed(client):
    headers = bearer(sign_up(client))
    saved = client.put("/me/profile/photo", headers=headers, files={"photo": ("me.jpg", JPEG, "image/jpeg")})
    assert saved.status_code == 200
    assert saved.json()["photo"].startswith("data:image/jpeg;base64,")
    removed = client.delete("/me/profile/photo", headers=headers)
    assert removed.json()["photo"] == ""


def test_a_photo_is_recognised_by_its_contents_not_its_name(client):
    headers = bearer(sign_up(client))
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    named_as_png = client.put("/me/profile/photo", headers=headers, files={"photo": ("me.png", svg, "image/png")})
    assert named_as_png.status_code == 415
    really_png = client.put("/me/profile/photo", headers=headers, files={"photo": ("me.txt", PNG, "text/plain")})
    assert really_png.json()["photo"].startswith("data:image/png;base64,")


def test_a_photo_that_is_too_big_is_refused(client):
    headers = bearer(sign_up(client))
    huge = JPEG + b"\x00" * 300_000
    assert client.put("/me/profile/photo", headers=headers,
                      files={"photo": ("me.jpg", huge, "image/jpeg")}).status_code == 413


def test_each_person_only_reaches_their_own_profile(client, world):
    asha = bearer(sign_up(client))
    client.put("/me/profile", headers=asha, json=DETAILS)
    client.put("/me/profile/photo", headers=asha, files={"photo": ("me.jpg", JPEG, "image/jpeg")})
    gina = admin_of(client, world, "pw_other", "gina@other.example")
    other = client.get("/me/profile", headers=gina).json()
    assert other["email"] == "gina@other.example" and other["photo"] == "" and other["department"] == ""


def test_a_shared_key_has_no_profile(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "demo")
    response = client.get("/me/profile", headers={"X-API-Key": "shared-demo-key", "X-User-Id": "E1001"})
    assert response.status_code == 404


def test_profile_changes_are_in_the_audit_log(client, world):
    headers = bearer(sign_up(client))
    client.put("/me/profile", headers=headers, json=DETAILS)
    client.put("/me/profile/photo", headers=headers, files={"photo": ("me.jpg", JPEG, "image/jpeg")})
    with world.engine.connect() as connection:
        actions = set(connection.execute(text("SELECT action FROM audit_events")).scalars())
    assert {"profile_changed", "profile_photo_changed"} <= actions


def test_the_apps_tenant_role_keeps_profiles_apart(client, world):
    headers = bearer(sign_up(client))
    client.put("/me/profile", headers=headers, json=DETAILS)
    with TenantEngine(world.engine, "pw_other").begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM user_profiles")).scalar() == 0
